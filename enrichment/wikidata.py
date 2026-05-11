"""
Wikidata SPARQL lookup for company facts.

Resolves a company name to a Wikidata entity, then pulls structured facts:
  - legal name, country, headquarters city
  - founding date, parent organization
  - CEO / founders
  - industry, ISIN, register number
  - website

Free, no auth. Cached 30 days per query.

Public API:
    await wikidata_lookup(name, country="Germany") -> list[dict]
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import List, Optional

import httpx

CACHE_DIR = Path(__file__).resolve().parent.parent / "cache" / "wikidata"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_TTL_SECONDS = 30 * 24 * 3600

WBSEARCH_ENDPOINT = "https://www.wikidata.org/w/api.php"
SPARQL_ENDPOINT = "https://query.wikidata.org/sparql"

USER_AGENT = "deepintel2-mvp/0.1 (https://example.local; contact@example.local)"


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")[:80]


def _cache_path(name: str) -> Path:
    return CACHE_DIR / f"{_slug(name)}.json"


def _load_cache(name: str) -> Optional[List[dict]]:
    p = _cache_path(name)
    if not p.exists() or (time.time() - p.stat().st_mtime) > CACHE_TTL_SECONDS:
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _save_cache(name: str, data: List[dict]) -> None:
    try:
        _cache_path(name).write_text(json.dumps(data), encoding="utf-8")
    except OSError as e:
        print(f"[wikidata] cache write failed: {e}")


async def _wbsearch(cx: httpx.AsyncClient, name: str, lang: str = "de") -> List[str]:
    """Return up to 5 candidate Q-IDs."""
    params = {
        "action": "wbsearchentities",
        "search": name,
        "language": lang,
        "type": "item",
        "limit": 5,
        "format": "json",
        "uselang": lang,
    }
    headers = {"User-Agent": USER_AGENT}
    try:
        r = await cx.get(WBSEARCH_ENDPOINT, params=params, headers=headers, timeout=15.0)
        r.raise_for_status()
        data = r.json()
    except httpx.HTTPError as e:
        print(f"[wikidata] wbsearch failed: {e}")
        return []
    return [hit["id"] for hit in data.get("search", []) if hit.get("id", "").startswith("Q")]


SPARQL_TEMPLATE = """
SELECT
  ?item ?itemLabel ?legalName ?countryLabel ?hqLabel ?industryLabel
  ?inception ?ceoLabel ?founderLabel ?parentLabel ?isin ?websiteUrl ?hrb ?ustid
WHERE {{
  VALUES ?item {{ {qids} }}
  OPTIONAL {{ ?item wdt:P1448 ?legalName . }}
  OPTIONAL {{ ?item wdt:P17 ?country . }}
  OPTIONAL {{ ?item wdt:P159 ?hq . }}
  OPTIONAL {{ ?item wdt:P452 ?industry . }}
  OPTIONAL {{ ?item wdt:P571 ?inception . }}
  OPTIONAL {{ ?item wdt:P169 ?ceo . }}
  OPTIONAL {{ ?item wdt:P112 ?founder . }}
  OPTIONAL {{ ?item wdt:P749 ?parent . }}
  OPTIONAL {{ ?item wdt:P946 ?isin . }}
  OPTIONAL {{ ?item wdt:P856 ?websiteUrl . }}
  OPTIONAL {{ ?item wdt:P5285 ?hrb . }}
  OPTIONAL {{ ?item wdt:P3608 ?ustid . }}
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "de,en". }}
}}
"""


async def _sparql_facts(cx: httpx.AsyncClient, qids: List[str]) -> List[dict]:
    if not qids:
        return []
    qids_block = " ".join(f"wd:{q}" for q in qids)
    query = SPARQL_TEMPLATE.format(qids=qids_block)
    headers = {"User-Agent": USER_AGENT, "Accept": "application/sparql-results+json"}
    try:
        r = await cx.get(SPARQL_ENDPOINT, params={"query": query, "format": "json"}, headers=headers, timeout=20.0)
        r.raise_for_status()
        data = r.json()
    except httpx.HTTPError as e:
        print(f"[wikidata] SPARQL failed: {e}")
        return []

    grouped: dict[str, dict] = {}
    for row in data.get("results", {}).get("bindings", []):
        qid = row.get("item", {}).get("value", "").rsplit("/", 1)[-1]
        if not qid:
            continue
        rec = grouped.setdefault(qid, {
            "source": "wikidata",
            "qid": qid,
            "name": row.get("itemLabel", {}).get("value"),
            "legal_name": row.get("legalName", {}).get("value"),
            "country": row.get("countryLabel", {}).get("value"),
            "headquarters": row.get("hqLabel", {}).get("value"),
            "industries": set(),
            "incorporation_date": row.get("inception", {}).get("value", "")[:10] or None,
            "ceos": set(),
            "founders": set(),
            "parents": set(),
            "isin": row.get("isin", {}).get("value"),
            "website": row.get("websiteUrl", {}).get("value"),
            "register_number": row.get("hrb", {}).get("value"),
            "vat_id": row.get("ustid", {}).get("value"),
        })

        if (v := row.get("industryLabel", {}).get("value")):
            rec["industries"].add(v)
        if (v := row.get("ceoLabel", {}).get("value")):
            rec["ceos"].add(v)
        if (v := row.get("founderLabel", {}).get("value")):
            rec["founders"].add(v)
        if (v := row.get("parentLabel", {}).get("value")):
            rec["parents"].add(v)

    out = []
    for rec in grouped.values():
        rec["industries"] = sorted(rec["industries"])
        rec["ceos"] = sorted(rec["ceos"])
        rec["founders"] = sorted(rec["founders"])
        rec["parents"] = sorted(rec["parents"])
        out.append(rec)
    return out


async def wikidata_lookup(name: str, lang: str = "de") -> List[dict]:
    if not name or len(name.strip()) < 3:
        return []

    cached = _load_cache(name)
    if cached is not None:
        return cached

    async with httpx.AsyncClient() as cx:
        qids = await _wbsearch(cx, name, lang=lang)
        results = await _sparql_facts(cx, qids[:3])  # top 3 candidates only

    _save_cache(name, results)
    return results


if __name__ == "__main__":
    import asyncio
    import sys

    async def _main():
        name = sys.argv[1] if len(sys.argv) > 1 else "Personio"
        out = await wikidata_lookup(name)
        print(json.dumps(out, indent=2, ensure_ascii=False))

    asyncio.run(_main())
