"""
Fast company extractor — no browser, no BFS crawl.

Strategy: fetch landing page HTML via httpx (~200ms), strip noise, send full
markdown to a single Gemini call with a rich schema. ~5-10s total.

For when you just need to know what a company does — not full B2B due diligence.

Public API:
    await fast_extract(url) -> dict
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from typing import List, Optional

import httpx
from openai import AsyncOpenAI  # litellm-compatible client; we route to Gemini
from urllib.parse import urlparse

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ---------- HTML → text -----------------------------------------------------

TAG_RX = re.compile(r"<[^>]+>")
SCRIPT_STYLE_RX = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>", re.S | re.I)
NAV_BLOCKS_RX = re.compile(
    r"<(nav|footer|header|aside)[^>]*>.*?</\1>", re.S | re.I
)
COOKIE_BLOCKS_RX = re.compile(
    r"<[^>]*(class|id)\s*=\s*['\"][^'\"]*(cookie|consent|gdpr|cmplz|borlabs)[^'\"]*['\"][^>]*>.*?</\w+>",
    re.S | re.I,
)
WHITESPACE_RX = re.compile(r"[ \t]+")
MULTI_NEWLINE_RX = re.compile(r"\n\s*\n\s*\n+")


def html_to_text(html: str, max_chars: int = 30000) -> str:
    """Strip HTML to readable text. Removes scripts, nav, footer, cookie blocks."""
    cleaned = SCRIPT_STYLE_RX.sub("", html)
    cleaned = NAV_BLOCKS_RX.sub("", cleaned)
    cleaned = COOKIE_BLOCKS_RX.sub("", cleaned)
    text = TAG_RX.sub("\n", cleaned)
    # HTML entities (small set, no full unescape needed)
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
                .replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"'))
    text = WHITESPACE_RX.sub(" ", text)
    text = MULTI_NEWLINE_RX.sub("\n\n", text)
    return text.strip()[:max_chars]


# ---------- Fetch -----------------------------------------------------------

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/17.0 Safari/605.1.15"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
}


class FetchResult:
    __slots__ = ("html", "status", "error_kind", "error_detail", "final_url")

    def __init__(self, html: Optional[str] = None, *, status: Optional[int] = None,
                 error_kind: Optional[str] = None, error_detail: Optional[str] = None,
                 final_url: Optional[str] = None):
        self.html = html
        self.status = status
        self.error_kind = error_kind          # 'dns', 'connect', 'timeout', 'http_status', 'non_html', 'empty'
        self.error_detail = error_detail
        self.final_url = final_url


async def fetch_html(url: str, *, follow_redirects: bool = True, timeout: float = 20.0,
                     client: Optional[httpx.AsyncClient] = None) -> FetchResult:
    cx = client or httpx.AsyncClient(headers=HEADERS, follow_redirects=follow_redirects, timeout=timeout)
    close = client is None
    try:
        r = await cx.get(url)
        if r.status_code >= 400:
            return FetchResult(status=r.status_code, error_kind="http_status",
                               error_detail=f"HTTP {r.status_code}", final_url=str(r.url))
        ctype = r.headers.get("content-type", "").lower()
        if "html" not in ctype:
            return FetchResult(status=r.status_code, error_kind="non_html",
                               error_detail=f"content-type: {ctype or 'none'}",
                               final_url=str(r.url))
        if not r.text or len(r.text.strip()) < 50:
            return FetchResult(status=r.status_code, error_kind="empty",
                               error_detail=f"body {len(r.text)} chars", final_url=str(r.url))
        return FetchResult(html=r.text, status=r.status_code, final_url=str(r.url))
    except httpx.ConnectError as e:
        return FetchResult(error_kind="dns", error_detail=str(e))
    except httpx.ReadTimeout as e:
        return FetchResult(error_kind="timeout", error_detail=str(e))
    except httpx.HTTPError as e:
        return FetchResult(error_kind="connect", error_detail=str(e))
    finally:
        if close:
            await cx.aclose()


# httpx errors that usually mean a WAF/bot-block (e.g. Cloudflare 403) rather
# than a genuinely dead site — worth retrying with a real browser.
_WAF_BLOCK_KINDS = {"http_status", "connect", "timeout", "non_html"}


async def browser_fetch(url: str) -> Optional[str]:
    """Fallback fetch via a real Chromium (crawl4ai). Handles two cases plain
    httpx cannot: (1) WAF/Cloudflare blocks (real TLS fingerprint + JS challenge)
    and (2) JS-rendered SPAs that ship an empty HTML shell (waits for network
    idle + lets late content render). Returns HTML or None."""
    try:
        from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig
    except ImportError as e:
        print(f"[fast] browser fallback unavailable: {e}")
        return None
    try:
        cfg = CrawlerRunConfig(
            wait_until="networkidle",
            page_timeout=20000,
            delay_before_return_html=2.5,
            scan_full_page=True,
        )
        async with AsyncWebCrawler(config=BrowserConfig(headless=True)) as c:
            r = await c.arun(url=url, config=cfg)
            if getattr(r, "success", False) and r.html and len(r.html.strip()) > 200:
                return r.html
    except Exception as e:  # noqa: BLE001 — fallback must never crash the request
        print(f"[fast] browser fallback failed: {e}")
    return None


IMPRESSUM_PATHS = [
    "/impressum", "/impressum/",
    "/imprint", "/imprint/",
    "/legal-notice", "/legal-notice/",
    "/legal", "/legal/",
    "/de/impressum", "/de/impressum/",
    "/en/imprint", "/en/imprint/",
]

IMPRESSUM_LINK_RX = re.compile(
    r'<a[^>]+href=[\'"]([^\'"]*(?:impressum|imprint|legal[-_ ]?notice|mentions[-_ ]?legales)[^\'"]*)[\'"]',
    re.I,
)


def _resolve(base_url: str, link: str) -> str:
    """Resolve relative link to absolute."""
    if link.startswith("http://") or link.startswith("https://"):
        return link
    p = urlparse(base_url)
    if link.startswith("//"):
        return f"{p.scheme}:{link}"
    if link.startswith("/"):
        return f"{p.scheme}://{p.netloc}{link}"
    return f"{p.scheme}://{p.netloc}/{link.lstrip('./')}"


async def find_impressum_url(start_url: str, home_html: Optional[str],
                              client: httpx.AsyncClient) -> Optional[str]:
    """Find Impressum URL: scan home page links first, then probe common paths."""
    p = urlparse(start_url)
    base = f"{p.scheme}://{p.netloc}"

    if home_html:
        for m in IMPRESSUM_LINK_RX.finditer(home_html):
            return _resolve(start_url, m.group(1))

    for path in IMPRESSUM_PATHS:
        candidate = base + path
        try:
            r = await client.head(candidate, follow_redirects=True, timeout=8.0)
            if r.status_code == 200 and "html" in r.headers.get("content-type", "").lower():
                return str(r.url)
        except httpx.HTTPError:
            continue
    return None


USER_FRIENDLY_MESSAGE = (
    "Leider ist keine Analyse möglich, da die Firmenwebseite nicht erreichbar ist "
    "oder nicht genügend relevante Informationen liefert."
)


# ---------- LLM extraction --------------------------------------------------

EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "Company name with legal form (GmbH/AG/SE/KG if visible)."},
        "tagline": {"type": "string", "description": "Marketing tagline if any."},
        "elevator_pitch": {
            "type": "string",
            "description": "ONE sentence describing what the company does. Concrete. No PR adjectives.",
        },
        "what_they_do": {
            "type": "string",
            "description": (
                "4-7 sentences explaining the offering in detail. Cover: "
                "what products/services they sell, who their customers are (B2B/B2C, "
                "industries, company sizes), how they make money (project / subscription / "
                "transactional / hardware sale), what makes them different. "
                "Stay strictly factual. Quote claims from the page, don't invent."
            ),
        },
        "core_products_services": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string", "description": "1-2 sentences explaining the offering."},
                    "category": {"type": "string"},
                },
                "required": ["name"],
            },
            "description": "Every distinct product / service / module / feature listed on the page.",
        },
        "target_customers": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Concrete customer segments mentioned (e.g. 'B2B Großhändler', 'Handwerksbetriebe', 'KMU 10-2000 Mitarbeiter').",
        },
        "business_model": {"type": "string", "description": "How they earn revenue. One short phrase."},
        "industry": {"type": "string", "description": "Concrete sector (not abstract — e.g. 'CNC machining for automotive')."},
        "headquarters": {"type": "string"},
        "founded": {"type": "string", "description": "Year if stated."},
        "employee_count": {"type": "string"},
        "languages": {"type": "array", "items": {"type": "string"}},
        "key_claims": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Concrete claims made on the page (e.g. 'Echtzeit-Bonitätsprüfung in unter 2 Sekunden'). Quote, don't paraphrase loosely.",
        },
    },
    "required": ["name", "elevator_pitch", "what_they_do", "core_products_services"],
}

SYSTEM_PROMPT = (
    "Du bist ein präziser Recherche-Assistent. Aus dem gegebenen Webseiten-Text "
    "extrahierst du strukturierte Fakten über ein Unternehmen. "
    "STRIKTE REGELN:\n"
    "1. Erfinde NICHTS. Wenn ein Feld nicht eindeutig im Text steht, lass es leer/null.\n"
    "2. Keine PR-Adjektive (innovativ, hochwertig, marktführend, einzigartig).\n"
    "3. Zitiere konkrete Behauptungen statt sie zu paraphrasieren.\n"
    "4. 'what_they_do' soll dicht und sachlich sein, 4-7 Sätze.\n"
    "5. 'elevator_pitch' EIN Satz ohne Werbesprache.\n"
    "6. Sprache der Felder: nutze die Sprache der Webseite (deutsch wenn deutsch).\n"
    "7. 'core_products_services': liste ALLE Produkte/Leistungen in der Reihenfolge "
    "ihres Auftretens auf der Seite. Sortiere NICHT um und lass nichts weg. Bei "
    "wiederholter Anfrage muss dieselbe Liste in derselben Reihenfolge entstehen.\n"
    "Gib NUR gültiges JSON zurück, ohne Markdown-Code-Block, ohne Erklärung."
)


async def llm_extract(page_text: str, source_url: str) -> Optional[dict]:
    """Single Gemini call, schema-constrained."""
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not set")

    # litellm endpoint for Gemini, OpenAI-compatible interface
    client = AsyncOpenAI(
        api_key=api_key,
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
    )

    user_msg = (
        f"Webseiten-URL: {source_url}\n\n"
        f"--- WEBSEITEN-TEXT ---\n{page_text}\n--- ENDE TEXT ---\n\n"
        f"Extrahiere die Fakten gemäß diesem JSON-Schema:\n"
        f"{json.dumps(EXTRACTION_SCHEMA, ensure_ascii=False)}\n\n"
        "Antworte mit gültigem JSON. Kein Vorwort. Kein Markdown-Codeblock."
    )

    model_name = os.getenv("FAST_MODEL", "gemini-2.5-flash-lite")
    resp = await client.chat.completions.create(
        model=model_name,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        temperature=0.0,
        top_p=0.0,
        response_format={"type": "json_object"},
        max_tokens=8000,
        extra_body={"reasoning_effort": "none"},
    )
    content = resp.choices[0].message.content or ""
    # Strip code fences if model added them despite instructions
    content = re.sub(r"^```(?:json)?\s*", "", content.strip())
    content = re.sub(r"\s*```$", "", content)
    try:
        return json.loads(content)
    except json.JSONDecodeError as e:
        print(f"[fast] JSON parse failed: {e}\n{content[:500]}")
        return None


# ---------- Pipeline --------------------------------------------------------


IMPRESSUM_SCHEMA = {
    "type": "object",
    "properties": {
        "company_name": {"type": "string", "description": "Firma incl. legal form (GmbH, AG, SE & Co. KG, ...)."},
        "street": {"type": "string"},
        "postal_code": {"type": "string"},
        "city": {"type": "string"},
        "country": {"type": "string"},
        "represented_by": {"type": "array", "items": {"type": "string"}, "description": "Vertretungsberechtigte (Geschäftsführer, Vorstand)."},
        "phone": {"type": "string"},
        "email": {"type": "string"},
        "website": {"type": "string"},
        "register_court": {"type": "string", "description": "Registergericht."},
        "register_number": {"type": "string", "description": "HRB / HRA number."},
        "vat_id": {"type": "string", "description": "USt-IdNr. (DE...)."},
        "tax_number": {"type": "string", "description": "Steuernummer if stated."},
        "responsible_for_content": {"type": "string", "description": "V.i.S.d.P. / inhaltlich Verantwortlicher."},
        "supervisory_authority": {"type": "string", "description": "Aufsichtsbehörde if listed."},
    },
}

IMPRESSUM_SYSTEM = (
    "Du extrahierst Felder aus einem deutschen Impressum / Legal Notice. "
    "STRIKT: nur Felder ausfüllen, die wörtlich im Text stehen. NICHTS erfinden. "
    "Keine Annahmen, keine Schätzungen. Wenn ein Feld fehlt, lass es null. "
    "Antworte mit gültigem JSON. Kein Markdown-Codeblock."
)


_IMPRESSUM_HR_RX = re.compile(r"\b(HRA|HRB|GnR|GsR|PR|VR)\s*\.?\s*(\d+)\b", re.I)
_IMPRESSUM_COURT_RX = re.compile(
    r"\b(?:amtsgericht|registergericht)\s*[:\-]?\s*([A-ZÄÖÜ][A-Za-zÄÖÜäöüß .\-]{1,50})",
    re.I,
)


def _extract_register_from_impressum_text(text: str) -> dict:
    """Deterministic fallback for HRB/HRA + Amtsgericht from Impressum text."""
    out: dict = {}
    if not text:
        return out
    m_hr = _IMPRESSUM_HR_RX.search(text)
    if m_hr:
        out["register_number"] = f"{m_hr.group(1).upper()} {m_hr.group(2)}"
    for line in text.splitlines():
        m_court = _IMPRESSUM_COURT_RX.search(line)
        if not m_court:
            continue
        court = re.split(r"[,;|/]", m_court.group(1), maxsplit=1)[0].strip(" .:-")
        if court:
            out["register_court"] = court
            break
    if "register_court" not in out:
        m_court = _IMPRESSUM_COURT_RX.search(text[:4000])
        if m_court:
            court = re.split(r"[,;|/\n\r]", m_court.group(1), maxsplit=1)[0].strip(" .:-")
            if court:
                out["register_court"] = court
    return out


def _merge_register_fallback(impressum_data: Optional[dict], impressum_text: str) -> Optional[dict]:
    detected = _extract_register_from_impressum_text(impressum_text)
    if not detected:
        return impressum_data
    imp = dict(impressum_data or {})
    for key in ("register_number", "register_court"):
        if not (imp.get(key) or "").strip() and detected.get(key):
            imp[key] = detected[key]
    return imp


def _effective_register_input(
    hr_no: Optional[str],
    register_court: Optional[str],
    impressum_data: Optional[dict],
    *,
    prefer_impressum: bool = False,
) -> dict:
    imp = impressum_data or {}
    user_hr = (hr_no or "").strip()
    user_court = (register_court or "").strip()
    imp_hr = (imp.get("register_number") or "").strip()
    imp_court = (imp.get("register_court") or "").strip()
    if prefer_impressum:
        eff_hr = imp_hr or user_hr
        eff_court = imp_court or user_court
    else:
        eff_hr = user_hr or imp_hr
        eff_court = user_court or imp_court
    return {"hr_no": eff_hr or None, "register_court": eff_court or None}


async def llm_extract_impressum(impressum_text: str, source_url: str) -> Optional[dict]:
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        return None
    client = AsyncOpenAI(
        api_key=api_key,
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
    )
    user_msg = (
        f"Quelle: {source_url}\n\n--- IMPRESSUM-TEXT ---\n{impressum_text}\n--- ENDE ---\n\n"
        f"Schema:\n{json.dumps(IMPRESSUM_SCHEMA, ensure_ascii=False)}\n\nAntworte mit JSON."
    )
    resp = await client.chat.completions.create(
        model=os.getenv("FAST_MODEL", "gemini-2.5-flash-lite"),
        messages=[
            {"role": "system", "content": IMPRESSUM_SYSTEM},
            {"role": "user", "content": user_msg},
        ],
        temperature=0.0,
        top_p=0.0,
        response_format={"type": "json_object"},
        max_tokens=2000,
        extra_body={"reasoning_effort": "none"},
    )
    content = (resp.choices[0].message.content or "").strip()
    content = re.sub(r"^```(?:json)?\s*", "", content)
    content = re.sub(r"\s*```$", "", content)
    try:
        return json.loads(content)
    except json.JSONDecodeError as e:
        print(f"[fast] impressum JSON parse failed: {e}")
        return None


def _to_report_shape(fast_result: dict) -> dict:
    """Convert fast_extract output into the CompanyReport shape expected by synthesise_profile."""
    e = fast_result.get("extracted") or {}
    imp = (fast_result.get("impressum") or {}).get("data")

    # Build summary block in report shape: {url, data:[{name,...}]}
    summary_data = {
        "name": e.get("name"),
        "tagline": e.get("tagline"),
        "summary": e.get("what_they_do") or e.get("elevator_pitch"),
        "industry": e.get("industry"),
        "founded": e.get("founded"),
        "headquarters": e.get("headquarters"),
        "employee_count": e.get("employee_count"),
        "website": fast_result.get("source_url"),
        "languages": e.get("languages"),
        "elevator_pitch": e.get("elevator_pitch"),
        "target_customers": e.get("target_customers"),
        "business_model": e.get("business_model"),
        "key_claims": e.get("key_claims"),
    }

    # Build catalog block
    items = []
    for it in e.get("core_products_services") or []:
        items.append({
            "name": it.get("name"),
            "type": "service",
            "description": it.get("description"),
            "category": it.get("category"),
            "url": None,
        })
    products_services = []
    if items:
        products_services.append({
            "url": fast_result.get("source_url"),
            "data": {"items": items},
        })

    return {
        "source_url": fast_result.get("source_url"),
        "summary": {"url": fast_result.get("source_url"), "data": [summary_data]},
        "products_services": products_services,
        "governance": None,
        "impressum": {"url": (fast_result.get("impressum") or {}).get("url"),
                      "data": [imp] if imp else None} if imp else None,
        "subpages": [],
    }


async def _enrich_only(company_name: str, *, hr_no: Optional[str] = None,
                       register_court: Optional[str] = None,
                       with_enrichment: bool = True) -> dict:
    """Name-only path (no website supplied): run Tavily (incl. insolvency) + sanctions.

    Branch + profile are website-derived and intentionally skipped here. The result
    keeps the same top-level shape as fast_extract so build_text_blocks and the UI
    renderers never KeyError on a missing key.
    """
    import time
    t0 = time.time()
    result = {
        "source_url": "",
        "register_input": {"hr_no": hr_no, "register_court": register_court}
                          if (hr_no or register_court) else None,
        "extracted": {"name": company_name},
        "impressum": None,
        "enrichment": {"tavily": {"competitor_snippets": [], "news": [], "risk_events": [],
                                  "insolvency": {"insolvenzverfahren": False, "insolvenz": False,
                                                 "confirmed": False, "source": "tavily",
                                                 "answer": "", "evidence": []}},
                       "sanctions": [], "wikidata": []},
        "profile": None,
        "metrics": {"name_only": True},
    }
    if with_enrichment:
        try:
            from enrichment.tavily_client import tavily_enrich
            from enrichment.sanctions import check_sanctions
            tv_task = asyncio.create_task(
                tavily_enrich(company_name, hr_no=hr_no, register_court=register_court))
            sn_task = asyncio.create_task(check_sanctions(company_name, []))
            result["enrichment"]["tavily"] = await tv_task
            result["enrichment"]["sanctions"] = await sn_task
        except ImportError as e:
            print(f"[name-only] enrichment unavailable: {e}")
        except Exception as e:  # noqa: BLE001
            print(f"[name-only] enrichment failed: {e}")
    result["metrics"]["total_ms"] = round((time.time() - t0) * 1000)
    return result


async def fast_extract(url: str, *, with_profile: bool = True,
                       with_enrichment: bool = True,
                       with_branch: bool = True,
                       hr_no: Optional[str] = None,
                       register_court: Optional[str] = None,
                       company_name: Optional[str] = None) -> dict:
    """Fetch landing page + Impressum + LLM extract. ~5-15s. No browser.

    Args:
        with_profile: also run synthesise_profile (4-block German B2B profile) via gpt-4o.
        with_enrichment: run Tavily competitor/news + OpenSanctions check in parallel.
        with_branch: classify into SectorBench branch + outlook + impact-on-company.
        hr_no: Handelsregister number (e.g. 'HRA 12345'); sharpens insolvency search.
        register_court: Registergericht / register city (e.g. 'Stuttgart'); sharpens insolvency search.
        company_name: authoritative company name; overrides the name derived from the
            site/Impressum for all enrichment (Tavily, sanctions, branch).
    """
    import time
    t0 = time.time()
    name_override = (company_name or "").strip() or None
    identity_match = None

    # No website supplied → resolve what to do from the available identifiers.
    # Priority: URL (handled below) > Handelsregister-Nr. (resolve name) > name.
    if not (url or "").strip():
        has_hr = bool((hr_no or "").strip())
        if not name_override and not has_hr:
            return {
                "source_url": "",
                "error": "Provide a company website URL, a Handelsregister number, or a company name.",
                "error_kind": "no_input",
                "metrics": {"total_ms": round((time.time() - t0) * 1000)},
            }

        # No website → no crawl. Run insolvency + enrichment on the company. If only an
        # HR number was supplied, resolve a name from the register so the search has a
        # subject (HR/court still sharpen the insolvency query downstream).
        name_for_search = name_override
        if not name_for_search and has_hr:
            from enrichment.company_finder import _resolve_name
            name_for_search = await _resolve_name(hr_no, register_court)
        if not name_for_search:
            return {
                "source_url": "",
                "error": ("Could not resolve a company from the supplied Handelsregister "
                          "number / register court. Add a company name or website."),
                "error_kind": "unresolved",
                "register_input": {"hr_no": hr_no, "register_court": register_court},
                "metrics": {"total_ms": round((time.time() - t0) * 1000)},
            }
        return await _enrich_only(
            name_for_search, hr_no=hr_no, register_court=register_court,
            with_enrichment=with_enrichment,
        )

    async with httpx.AsyncClient(
        headers=HEADERS, follow_redirects=True, timeout=20.0,
    ) as cx:
        home = await fetch_html(url, client=cx)
        home_html = home.html
        used_browser = False
        # WAF/Cloudflare block (e.g. 403 from a datacenter IP) → retry via real browser.
        if (home.error_kind in _WAF_BLOCK_KINDS) or not home_html:
            print(f"[fast] httpx fetch failed ({home.error_kind}); trying browser fallback")
            fallback_html = await browser_fetch(url)
            used_browser = True
            if fallback_html:
                home_html = fallback_html
            else:
                return {
                    "source_url": url,
                    "final_url": home.final_url,
                    "error": USER_FRIENDLY_MESSAGE,
                    "error_kind": home.error_kind or "empty",
                    "error_detail": home.error_detail,
                    "http_status": home.status,
                    "metrics": {"fetch_ms": round((time.time() - t0) * 1000)},
                }

        # Sanity: cleaned text must have substance. A 200 with near-empty text is
        # usually a JS-rendered SPA shipping an empty shell → render with browser.
        text = html_to_text(home_html)
        if len(text) < 200 and not used_browser:
            print(f"[fast] thin text ({len(text)} chars); trying browser fallback (likely JS SPA)")
            fallback_html = await browser_fetch(url)
            if fallback_html:
                home_html = fallback_html
                text = html_to_text(home_html)

        if len(text) < 200:
            return {
                "source_url": url,
                "final_url": home.final_url,
                "error": USER_FRIENDLY_MESSAGE,
                "error_kind": "no_content",
                "error_detail": f"cleaned text {len(text)} chars",
                "http_status": home.status,
                "metrics": {
                    "fetch_ms": round((time.time() - t0) * 1000),
                    "home_html_bytes": len(home_html),
                    "home_text_chars": len(text),
                },
            }

        # Resolve Impressum URL + fetch it in parallel with home-page extract
        imp_url_task = asyncio.create_task(find_impressum_url(url, home_html, cx))

        home_llm_task = asyncio.create_task(llm_extract(text, url))

        imp_url = await imp_url_task
        imp_html = None
        if imp_url:
            imp = await fetch_html(imp_url, client=cx)
            imp_html = imp.html if not imp.error_kind else None

    fetch_ms = (time.time() - t0) * 1000

    # Home extract
    t_llm = time.time()
    data = await home_llm_task
    home_llm_ms = (time.time() - t_llm) * 1000

    # Impressum extract (only if HTML found)
    impressum_data: Optional[dict] = None
    imp_llm_ms = 0
    if imp_html:
        imp_text = html_to_text(imp_html, max_chars=15000)
        t_imp = time.time()
        impressum_data = await llm_extract_impressum(imp_text, imp_url or url)
        impressum_data = _merge_register_fallback(impressum_data, imp_text)
        imp_llm_ms = (time.time() - t_imp) * 1000

    # Fact-check supplied Handelsregister data against the crawled Impressum.
    # URL-backed analysis should not fail solely because caller-supplied register
    # fields differ from the site's Impressum; keep the diagnostic and trust the
    # Impressum register for downstream enrichment instead.
    if identity_match is None and ((hr_no or "").strip() or (register_court or "").strip()):
        from enrichment.company_finder import _verify
        identity_match = _verify(hr_no, register_court, impressum_data)

    effective_register = _effective_register_input(
        hr_no,
        register_court,
        impressum_data,
        prefer_impressum=(identity_match or {}).get("verified") is False,
    )
    effective_hr_no = effective_register.get("hr_no")
    effective_register_court = effective_register.get("register_court")

    extract_total_ms = (time.time() - t0) * 1000

    result = {
        "source_url": url,
        "register_input": effective_register if (effective_hr_no or effective_register_court) else None,
        "extracted": data or {},
        "impressum": {
            "url": imp_url,
            "data": impressum_data,
        } if imp_url else None,
        "enrichment": {"tavily": {"competitor_snippets": [], "news": [], "risk_events": [],
                                  "insolvency": {"insolvenzverfahren": False, "insolvenz": False,
                                                 "confirmed": False, "source": "tavily",
                                                 "answer": "", "evidence": []}},
                       "sanctions": [], "wikidata": []},
        "profile": None,
        "metrics": {
            "home_html_bytes": len(home_html),
            "home_text_chars": len(text),
            "impressum_found": bool(imp_url),
            "fetch_ms": round(fetch_ms),
            "home_llm_ms": round(home_llm_ms),
            "impressum_llm_ms": round(imp_llm_ms),
            "extract_total_ms": round(extract_total_ms),
        },
    }

    # ---- Enrichment + synthesis (optional) ----
    if with_enrichment or with_profile:
        company_name = name_override or (impressum_data or {}).get("company_name") or (data or {}).get("name") or url
        persons = (impressum_data or {}).get("represented_by") or []
        if isinstance(persons, str):
            persons = [persons]

        enrich_t0 = time.time()

        # Run Tavily + sanctions in parallel
        tavily_task = None
        sanctions_task = None
        if with_enrichment:
            try:
                from enrichment.tavily_client import tavily_enrich
                from enrichment.sanctions import check_sanctions
                tavily_task = asyncio.create_task(
                    tavily_enrich(company_name, own_domain=url,
                                  hr_no=effective_hr_no,
                                  register_court=effective_register_court)
                )
                sanctions_task = asyncio.create_task(check_sanctions(company_name, persons))
            except ImportError as e:
                print(f"[fast] enrichment unavailable: {e}")

        if tavily_task:
            try:
                result["enrichment"]["tavily"] = await tavily_task
            except Exception as e:  # noqa: BLE001
                print(f"[fast] tavily failed: {e}")
        if sanctions_task:
            try:
                result["enrichment"]["sanctions"] = await sanctions_task
            except Exception as e:  # noqa: BLE001
                print(f"[fast] sanctions failed: {e}")

        result["metrics"]["enrichment_ms"] = round((time.time() - enrich_t0) * 1000)

        if with_profile:
            try:
                from synthesis.profile_generator import synthesize_profile
                report_shape = _to_report_shape(result)
                report_shape["enrichment"] = result["enrichment"]
                profile_t0 = time.time()
                result["profile"] = await synthesize_profile(report_shape)
                result["metrics"]["profile_ms"] = round((time.time() - profile_t0) * 1000)
            except Exception as e:  # noqa: BLE001
                print(f"[fast] synthesis failed: {e}")
                result["profile"] = {"error": str(e)}

    # ---- Branch classification + outlook ----
    if with_branch:
        try:
            from synthesis.branch_generator import analyze_branch
            branch_t0 = time.time()
            company_name = name_override or ((impressum_data or {}).get("company_name")) or (data or {}).get("name") or ""
            result["branch"] = await analyze_branch(data or {}, company_name=company_name)
            result["metrics"]["branch_ms"] = round((time.time() - branch_t0) * 1000)
        except Exception as e:  # noqa: BLE001
            print(f"[fast] branch analysis failed: {e}")
            result["branch"] = {"error": str(e)}

    if identity_match is not None:
        result["identity_match"] = identity_match

    result["metrics"]["total_ms"] = round((time.time() - t0) * 1000)
    return result


if __name__ == "__main__":
    import sys

    async def _main():
        url = sys.argv[1] if len(sys.argv) > 1 else "https://www.boniforce.de"
        out = await fast_extract(url)
        print(json.dumps(out, indent=2, ensure_ascii=False))

    asyncio.run(_main())
