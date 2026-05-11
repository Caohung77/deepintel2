"""
OpenCorporates anonymous lookup (free tier, ~50 req/day per IP).

Used to verify legal entity exists, capture company_number, jurisdiction, and
incorporation date. Cached 30 days per (jurisdiction, name).

Public API:
    await search_opencorporates(name, jurisdiction="de") -> list[dict]
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import List, Optional

import httpx

CACHE_DIR = Path(__file__).resolve().parent.parent / "cache" / "opencorporates"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_TTL_SECONDS = 30 * 24 * 3600

ENDPOINT = "https://api.opencorporates.com/v0.4/companies/search"


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")[:80]


def _cache_path(name: str, jurisdiction: str) -> Path:
    return CACHE_DIR / f"{jurisdiction}_{_slug(name)}.json"


def _load_cache(name: str, jurisdiction: str) -> Optional[List[dict]]:
    p = _cache_path(name, jurisdiction)
    if not p.exists() or (time.time() - p.stat().st_mtime) > CACHE_TTL_SECONDS:
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _save_cache(name: str, jurisdiction: str, data: List[dict]) -> None:
    try:
        _cache_path(name, jurisdiction).write_text(json.dumps(data), encoding="utf-8")
    except OSError as e:
        print(f"[opencorporates] cache write failed: {e}")


async def search_opencorporates(name: str, jurisdiction: str = "de") -> List[dict]:
    if not name or len(name.strip()) < 3:
        return []

    cached = _load_cache(name, jurisdiction)
    if cached is not None:
        return cached

    params = {"q": name, "jurisdiction_code": jurisdiction, "format": "json", "per_page": 10}
    try:
        async with httpx.AsyncClient(timeout=20.0) as cx:
            r = await cx.get(ENDPOINT, params=params)
            if r.status_code == 429:
                print("[opencorporates] rate-limited (429) — skipping")
                return []
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPError as e:
        print(f"[opencorporates] request failed: {e}")
        return []

    out = []
    for item in (data.get("results", {}) or {}).get("companies", []) or []:
        c = item.get("company", {})
        out.append({
            "source": "opencorporates",
            "legal_name": c.get("name"),
            "company_number": c.get("company_number"),
            "jurisdiction": c.get("jurisdiction_code"),
            "status": c.get("current_status"),
            "incorporation_date": c.get("incorporation_date"),
            "registered_address": c.get("registered_address_in_full"),
            "company_type": c.get("company_type"),
            "url": c.get("opencorporates_url"),
        })
    _save_cache(name, jurisdiction, out)
    return out


if __name__ == "__main__":
    import asyncio
    import sys

    async def _main():
        name = sys.argv[1] if len(sys.argv) > 1 else "Personio"
        juris = sys.argv[2] if len(sys.argv) > 2 else "de"
        out = await search_opencorporates(name, juris)
        print(json.dumps(out, indent=2, ensure_ascii=False))

    asyncio.run(_main())
