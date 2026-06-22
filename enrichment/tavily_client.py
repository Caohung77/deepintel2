"""
Tavily search client for B2B enrichment.

Four queries per company:
  1. Competitors search
  2. Risk-event search (insolvency, lawsuits, scandals, recalls, fines)
  3. Recent news (last 90 days)
  4. Insolvency question (uses Tavily include_answer to ask + answer directly)

Returns normalised dicts ready to feed into the synthesis prompt.

Public API:
    await tavily_enrich(company_name, own_domain) -> dict[str, list]
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlparse

import httpx
from rapidfuzz import fuzz

CACHE_DIR = Path(__file__).resolve().parent.parent / "cache" / "tavily"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_TTL_SECONDS = 7 * 24 * 3600  # 7 days

TAVILY_ENDPOINT = "https://api.tavily.com/search"

RISK_KEYWORDS = (
    "Insolvenz OR Klage OR Skandal OR Rückruf OR Bußgeld OR "
    "Datenleck OR Cyberangriff OR Rechtsstreit OR insolvency OR lawsuit OR fine OR recall"
)

RISK_TAG_RX = {
    "litigation": re.compile(r"\b(klage|lawsuit|gericht|court|sue|verklagt|sued)\b", re.I),
    "insolvency": re.compile(r"\b(insolven[zc]|bankrupt|chapter\s*11|liquidation)\b", re.I),
    "fine": re.compile(r"\b(bußgeld|fine|penalty|strafe|sanction)\b", re.I),
    "recall": re.compile(r"\b(rückruf|recall|zurückgerufen)\b", re.I),
    "cyber": re.compile(r"\b(datenleck|cyberangriff|hack(ed|ing)?|breach|leak)\b", re.I),
    "scandal": re.compile(r"\b(skandal|scandal|fraud|betrug|corrupt)\b", re.I),
}


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")[:80]


def _cache_path(key: str) -> Path:
    return CACHE_DIR / f"{_slug(key)}.json"


def _load_cache(key: str) -> Optional[dict]:
    p = _cache_path(key)
    if not p.exists():
        return None
    if (time.time() - p.stat().st_mtime) > CACHE_TTL_SECONDS:
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _save_cache(key: str, data: dict) -> None:
    try:
        _cache_path(key).write_text(json.dumps(data), encoding="utf-8")
    except OSError as e:
        print(f"[tavily] cache write failed: {e}")


async def _tavily_search(
    cx: httpx.AsyncClient,
    query: str,
    api_key: str,
    *,
    topic: str = "general",
    days: Optional[int] = None,
    max_results: int = 8,
    exclude_domains: Optional[List[str]] = None,
    include_domains: Optional[List[str]] = None,
    include_answer: bool = False,
    search_depth: str = "basic",
    time_range: Optional[str] = None,
) -> dict:
    cache_key = f"{topic}_{query}_{days}_{int(include_answer)}_{search_depth}_{time_range}_{','.join(include_domains or [])}"
    cached = _load_cache(cache_key)
    if cached:
        return cached

    payload = {
        "query": query,
        "topic": topic,
        "max_results": max_results,
        "search_depth": search_depth,
    }
    if include_answer:
        payload["include_answer"] = True
    if time_range:
        payload["time_range"] = time_range
    if days and topic == "news":
        payload["days"] = days
    if exclude_domains:
        payload["exclude_domains"] = exclude_domains
    if include_domains:
        payload["include_domains"] = include_domains

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        r = await cx.post(TAVILY_ENDPOINT, json=payload, headers=headers, timeout=30.0)
        r.raise_for_status()
        data = r.json()
    except httpx.HTTPError as e:
        print(f"[tavily] {topic} query failed: {e}")
        return {"results": []}

    _save_cache(cache_key, data)
    return data


def _norm_results(raw: dict) -> List[dict]:
    out = []
    for r in raw.get("results", []) or []:
        out.append({
            "title": r.get("title", "").strip(),
            "url": r.get("url", "").strip(),
            "snippet": (r.get("content") or r.get("snippet") or "").strip()[:600],
            "published_date": r.get("published_date"),
            "score": r.get("score"),
        })
    return out


async def _fetch_result_text(cx: httpx.AsyncClient, url: str) -> str:
    if not url:
        return ""
    try:
        r = await cx.get(
            url,
            headers={"User-Agent": "deepintel2/0.2 insolvency identity check"},
            follow_redirects=True,
            timeout=8.0,
        )
        r.raise_for_status()
    except httpx.HTTPError:
        return ""
    ctype = (r.headers.get("content-type") or "").lower()
    if "text/html" not in ctype and "text/plain" not in ctype and "xml" not in ctype:
        return ""
    text = re.sub(r"<(script|style)\b.*?</\1>", " ", r.text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text[:16000]


async def _augment_insolvency_identity_text(items: List[dict], *, hr_no: Optional[str]) -> None:
    """Fetch a few candidate source pages so HRB/Amtsgericht on the page can validate identity."""
    if not items or os.getenv("INSOLVENCY_FETCH_SOURCE_PAGES", "1").lower() in {"0", "false", "no"}:
        return
    # Keep this bounded; it only enriches identity validation and must not dominate latency.
    candidates = [it for it in items if it.get("url")][:8]
    if not candidates:
        return
    async with httpx.AsyncClient() as cx:
        texts = await asyncio.gather(
            *(_fetch_result_text(cx, it.get("url") or "") for it in candidates),
            return_exceptions=True,
        )
    for it, text in zip(candidates, texts):
        if isinstance(text, str) and text:
            it["_page_text"] = text


def _tag_risk(item: dict) -> Optional[str]:
    blob = f"{item.get('title','')} {item.get('snippet','')}"
    for tag, rx in RISK_TAG_RX.items():
        if rx.search(blob):
            return tag
    return None


# --- Insolvency signal patterns (lead structural, gate bare "insolvent" hardest) ---

# German insolvency aggregators that republish court filings. Used as include_domains
# for a recall pass that survives datacenter-IP throttling (see tavily_enrich).
_INSOLVENCY_DOMAINS = [
    "insolvenzbekanntmachungen.de", "verbraucherschutzforum.berlin",
    "versteigerungskalender.de", "insolvenzradar.de", "infobroker.de",
    "unternehmensregister.de",
]

# Pure company-registry directories. They render an "Insolvenzverfahren" section
# label on EVERY profile regardless of actual status, so a keyword match on such a
# page is structural noise, not a real proceeding. Registries list the company; they
# do NOT establish that an insolvency exists. Never count them as insolvency evidence.
_REGISTRY_NOISE_DOMAINS = ("northdata.de", "northdata.com")

# Court file number, e.g. "2 IN 277/26" — highest-precision proceeding marker.
_COURT_AZ_RX = re.compile(r"\b\d+\s*IN\s*\d+\s*/\s*\d+\b", re.I)

# Proceeding IN PROCESS (opened / preliminary / applied for, not yet concluded).
_VERFAHREN_RX = re.compile(
    r"("
    r"vorl(?:ä|ae|a)ufige[rn]?\s+insolvenzverwalter|insolvenzverwalter\s+(wurde\s+)?bestellt|"
    r"sicherungsma(?:ß|ss)nahmen|"
    r"insolvenzverfahren\b|insolvenzverfahren\s+(wurde\s+)?eröffnet|eröffnung\s+des\s+insolvenzverfahrens|"
    r"vorläufige[s]?\s+insolvenzverfahren|insolvenzantrag|antrag\s+auf\s+insolvenz|"
    r"schutzschirmverfahren|regelinsolvenz|eigenverwaltung|insolvenz\s+angemeldet|"
    r"files?\s+for\s+insolvency|filed\s+for\s+(insolvency|bankruptcy)|"
    r"insolvency\s+proceedings\s+(opened|filed)|chapter\s*11"
    r")",
    re.I,
)

# Already INSOLVENT / concluded / liquidated / dissolved by insolvency.
_INSOLVENT_RX = re.compile(
    r"("
    r"ist\s+insolvent|ist\s+zahlungsunfähig|zahlungsunfähig|"
    r"insolvenzverfahren\s+abgeschlossen|"
    r"durch\s+eröffnung\s+des\s+insolvenzverfahrens.{0,60}?aufgelöst|"
    r"aufgelöst\s+wegen\s+insolvenz|liquidiert|abgewickelt|"
    r"bankrupt|liquidated|wound\s+up"
    r")",
    re.I,
)

# Explicit negation — "not insolvent", "no insolvency procedure", "keine Insolvenz".
# Suppresses WEAK ties only; strong court signals override it.
_NEG_RX = re.compile(
    r"\b(no|not|kein|keine|nicht)\b[^.;!?]{0,40}?(insolven\w+|bankrupt\w*|zahlungsunfähig)",
    re.I,
)

_LEGAL_SUFFIX = {
    "gmbh", "mbh", "co", "kg", "ag", "se", "ug", "kgaa", "ohg", "gbr",
    "ek", "ev", "und", "the", "haftungsbeschränkt", "haftungsbeschraenkt", "cnc",
}
_PROX_WINDOW = 120  # chars between a company-name token and a signal to attribute it
_DEFAULT_IDENTITY_THRESHOLD = 1.0

_UMLAUTS = str.maketrans({
    "ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss",
    "Ä": "ae", "Ö": "oe", "Ü": "ue",
})

_LEGAL_FORM_RX = (
    r"(?:"
    r"gmbh\s*&\s*co\.?\s*kg|"
    r"ug\s*\(\s*haftungsbeschr(?:ä|ae)nkt\s*\)|"
    r"gmbh|ag|se|kgaa|kg|ohg|gbr|"
    r"e\.?\s*k\.?|e\.?\s*v\.?"
    r")"
)
_LEGAL_ENTITY_RX = re.compile(
    rf"\b([A-ZÄÖÜ0-9][A-Za-zÄÖÜäöüß0-9+&.,'’()/\- ]{{0,120}}?\b{_LEGAL_FORM_RX})\b",
    re.I,
)
_COURT_MENTION_RX = re.compile(
    r"\b(?:amtsgericht|registergericht|ag)\s*[:\-]?\s*([A-ZÄÖÜ][A-Za-zÄÖÜäöüß .\-]{1,50})",
    re.I,
)


def _normalise_german(s: str) -> str:
    return (s or "").translate(_UMLAUTS).lower()


def _normalised_words(s: str, *, strip_legal: bool = False) -> List[str]:
    toks = re.split(r"[^a-z0-9]+", _normalise_german(s))
    out = []
    for t in toks:
        if not t:
            continue
        if strip_legal and t in _LEGAL_SUFFIX:
            continue
        out.append(t)
    return out


def _identity_threshold() -> float:
    raw = os.getenv("INSOLVENCY_IDENTITY_THRESHOLD", str(_DEFAULT_IDENTITY_THRESHOLD))
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_IDENTITY_THRESHOLD
    return max(0.0, min(1.0, val))


def _compact_entity(s: str) -> str:
    """Company name key robust to spaces/hyphens/legal forms.

    Example: "Elektro-Technik Jordan GmbH" and "Elektrotechnik Jordan GmbH"
    both become "elektrotechnikjordan", while "Elektro Jordan GmbH" remains
    "elektrojordan". This protects against familiar-but-different names.
    """
    return "".join(_normalised_words(s, strip_legal=True))


def _canonical_legal_name(s: str) -> str:
    return "".join(_normalised_words(s, strip_legal=False))


def _legal_entity_mentions(text: str) -> List[str]:
    mentions: List[str] = []
    seen = set()
    for m in _LEGAL_ENTITY_RX.finditer(text or ""):
        phrase = re.sub(r"\s+", " ", m.group(1)).strip(" -:;,.\t\r\n")
        parts = phrase.split()
        # Generate suffix candidates so titles like "Firmeninsolvenz Jordan GmbH"
        # can still yield the exact legal entity "Jordan GmbH", while
        # "G. Jordan GmbH & Co. KG" remains a different entity.
        candidates = [phrase]
        for i in range(1, min(len(parts), 5)):
            candidates.append(" ".join(parts[i:]))
        for c in candidates:
            key = _canonical_legal_name(c)
            if key and key not in seen:
                seen.add(key)
                mentions.append(c)
    return mentions


def _parse_hr_key(hr_no: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    if not hr_no:
        return None, None
    m_art = re.search(r"\b(HRA|HRB|GnR|GsR|PR|VR)\b", hr_no, re.I)
    m_num = re.search(r"\d+", hr_no)
    art = m_art.group(1).upper() if m_art else None
    return art, (m_num.group() if m_num else None)


def _hr_keys_in_text(text: str) -> set[tuple[str, str]]:
    out = set()
    for m in re.finditer(r"\b(HRA|HRB|GnR|GsR|PR|VR)\s*\.?\s*(\d+)\b", text or "", re.I):
        out.add((m.group(1).upper(), m.group(2)))
    return out


def _contains_hr_key(text: str, hr_no: Optional[str]) -> bool:
    art, num = _parse_hr_key(hr_no)
    if not art or not num:
        return False
    return (art, num) in _hr_keys_in_text(text)


def _court_norm(value: Optional[str]) -> str:
    value = re.sub(r"(?i)\b(amtsgericht|registergericht|ag)\b\.?", "", value or "")
    return _normalise_german(value).strip()


def _court_matches_text(text: str, register_court: Optional[str]) -> Optional[bool]:
    want = _court_norm(register_court)
    if not want:
        return None
    found = []
    for m in _COURT_MENTION_RX.finditer(text or ""):
        court = _court_norm(m.group(1).splitlines()[0])
        court = re.split(r"[,;|/]", court, maxsplit=1)[0].strip()
        if court:
            found.append(court)
    if not found:
        return None
    return any(
        re.search(rf"\b{re.escape(want)}\b", court) or fuzz.token_sort_ratio(want, court) >= 85
        for court in found
    )


def _legal_name_score(company_name: str, text: str) -> float:
    target = _canonical_legal_name(company_name)
    if not target:
        return 0.0
    scores = []
    for mention in _legal_entity_mentions(text):
        cand = _canonical_legal_name(mention)
        if not cand:
            continue
        if cand == target:
            return 1.0
        scores.append(fuzz.ratio(target, cand) / 100.0)
    return max(scores) if scores else 0.0


def _same_company_entity(
    company_name: str,
    text: str,
    *,
    hr_no: Optional[str] = None,
    register_court: Optional[str] = None,
) -> bool:
    """High-precision entity gate for company-scoped search results.

    Tavily/Google can return near-neighbour names even for quoted queries. Token-only
    matching is too permissive for names like "Elektro Technik Jordan GmbH" vs.
    "Elektro Jordan GmbH", so require either the exact Handelsregister key or an
    exact normalized legal-name match at the default threshold.
    """
    if not company_name or not text:
        return False

    threshold = _identity_threshold()
    target_hr = _parse_hr_key(hr_no)
    text_hrs = _hr_keys_in_text(text)
    if target_hr[0] and target_hr[1]:
        if text_hrs and target_hr not in text_hrs:
            return False
        if target_hr in text_hrs:
            court_match = _court_matches_text(text, register_court)
            if court_match is False:
                return False
            return True

    score = _legal_name_score(company_name, text)
    if score >= threshold:
        return True

    if threshold >= 1.0:
        return False

    # Optional recall mode: lower INSOLVENCY_IDENTITY_THRESHOLD to allow close
    # legal-name variants. Token-only matching is deliberately unavailable at the
    # default precision because it makes short names like "Jordan GmbH" unsafe.
    name_key = _compact_entity(company_name)
    text_key = _compact_entity(text)
    if len(name_key) >= 8 and name_key in text_key:
        return True
    return False


def _core_tokens(name: str) -> List[str]:
    toks = re.split(r"[^a-z0-9äöüß]+", (name or "").lower())
    return [t for t in toks if len(t) >= 3 and t not in _LEGAL_SUFFIX]


def _token_matches(text_low: str, token: str) -> bool:
    """Whole-word match. Substring matching falsely tied 'Bau' to 'Bauer',
    attributing another firm's insolvency to a co-named company."""
    return re.search(rf"\b{re.escape(token)}\b", text_low) is not None


