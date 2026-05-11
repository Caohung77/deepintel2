"""
Tavily search client for B2B enrichment.

Three queries per company:
  1. Competitors search
  2. Risk-event search (insolvency, lawsuits, scandals, recalls, fines)
  3. Recent news (last 90 days)

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
) -> dict:
    cache_key = f"{topic}_{query}_{days}"
    cached = _load_cache(cache_key)
    if cached:
        return cached

    payload = {
        "query": query,
        "topic": topic,
        "max_results": max_results,
        "search_depth": "basic",
    }
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


async def tavily_enrich(company_name: str, own_domain: Optional[str] = None) -> dict:
    """Run 3 Tavily searches and normalise into competitors / news / risk_events."""
    api_key = os.getenv("TAVILY_API_KEY")
    if not api_key:
        print("[tavily] TAVILY_API_KEY not set — skipping enrichment")
        return {"competitors": [], "news": [], "risk_events": []}

    if not company_name:
        return {"competitors": [], "news": [], "risk_events": []}

    exclude = []
    if own_domain:
        d = urlparse(own_domain).netloc or own_domain
        exclude.append(d.replace("www.", ""))

    async with httpx.AsyncClient() as cx:
        comp_q = f'"{company_name}" competitors OR Wettbewerber OR alternatives'
        risk_q = f'"{company_name}" ({RISK_KEYWORDS})'
        news_q = f'"{company_name}"'

        comp_raw, risk_raw, news_raw = await asyncio.gather(
            _tavily_search(cx, comp_q, api_key, exclude_domains=exclude, max_results=8),
            _tavily_search(cx, risk_q, api_key, exclude_domains=exclude, max_results=10),
            _tavily_search(cx, news_q, api_key, topic="news", days=90, max_results=10),
        )

    comp_items = _norm_results(comp_raw)
    risk_items = _norm_results(risk_raw)
    news_items = _norm_results(news_raw)

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
