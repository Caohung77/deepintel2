"""
SectorBench Public Data API client.

API: https://sectorbench.theaiwhisperer.cloud/api/v1/
Auth: Bearer token in `SECTORBENCH_API_TOKEN` env var (format sbk_<43>).

10 fixed branches:
  automotive, healthcare, construction, renewable_energy, logistics,
  fintech, it_services, retail, hospitality, manufacturing

Public API:
    await list_branches() -> list[dict]
    await get_branch(key) -> dict
    await get_branch_news(key) -> dict
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

BASE_URL = os.getenv("SECTORBENCH_BASE_URL", "https://sectorbench.theaiwhisperer.cloud/api/v1")

CACHE_DIR = Path(__file__).resolve().parent.parent / "cache" / "sectorbench"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
SCORES_TTL = 24 * 3600       # 24h — scores update monthly anyway
NEWS_TTL = 24 * 3600         # 24h — news report monthly
META_TTL = 4 * 3600

BRANCH_KEYS = (
    "automotive", "healthcare", "construction", "renewable_energy", "logistics",
    "fintech", "it_services", "retail", "hospitality", "manufacturing",
)


def _token() -> Optional[str]:
    return os.getenv("SECTORBENCH_API_TOKEN")


def _cache_path(name: str) -> Path:
    return CACHE_DIR / f"{name}.json"


def _read_cache(name: str, ttl: int) -> Optional[Any]:
    p = _cache_path(name)
    if not p.exists() or (time.time() - p.stat().st_mtime) > ttl:
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_cache(name: str, data: Any) -> None:
    try:
        _cache_path(name).write_text(json.dumps(data), encoding="utf-8")
    except OSError as e:
        print(f"[sectorbench] cache write failed {name}: {e}")


async def _get(path: str, *, cache_key: Optional[str] = None, ttl: int = 3600) -> Optional[dict]:
    token = _token()
    if not token:
        print("[sectorbench] SECTORBENCH_API_TOKEN not set — skipping")
        return None

    if cache_key:
        cached = _read_cache(cache_key, ttl)
        if cached is not None:
            return cached

    url = f"{BASE_URL}{path}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=15.0) as cx:
            r = await cx.get(url, headers=headers)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPError as e:
        print(f"[sectorbench] GET {path} failed: {e}")
        return None

    if cache_key:
        _write_cache(cache_key, data)
    return data


async def list_branches() -> List[Dict[str, Any]]:
    """Current composite scores for all 10 branches, sorted by rank."""
    data = await _get("/scores/ranking", cache_key="ranking", ttl=SCORES_TTL)
    if not data:
        # fallback /scores
        data = await _get("/scores", cache_key="scores", ttl=SCORES_TTL)
        if not data:
            return []
        return data.get("scores", []) or []
    return data.get("ranking", []) or []


async def get_branch(branch_key: str) -> Optional[Dict[str, Any]]:
    if branch_key not in BRANCH_KEYS:
        return None
    return await _get(f"/branches/{branch_key}", cache_key=f"branch_{branch_key}", ttl=SCORES_TTL)


async def get_branch_news(branch_key: str) -> Optional[Dict[str, Any]]:
    """Latest monthly branch news report: executive_overview, key_developments, outlook."""
    if branch_key not in BRANCH_KEYS:
        return None
    return await _get(f"/branches/{branch_key}/news", cache_key=f"news_{branch_key}", ttl=NEWS_TTL)


if __name__ == "__main__":
    import asyncio
    import sys

    async def _main():
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except ImportError:
            pass

        cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
        if cmd == "list":
            scores = await list_branches()
            for s in scores[:5]:
                print(f"{s.get('rank', '?'):>2}. {s.get('branch_key', ''):20} "
                      f"score={s.get('composite_score', 0):.1f}  risk={s.get('risk_level')}")
        elif cmd == "news":
            key = sys.argv[2] if len(sys.argv) > 2 else "fintech"
            news = await get_branch_news(key)
            print(json.dumps(news, indent=2, ensure_ascii=False)[:2000])

    asyncio.run(_main())