def _token_positions(text_low: str, tokens: List[str]) -> List[int]:
    pos: List[int] = []
    for t in tokens:
        for m in re.finditer(rf"\b{re.escape(t)}\b", text_low):
            pos.append(m.start())
    return pos


_PROX_AFTER = 40  # name may trail the signal only by a tight margin


def _attributable(rx: re.Pattern, text: str, name_pos: List[int]) -> bool:
    """True if a pattern match plausibly attaches to THIS company, not a
    co-listed one. German insolvency records put the company name *before* its
    signal ("X GmbH, <addr>. Durch Beschluss ... (Az) wurde ... bestellt"), so
    we attribute when a name token precedes the signal within _PROX_WINDOW, or
    trails it within a tight _PROX_AFTER. A co-listed name sitting after another
    firm's Aktenzeichen is therefore not attributed."""
    for m in rx.finditer(text):
        for p in name_pos:
            if 0 <= (m.start() - p) <= _PROX_WINDOW:   # name before signal
                return True
            if 0 <= (p - m.end()) <= _PROX_AFTER:      # name shortly after signal
                return True
    return False


def _classify_insolvency(
    company_name: str,
    answer: str,
    items: List[dict],
    *,
    hr_no: Optional[str] = None,
    register_court: Optional[str] = None,
) -> dict:
    """Derive two booleans from company-attributable signals in the results.

    insolvenzverfahren -> SOFT news signal: an insolvency is reported (proceeding,
                          court Az, or even liquidation phrasing) but NOT officially
                          confirmed -> "investigate".
    insolvenz          -> always False here; "amtlich bestätigt" is set ONLY by the
                          official portal override in tavily_enrich, never from news.

    Tavily's synthesised ``answer`` is NOT trusted for the booleans — it tends to
    echo whichever narrative dominates the index (e.g. an old acquisition) and
    produced a false "not insolvent". Instead each result item is checked for an
    insolvency signal *within a character window of the company name*, so phrases
    belonging to other companies on the same page do not bleed in. Strong court
    signals (Aktenzeichen, "vorläufiger Insolvenzverwalter", "eröffnet") are not
    suppressed by negation; only weak/ambiguous matches are.
    """
    answer = (answer or "").strip()
    core = _core_tokens(company_name)
    verfahren = False
    hits: List[dict] = []

    for it in items:
        # Skip pure company-registry directories: they carry an "Insolvenzverfahren"
        # label on every profile, so a keyword hit there is structural noise, not a
        # real proceeding. A registry listing must never raise insolvency suspicion.
        host = (urlparse(it.get("url", "") or "").netloc or "").lower().replace("www.", "")
        if any(host == d or host.endswith("." + d) for d in _REGISTRY_NOISE_DOMAINS):
            continue
        text = f"{it.get('title','')} {it.get('snippet','')} {it.get('_page_text','')}"
        if not _same_company_entity(
            company_name, text, hr_no=hr_no, register_court=register_court,
        ):
            continue
        title_text = it.get("title", "") or ""
        title_tied = _same_company_entity(
            company_name, title_text, hr_no=hr_no, register_court=register_court,
        )
        low = text.lower()
        name_pos = _token_positions(low, core)
        if not name_pos and not _contains_hr_key(text, hr_no):
            continue

        # If the exact register key is present, the result is already tied to this
        # legal entity. If the title itself is the exact legal entity, Google/Tavily
        # snippets can place the court text slightly farther away, so allow item-wide
        # markers. Otherwise the signal must sit near the company name.
        hr_tied = _contains_hr_key(text, hr_no)
        if hr_tied or title_tied:
            az = bool(_COURT_AZ_RX.search(text))
            v = bool(_VERFAHREN_RX.search(text))
            i = bool(_INSOLVENT_RX.search(text))
        else:
            az = _attributable(_COURT_AZ_RX, text, name_pos)
            v = _attributable(_VERFAHREN_RX, text, name_pos)
            i = _attributable(_INSOLVENT_RX, text, name_pos)

        # Negation suppresses only when there is no strong court-tied signal.
        if _NEG_RX.search(text) and not (az or v or i):
            continue

        # NEWS tier is SOFT only. Any insolvency signal — proceeding, court Az, or
        # even "liquidiert"/"ist insolvent" — sets insolvenzverfahren ("investigate,
        # reported but not officially confirmed"). `insolvenz` (amtlich bestätigt) is
        # NEVER set from news; only the official portal can set it (in tavily_enrich).
        if v or az or i:
            verfahren = True
            hits.append(it)

    # Only pages with an attributable insolvency signal count as evidence. Title-only
    # same-name pages are deliberately excluded; they made unrelated company/profile
    # pages look like sources for insolvency.
    ev = list(hits)
    return {
        "insolvenzverfahren": verfahren,
        "insolvenz": False,
        "answer": answer,
        "evidence": [
            {"title": it.get("title"), "url": it.get("url"), "snippet": it.get("snippet")}
            for it in ev[:5]
        ],
    }


