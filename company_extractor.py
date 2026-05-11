"""
Company website extractor using crawl4ai.

Pipeline:
  1. Deep-crawl the start URL to discover Impressum, About, Products/Services,
     and Governance/Leadership pages (same domain).
  2. LLM-extract structured data per section against pydantic schemas.
  3. Aggregate into a single structured JSON report.

Output sections:
  summary           - general company overview
  products_services - product/service catalog
  governance        - leadership, board, ownership
  impressum         - German legal notice / contact (most important)
  subpages          - all discovered URLs with titles

Install:
  pip install -U crawl4ai pydantic
  crawl4ai-setup

Run:
  export OPENAI_API_KEY=sk-...
  python company_extractor.py https://www.example-gmbh.de --out report.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from typing import List, Optional
from urllib.parse import urlparse

from pydantic import BaseModel, Field

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from crawl4ai import (
    AsyncWebCrawler,
    BrowserConfig,
    CacheMode,
    CrawlerRunConfig,
    LLMConfig,
)
from crawl4ai.deep_crawling import BFSDeepCrawlStrategy
from crawl4ai.deep_crawling.filters import DomainFilter, FilterChain
from crawl4ai.extraction_strategy import LLMExtractionStrategy

# ---------- Schemas ---------------------------------------------------------


class CompanyOverview(BaseModel):
    name: Optional[str] = Field(None, description="Official company name including legal form")
    tagline: Optional[str] = Field(None, description="Company tagline or slogan")
    summary: Optional[str] = Field(None, description="2-4 sentence summary of what the company does")
    industry: Optional[str] = None
    founded: Optional[str] = Field(None, description="Year founded if stated")
    headquarters: Optional[str] = Field(None, description="HQ city/country")
    employee_count: Optional[str] = Field(None, description="Employee count or range if stated")
    website: Optional[str] = None
    languages: Optional[List[str]] = Field(None, description="Languages the site is offered in")


class Person(BaseModel):
    name: str
    role: Optional[str] = Field(None, description="Title/role e.g. CEO, Geschäftsführer, Board member")


class Governance(BaseModel):
    executives: Optional[List[Person]] = Field(None, description="Management team / Geschäftsführung")
    board_members: Optional[List[Person]] = Field(None, description="Board of directors / Aufsichtsrat")
    ownership: Optional[str] = Field(None, description="Ownership structure if mentioned")
    parent_company: Optional[str] = None


class Impressum(BaseModel):
    company_name: Optional[str] = Field(None, description="Firma incl. legal form (GmbH, AG, ...)")
    street: Optional[str] = None
    postal_code: Optional[str] = None
    city: Optional[str] = None
    country: Optional[str] = None
    represented_by: Optional[List[str]] = Field(None, description="Vertretungsberechtigte (Geschäftsführer)")
    phone: Optional[str] = None
    email: Optional[str] = None
    website: Optional[str] = None
    register_court: Optional[str] = Field(None, description="Registergericht")
    register_number: Optional[str] = Field(None, description="HRB / HRA Nummer")
    vat_id: Optional[str] = Field(None, description="USt-IdNr.")
    tax_number: Optional[str] = Field(None, description="Steuernummer")
    responsible_for_content: Optional[str] = Field(None, description="V.i.S.d.P. / inhaltlich Verantwortlicher")


class ProductOrService(BaseModel):
    name: str
    type: str = Field(..., description="'product' or 'service'")
    description: Optional[str] = None
    category: Optional[str] = None
    url: Optional[str] = None


class Catalog(BaseModel):
    items: List[ProductOrService]


# ---------- Discovery -------------------------------------------------------

IMPRESSUM_HINTS = re.compile(r"(impressum|imprint|legal[-_ ]?notice|mentions[-_ ]?legales)", re.I)
ABOUT_HINTS = re.compile(r"(about|ueber[-_ ]?uns|über[-_ ]?uns|unternehmen|company|who[-_ ]?we[-_ ]?are)", re.I)
PRODUCT_HINTS = re.compile(
    r"(produkt|product|leistung|service|solution|angebot|portfolio|loesung|lösung|"
    r"feature|pricing|preise|plan(s)?|api|use[-_ ]?case|anwendung|"
    r"platform|plattform|tool(s)?|software|module|funktion|integration|shop)",
    re.I,
)
GOVERNANCE_HINTS = re.compile(
    r"(/(our[-_ ]?team|leadership|management[-_ ]?team|executives?|leaders?|"
    r"geschaeftsfuehrung|geschäftsführung|fuehrungsteam|führungsteam|"
    r"board|vorstand|aufsichtsrat|investor[-_ ]?relations))",
    re.I,
)
SKIP_HINTS = re.compile(r"(privacy|datenschutz|cookie|agb|terms|jobs?|career|karriere|blog|news|press)", re.I)


def _domain(url: str) -> str:
    return urlparse(url).netloc.lower()


async def discover_pages(start_url: str, max_pages: int = 60, max_depth: int = 2) -> dict:
    """BFS-crawl on same domain, classify URLs by hint regex."""
    domain = _domain(start_url)

    cfg = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        deep_crawl_strategy=BFSDeepCrawlStrategy(
            max_depth=max_depth,
            max_pages=max_pages,
            filter_chain=FilterChain([DomainFilter(allowed_domains=[domain])]),
        ),
        stream=False,
    )
    async with AsyncWebCrawler(config=BrowserConfig(headless=True)) as crawler:
        results = await crawler.arun(url=start_url, config=cfg)

    buckets = {"impressum": [], "about": [], "products": [], "governance": [], "other": []}
    subpages = []

    for r in results:
        if not r.success:
            continue
        url = r.url
        title = (r.metadata or {}).get("title") if hasattr(r, "metadata") else None
        subpages.append({"url": url, "title": title})

        if SKIP_HINTS.search(url):
            continue
        if IMPRESSUM_HINTS.search(url):
            buckets["impressum"].append(url)
        elif GOVERNANCE_HINTS.search(url):
            buckets["governance"].append(url)
        elif ABOUT_HINTS.search(url):
            buckets["about"].append(url)
        elif PRODUCT_HINTS.search(url):
            buckets["products"].append(url)
        else:
            buckets["other"].append(url)

    buckets["subpages"] = subpages
    return buckets


# ---------- LLM extraction --------------------------------------------------


def _llm_strategy(schema_model: type[BaseModel], instruction: str) -> LLMExtractionStrategy:
    provider = os.getenv("LLM_PROVIDER", "gemini/gemini-2.5-flash")
    # Pick the right API token per provider prefix.
    if provider.startswith("gemini/") or provider.startswith("google/"):
        api_token = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or os.getenv("LLM_API_KEY")
    elif provider.startswith("anthropic/"):
        api_token = os.getenv("ANTHROPIC_API_KEY") or os.getenv("LLM_API_KEY")
    else:
        api_token = os.getenv("OPENAI_API_KEY") or os.getenv("LLM_API_KEY")
    instruction_with_noise_filter = (
        instruction
        + "\n\nIGNORE: cookie-consent banners, privacy/DSGVO blurbs, navigation menus, "
        "footer links, language switchers, social-media share buttons, "
        "newsletter signup forms. Focus only on substantive content describing what "
        "the company sells or what the page is about."
    )
    return LLMExtractionStrategy(
        llm_config=LLMConfig(provider=provider, api_token=api_token),
        schema=schema_model.model_json_schema(),
        extraction_type="schema",
        instruction=instruction_with_noise_filter,
        # plain markdown: fit_markdown can return empty on JS-heavy/SPA pages
        input_format="markdown",
        apply_chunking=True,
        chunk_token_threshold=8000,
    )


# JS that tries to dismiss common cookie-consent banners before extraction.
# Best-effort: clicks any element matching common "accept"/"alle akzeptieren" patterns.
COOKIE_DISMISS_JS = r"""
(() => {
  const SELECTORS = [
    'button[id*="accept" i]',
    'button[class*="accept" i]',
    'button[id*="zustimmen" i]',
    'button[class*="zustimmen" i]',
    'button[id*="akzeptier" i]',
    'button[class*="akzeptier" i]',
    'a[id*="accept" i]',
    '[aria-label*="Accept" i]',
    '[aria-label*="akzeptier" i]',
    '#onetrust-accept-btn-handler',
    '.cmplz-accept',
    '.cc-allow',
    '.cookie-accept',
  ];
  for (const sel of SELECTORS) {
    document.querySelectorAll(sel).forEach(el => {
      try { el.click(); } catch (e) {}
    });
  }
})();
"""


async def _extract_one(crawler: AsyncWebCrawler, url: str, schema: type[BaseModel], instruction: str) -> Optional[dict]:
    cfg = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        extraction_strategy=_llm_strategy(schema, instruction),
        js_code=[COOKIE_DISMISS_JS],
        delay_before_return_html=1.5,  # allow cookie dismissal + lazy content to settle
    )
    r = await crawler.arun(url=url, config=cfg)
    if not r.success or not r.extracted_content:
        return None
    try:
        data = json.loads(r.extracted_content) if isinstance(r.extracted_content, str) else r.extracted_content
    except json.JSONDecodeError:
        data = r.extracted_content
    return {"url": url, "data": data}


async def extract_overview(crawler: AsyncWebCrawler, urls: List[str]) -> Optional[dict]:
    instruction = (
        "Extract a concrete company overview. "
        "Fields:\n"
        "- name: full legal name (include GmbH/AG/SE/KG if visible).\n"
        "- tagline: company tagline or marketing claim, in original language.\n"
        "- summary: 3-5 dense sentences. Cover: what they actually sell or build, who buys it, "
        "  how they make money (project work, monthly subscription, license, transactions, hardware sale), "
        "  geographic focus, and any standout differentiator. "
        "  Avoid PR adjectives. Avoid 'innovative', 'leading', 'cutting-edge'.\n"
        "- industry: concrete sector (e.g. 'CNC machining for automotive', not 'manufacturing').\n"
        "- founded: 4-digit year if stated.\n"
        "- headquarters: city, country.\n"
        "- employee_count: number or range if stated.\n"
        "- website: primary domain.\n"
        "- languages: ISO codes for languages the site is offered in (e.g. ['de', 'en']).\n"
        "Return null for any field not directly stated. Do not invent."
    )
    for u in urls:
        out = await _extract_one(crawler, u, CompanyOverview, instruction)
        if out and out["data"]:
            return out
    return None


async def extract_governance(crawler: AsyncWebCrawler, urls: List[str]) -> Optional[dict]:
    instruction = (
        "Extract company governance: executives/management team (name + role), board members, "
        "ownership structure, and parent company if any. Use null for unknown fields. "
        "Do not include random employees, only leadership."
    )
    for u in urls:
        out = await _extract_one(crawler, u, Governance, instruction)
        if out and out["data"]:
            return out
    return None


async def extract_impressum(crawler: AsyncWebCrawler, urls: List[str]) -> Optional[dict]:
    instruction = (
        "Extract the German Impressum / legal notice fields exactly as printed. "
        "Leave fields null if not present. Do NOT invent or guess data."
    )
    for u in urls:
        out = await _extract_one(crawler, u, Impressum, instruction)
        if out and out["data"]:
            return out
    return None


def _normalize_catalog(data: Any) -> Optional[dict]:
    """crawl4ai returns LLM extract in two shapes — normalise to {items: [...]}."""
    if not data:
        return None
    # Already wrapped
    if isinstance(data, dict) and "items" in data:
        items = data["items"]
        return {"items": items} if items else None
    # Single wrapper as list of one dict containing items
    if isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict) and "items" in data[0]:
        items = data[0]["items"]
        return {"items": items} if items else None
    # Flat list of items
    if isinstance(data, list) and data and isinstance(data[0], dict) and "name" in data[0]:
        # Strip crawl4ai artefacts
        cleaned = [{k: v for k, v in it.items() if k != "error"} for it in data]
        return {"items": cleaned}
    return None


async def extract_catalog(crawler: AsyncWebCrawler, urls: List[str], limit: int = 8) -> List[dict]:
    instruction = (
        "Extract every distinct product, service, feature, module, use-case or platform "
        "offering described on this page. Treat the company's main offering as one item too. "
        "For each item: name, type ('product' for physical/digital goods, 'service' otherwise; "
        "use 'service' for SaaS subscriptions and platform features), short description, "
        "category, and URL if linked. Include core platform features (e.g. 'AI credit scoring', "
        "'B2B shop integration') even when no separate product page exists. "
        "Skip blog posts, news, team members, legal pages, and unrelated content. "
        "Return empty items list ONLY if the page genuinely has no offering content."
    )
    out = []
    seen_names: set[str] = set()
    for u in urls[:limit]:
        item = await _extract_one(crawler, u, Catalog, instruction)
        if not item or not item.get("data"):
            continue
        normalized = _normalize_catalog(item["data"])
        if not normalized or not normalized["items"]:
            continue
        # Deduplicate item names across pages (case-insensitive)
        fresh = []
        for it in normalized["items"]:
            name = (it.get("name") or "").strip().lower()
            if name and name not in seen_names:
                seen_names.add(name)
                fresh.append(it)
        if not fresh:
            continue
        out.append({"url": item["url"], "data": {"items": fresh}})
    return out


# ---------- Pipeline --------------------------------------------------------


IMPRESSUM_PROBE_PATHS = [
    "/impressum", "/impressum/", "/imprint", "/imprint/",
    "/legal-notice", "/legal-notice/", "/legal", "/legal/",
    "/de/impressum", "/de/impressum/", "/en/imprint", "/en/imprint/",
]


async def _probe_impressum(start_url: str) -> List[str]:
    """HEAD/GET-probe common Impressum paths. Avoids hallucination from start-URL fallback."""
    import httpx
    base = f"{urlparse(start_url).scheme}://{urlparse(start_url).netloc}"
    found = []
    async with httpx.AsyncClient(follow_redirects=True, timeout=10.0) as cx:
        for path in IMPRESSUM_PROBE_PATHS:
            try:
                r = await cx.head(base + path)
                if r.status_code == 200 and "text/html" in r.headers.get("content-type", ""):
                    found.append(str(r.url))
                    break
            except (httpx.RequestError, httpx.TimeoutException):
                continue
    return found


async def run(start_url: str, max_pages: int = 60, max_product_pages: int = 8) -> dict:
    buckets = await discover_pages(start_url, max_pages=max_pages)

    impressum_urls = buckets["impressum"]
    if not impressum_urls:
        impressum_urls = await _probe_impressum(start_url)

    overview_urls = buckets["about"] + [start_url]
    governance_urls = buckets["governance"] + buckets["about"]

    # Always extract from home page first — most companies describe their offering there.
    # Then add detected product/use-case sub-pages (deduplicated).
    product_urls: List[str] = [start_url]
    for u in buckets["products"]:
        if u not in product_urls:
            product_urls.append(u)

    async with AsyncWebCrawler(config=BrowserConfig(headless=True)) as crawler:
        overview = await extract_overview(crawler, overview_urls)
        governance = await extract_governance(crawler, governance_urls) if governance_urls else None
        impressum = await extract_impressum(crawler, impressum_urls) if impressum_urls else None
        catalog = await extract_catalog(crawler, product_urls, limit=max_product_pages)

    return {
        "source_url": start_url,
        "summary": overview,
        "products_services": catalog,
        "governance": governance,
        "impressum": impressum,
        "subpages": buckets["subpages"],
        "discovery": {
            "impressum_candidates": buckets["impressum"],
            "about_candidates": buckets["about"],
            "product_candidates": buckets["products"],
            "governance_candidates": buckets["governance"],
        },
    }


# ---------- CLI -------------------------------------------------------------


def _cli() -> int:
    ap = argparse.ArgumentParser(description="Company website extractor (crawl4ai)")
    ap.add_argument("url", help="Start URL of company website")
    ap.add_argument("--out", help="Write JSON report to this file")
    ap.add_argument("--max-pages", type=int, default=60, help="Max pages to crawl (default 60)")
    ap.add_argument("--max-product-pages", type=int, default=8, help="Max product pages to LLM-extract")
    args = ap.parse_args()

    report = asyncio.run(run(args.url, max_pages=args.max_pages, max_product_pages=args.max_product_pages))
    text = json.dumps(report, indent=2, ensure_ascii=False)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"Wrote {args.out}", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
