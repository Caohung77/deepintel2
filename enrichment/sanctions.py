"""
Sanctions check via OpenSanctions consolidated dataset.

OpenSanctions aggregates EU consolidated list, OFAC SDN, UK HMT, UN, and others
into a single CSV. Free for non-commercial and commercial use under CC-BY 4.0.

- Downloads consolidated CSV to cache/ once per 24h.
- Fuzzy-matches a candidate company name (and optional list of person names)
  using RapidFuzz WRatio. Threshold default 90.
- Returns hits as a list of plain dicts.

Public API:
    await check_sanctions(company_name, person_names) -> list[dict]
"""

from __future__ import annotations

import asyncio
import csv
import io
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional

import httpx
from rapidfuzz import fuzz, process

CACHE_DIR = Path(__file__).resolve().parent.parent / "cache"
CACHE_DIR.mkdir(exist_ok=True)

OPENSANCTIONS_URL = "https://data.opensanctions.org/datasets/latest/sanctions/targets.simple.csv"
OS_CACHE = CACHE_DIR / "opensanctions_sanctions.csv"

CACHE_TTL_SECONDS = 24 * 3600
MATCH_THRESHOLD = 90


@dataclass
class SanctionsEntry:
    list_name: str          # source dataset code (e.g. "eu_fsf", "us_ofac_sdn")
    entity: str             # canonical name
    schema: Optional[str] = None  # "Person", "Company", "Organization", "LegalEntity"
    program: Optional[str] = None
    listed_date: Optional[str] = None
    countries: Optional[str] = None
    aliases: Optional[str] = None


def _is_fresh(path: Path) -> bool:
    return path.exists() and (time.time() - path.stat().st_mtime) < CACHE_TTL_SECONDS


async def _download(url: str, dest: Path, timeout: float = 180.0) -> bool:
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as cx:
            r = await cx.get(url)
            r.raise_for_status()
            dest.write_bytes(r.content)
        return True
    except (httpx.HTTPError, OSError) as e:
        print(f"[sanctions] download failed for {url}: {e}")
        return False


async def _ensure_cached() -> None:
    if not _is_fresh(OS_CACHE):
        await _download(OPENSANCTIONS_URL, OS_CACHE)


def _parse_opensanctions(path: Path) -> List[SanctionsEntry]:
    if not path.exists():
        return []
    out: List[SanctionsEntry] = []
    try:
        with path.open("r", encoding="utf-8", errors="ignore", newline="") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                name = (row.get("name") or "").strip()
                if not name:
                    continue
                datasets = (row.get("dataset") or row.get("datasets") or "").strip() or "opensanctions"
                primary = datasets.split(";")[0].strip() or "opensanctions"
                out.append(SanctionsEntry(
                    list_name=primary,
                    entity=name,
                    schema=row.get("schema") or None,
                    program=(row.get("sanctions") or row.get("program_ids") or None),
                    listed_date=row.get("first_seen") or None,
                    countries=row.get("countries") or None,
                    aliases=row.get("aliases") or None,
                ))
    except (csv.Error, OSError) as e:
        print(f"[sanctions] parse error: {e}")
    return out


_LIST_CACHE: dict[str, object] = {}


async def _load_lists() -> List[SanctionsEntry]:
    await _ensure_cached()
    if "merged" not in _LIST_CACHE:
        _LIST_CACHE["merged"] = _parse_opensanctions(OS_CACHE)
    return _LIST_CACHE["merged"]  # type: ignore[return-value]


def _build_lookup(entries: List[SanctionsEntry]) -> tuple[List[str], List[int]]:
    """Flatten name + aliases into a parallel array pointing back to entry index.

    Aliases shorter than 6 chars or single-word common terms are skipped to
    prevent false positives like alias "Group" matching any "X Group" query.
    """
    SKIP_SINGLES = {"group", "company", "corp", "ltd", "gmbh", "ag", "se", "kg", "ohg",
                    "limited", "holdings", "international", "global", "trading"}
    cands: List[str] = []
    parent_idx: List[int] = []
    for i, e in enumerate(entries):
        cands.append(e.entity)
        parent_idx.append(i)
        if e.aliases:
            for a in e.aliases.split(";"):
                a = a.strip().strip('"').strip("'")
                if not a or len(a) < 6:
                    continue
                # reject single-token generic words
                if " " not in a and a.lower() in SKIP_SINGLES:
                    continue
                cands.append(a)
                parent_idx.append(i)
    return cands, parent_idx


async def _get_lookup() -> tuple[List[SanctionsEntry], List[str], List[int]]:
    entries = await _load_lists()
    if "lookup" not in _LIST_CACHE:
        _LIST_CACHE["lookup"] = _build_lookup(entries)
    cands, parent_idx = _LIST_CACHE["lookup"]  # type: ignore[assignment]
    return entries, cands, parent_idx


def _match(
    query: str,
    entries: List[SanctionsEntry],
    cands: List[str],
    parent_idx: List[int],
    threshold: int = MATCH_THRESHOLD,
) -> List[dict]:
    if not query or not entries:
        return []
    q = query.strip()
    if len(q) < 4:
        return []
    # token_sort_ratio: requires both sides share same tokens (no subset trick).
    # Combined with min-length sanity check to avoid alias-fragment matches.
    results = process.extract(q, cands, scorer=fuzz.token_sort_ratio, limit=10, score_cutoff=threshold)
    hits = []
    seen_parents: set[int] = set()
    for matched_name, score, cand_idx in results:
        # Length sanity: candidate must be within 50%-200% of query length.
        if not (0.5 * len(q) <= len(matched_name) <= 2.0 * len(q)):
            continue
        pidx = parent_idx[cand_idx]
        if pidx in seen_parents:
            continue
        seen_parents.add(pidx)
        e = entries[pidx]
        hits.append({
            "list_name": e.list_name,
            "matched_entity": e.entity,
            "matched_alias": matched_name if matched_name != e.entity else None,
            "match_score": float(score),
            "schema": e.schema,
            "program": e.program,
            "listed_date": e.listed_date,
            "countries": e.countries,
            "matched_query": q,
        })
    return hits


async def check_sanctions(company_name: Optional[str], person_names: Optional[Iterable[str]] = None) -> List[dict]:
    """Return list of sanction hits across EU + OFAC. Empty list = clean."""
    queries = []
    if company_name:
        queries.append(company_name)
    for n in person_names or []:
        if n and n.strip():
            queries.append(n.strip())
    if not queries:
        return []

    entries, cands, parent_idx = await _get_lookup()
    hits: List[dict] = []
    seen = set()
    for q in queries:
        for h in _match(q, entries, cands, parent_idx):
            key = (h["list_name"], h["matched_entity"], h["matched_query"])
            if key in seen:
                continue
            seen.add(key)
            hits.append(h)
    return hits


if __name__ == "__main__":
    # Smoke test
    import sys

    async def _main():
        name = sys.argv[1] if len(sys.argv) > 1 else "Wagner Group"
        hits = await check_sanctions(name)
        import json as _json
        print(_json.dumps(hits, indent=2, ensure_ascii=False))

    asyncio.run(_main())