async def tavily_enrich(company_name: str, own_domain: Optional[str] = None,
                        hr_no: Optional[str] = None,
                        register_court: Optional[str] = None) -> dict:
    """Run 3 Tavily searches and normalise into competitors / news / risk_events.

    hr_no / register_court (Handelsregister no. + Registergericht) are woven into
    the insolvency query for exact company disambiguation when supplied.
    """
    api_key = os.getenv("TAVILY_API_KEY")
    _empty_insolvency = {
        "insolvenzverfahren": False,
        "insolvenz": False,
        "answer": "",
        "evidence": [],
    }

    if not api_key:
        print("[tavily] TAVILY_API_KEY not set — skipping enrichment")
        return {"competitor_snippets": [], "news": [], "risk_events": [], "insolvency": _empty_insolvency}

    if not company_name:
        return {"competitor_snippets": [], "news": [], "risk_events": [], "insolvency": _empty_insolvency}

    exclude = []
    if own_domain:
        d = urlparse(own_domain).netloc or own_domain
        exclude.append(d.replace("www.", ""))

    async with httpx.AsyncClient() as cx:
        comp_q = f'"{company_name}" competitors OR Wettbewerber OR alternatives'
        risk_q = f'"{company_name}" ({RISK_KEYWORDS})'
        news_q = f'"{company_name}"'
        # Insolvency search — keyword-first with the EXACT quoted company name, the way a
        # Google query surfaces the actual Bekanntmachung (verbose NL questions diluted it
        # to generic insolvency listicles). The Handelsregister NUMBER pins identity for
        # co-named firms. We deliberately OMIT the register court: the insolvency court
        # differs from the register court (e.g. registered AG Stuttgart, proceeding AG
        # Ludwigsburg), so forcing "Amtsgericht <Registergericht>" biases against the real
        # filing. Two passes merged for recall: the bare proceeding term, and the
        # proceeding-specific markers (Insolvenzverwalter / Antrag / eröffnet / Az).
        inso_id = f' ({hr_no.strip()})' if hr_no else ""
        inso_q = f'insolvenzverfahren insolvenz "{company_name}"{inso_id}'
        inso_q2 = (
            f'"{company_name}"{inso_id} insolvenzverwalter OR insolvenzantrag '
            f'OR "Insolvenzverfahren eröffnet" OR Aktenzeichen'
        )
        # Third pass restricted to German insolvency aggregators that republish court
        # filings. Critical for RECALL from datacenter IPs: Tavily throttles broad
        # queries from server IPs (prod returned 2 results where a residential IP got
        # 5 and missed the Az source), but a domain-scoped query still surfaces the
        # aggregator pages. This is the IP-robust path to the proceeding evidence.
        inso_q3 = f'insolvenz insolvenzverfahren "{company_name}"{inso_id}'

        comp_raw, risk_raw, news_raw, inso_raw, inso_raw2, inso_raw3 = await asyncio.gather(
            _tavily_search(cx, comp_q, api_key, exclude_domains=exclude, max_results=8),
            _tavily_search(cx, risk_q, api_key, exclude_domains=exclude, max_results=10),
            _tavily_search(cx, news_q, api_key, topic="news", days=90, max_results=10),
            _tavily_search(
                cx, inso_q, api_key, max_results=10,
                include_answer=True, search_depth="advanced", time_range="year",
            ),
            _tavily_search(
                cx, inso_q2, api_key, max_results=10,
                search_depth="advanced", time_range="year",
            ),
            _tavily_search(
                cx, inso_q3, api_key, max_results=10,
                include_domains=_INSOLVENCY_DOMAINS, search_depth="advanced", time_range="year",
            ),
        )

    comp_items = _norm_results(comp_raw)
    risk_items = _norm_results(risk_raw)
    news_items = _norm_results(news_raw)
    # Merge all insolvency passes, dedupe by URL (preserve order: targeted pass first,
    # then proceeding-markers, then the aggregator-domain recall pass).
    inso_items = _norm_results(inso_raw)
    _seen = {it.get("url") for it in inso_items}
    for it in _norm_results(inso_raw2) + _norm_results(inso_raw3):
        if it.get("url") not in _seen:
            inso_items.append(it)
            _seen.add(it.get("url"))
    if hr_no or len(_core_tokens(company_name)) <= 1:
        await _augment_insolvency_identity_text(inso_items, hr_no=hr_no)
    insolvency = _classify_insolvency(
        company_name,
        inso_raw.get("answer", "") or "",
        inso_items,
        hr_no=hr_no,
        register_court=register_court,
    )
    insolvency["source"] = "tavily"
    insolvency["confirmed"] = False

    # Amtlich tier: the official portal indexes fresh filings web search misses (and
    # avoids Tavily's twin-name false positives). An exact Handelsregister entry is a
    # unique key. An official publication => `insolvenz` (amtlich bestätigt) = True.
    # It must NEVER touch the news-derived `insolvenzverfahren` soft signal — in
    # particular, a portal with NO entry must not clear a real news "investigate"
    # flag (that would launder a false negative into "amtlich keine Insolvenz").
    if hr_no and register_court:
        try:
            from enrichment.insolvency_portal import check_insolvency_portal
            portal = await check_insolvency_portal(hr_no, register_court)
        except Exception as e:  # noqa: BLE001 — portal must not break enrichment
            print(f"[tavily] portal check failed: {e}")
            portal = {"checked": False, "note": str(e)}
        if portal.get("checked"):
            insolvency["confirmed"] = True            # portal was actually queried
            insolvency["portal_note"] = portal.get("note")
            if portal.get("found"):
                insolvency["insolvenz"] = True        # official publication exists
                insolvency["source"] = portal["source"]   # portal determined the verdict
                ann = portal.get("announcements") or []
                if ann:
                    insolvency["evidence"] = [
                        {"title": f"{a.get('date')} · {a.get('az')} · {a.get('name')} ({a.get('register')})",
                         "url": "https://www.insolvenzbekanntmachungen.de/",
                         "snippet": f"Sitz: {a.get('sitz')} · Gericht: {a.get('court')}"}
                        for a in ann[:5]
                    ]
        else:
            insolvency["portal_note"] = portal.get("note")

    risk_events = []
    for it in risk_items:
        text = f"{it.get('title','')} {it.get('snippet','')}"
        if not _same_company_entity(
            company_name, text, hr_no=hr_no, register_court=register_court,
        ):
            continue
        tag = _tag_risk(it)
        if tag:
            it["risk_tag"] = tag
            risk_events.append(it)

    news_items = [
        it for it in news_items
        if _same_company_entity(
            company_name,
            f"{it.get('title','')} {it.get('snippet','')}",
            hr_no=hr_no,
            register_court=register_court,
        )
    ]
    for it in news_items:
        it["risk_tag"] = _tag_risk(it)

    return {
        "competitor_snippets": comp_items,
        "news": news_items,
        "risk_events": risk_events,
        "insolvency": insolvency,
    }


if __name__ == "__main__":
    import sys
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    async def _main():
        name = sys.argv[1] if len(sys.argv) > 1 else "Personio SE & Co. KG"
        own = sys.argv[2] if len(sys.argv) > 2 else "personio.com"
        out = await tavily_enrich(name, own)
        print(json.dumps(out, indent=2, ensure_ascii=False))

    asyncio.run(_main())
