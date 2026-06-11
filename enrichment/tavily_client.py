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
    include_answer: bool = False,
    search_depth: str = "basic",
    time_range: Optional[str] = None,
) -> dict:
    cache_key = f"{topic}_{query}_{days}_{int(include_answer)}_{search_depth}_{time_range}"
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


def _tag_risk(item: dict) -> Optional[str]:
    blob = f"{item.get('title','')} {item.get('snippet','')}"
    for tag, rx in RISK_TAG_RX.items():
        if rx.search(blob):
            return tag
    return None


# --- Insolvency signal patterns (lead structural, gate bare "insolvent" hardest) ---

# Court file number, e.g. "2 IN 277/26" — highest-precision proceeding marker.
_COURT_AZ_RX = re.compile(r"\b\d+\s*IN\s*\d+\s*/\s*\d+\b", re.I)

# Proceeding IN PROCESS (opened / preliminary / applied for, not yet concluded).
_VERFAHREN_RX = re.compile(
    r"("
    r"vorläufige[rn]?\s+insolvenzverwalter|insolvenzverwalter\s+(wurde\s+)?bestellt|"
    r"sicherungsmaßnahmen|"
    r"insolvenzverfahren\s+(wurde\s+)?eröffnet|eröffnung\s+des\s+insolvenzverfahrens|"
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
    "ek", "ev", "und", "the", "haftungsbeschränkt", "cnc",
}
_PROX_WINDOW = 120  # chars between a company-name token and a signal to attribute it


def _core_tokens(name: str) -> List[str]:
    toks = re.split(r"[^a-z0-9]+", (name or "").lower())
    return [t for t in toks if len(t) >= 3 and t not in _LEGAL_SUFFIX]


def _token_positions(text_low: str, tokens: List[str]) -> List[int]:
    pos: List[int] = []
    for t in tokens:
        start = 0
        while (i := text_low.find(t, start)) >= 0:
            pos.append(i)
            start = i + len(t)
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


def _classify_insolvency(company_name: str, answer: str, items: List[dict]) -> dict:
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
    named_hits: List[dict] = []

    for it in items:
        text = f"{it.get('title','')} {it.get('snippet','')}"
        low = text.lower()
        name_pos = _token_positions(low, core)
        # Require the company to be clearly mentioned (>=2 distinct core tokens,
        # or the single token if the name has only one).
        distinct = sum(1 for t in core if t in low)
        if distinct < min(2, len(core)) or not name_pos:
            continue

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
        else:
            # Supplemental evidence: a result whose TITLE clearly names the company is
            # surfaced as a Beleg even when Tavily's snippet is too thin to carry a
            # signal (e.g. an insolvency-listing page that returns only nav chrome).
            # These come from the insolvency-specific queries, so they are insolvency
            # context. They enrich the evidence list ONLY — they never set the boolean.
            title_low = (it.get("title") or "").lower()
            if sum(1 for t in core if t in title_low) >= min(2, len(core)):
                named_hits.append(it)

    hit_urls = {h.get("url") for h in hits}
    ev = hits + [n for n in named_hits if n.get("url") not in hit_urls]
    return {
        "insolvenzverfahren": verfahren,
        "insolvenz": False,
        "answer": answer,
        "evidence": [
            {"title": it.get("title"), "url": it.get("url"), "snippet": it.get("snippet")}
            for it in (ev or items)[:5]
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

        comp_raw, risk_raw, news_raw, inso_raw, inso_raw2 = await asyncio.gather(
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
        )

    comp_items = _norm_results(comp_raw)
    risk_items = _norm_results(risk_raw)
    news_items = _norm_results(news_raw)
    # Merge both insolvency passes, dedupe by URL (preserve order: targeted pass first).
    inso_items = _norm_results(inso_raw)
    _seen = {it.get("url") for it in inso_items}
    for it in _norm_results(inso_raw2):
        if it.get("url") not in _seen:
            inso_items.append(it)
            _seen.add(it.get("url"))
    insolvency = _classify_insolvency(company_name, inso_raw.get("answer", "") or "", inso_items)
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
        tag = _tag_risk(it)
        if tag:
            it["risk_tag"] = tag
            risk_events.append(it)

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
