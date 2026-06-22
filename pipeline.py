"""
Pipeline orchestrator: site crawl + enrichment + synthesis.

Stages:
  1. company_extractor.run()    site crawl + LLM extract
  2. tavily_enrich              competitors + news + risk events
  3. wikidata_lookup            structured corporate facts
  4. check_sanctions            EU+OFAC+UN+UK fuzzy match
  5. profile_generator.synthesize   German B2B profile (4 blocks)

Stages 2-4 run concurrently after stage 1.
Stage 5 runs after all enrichment done.

Public API:
    await run_full_pipeline(url, *, with_profile=True) -> dict
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from company_extractor import run as run_site_extractor
from enrichment.sanctions import check_sanctions
from enrichment.tavily_client import tavily_enrich
from enrichment.wikidata import wikidata_lookup


def _company_name(site_report: dict) -> Optional[str]:
    """Best-effort: prefer Impressum company_name, then summary name."""
    imp = site_report.get("impressum")
    if imp and imp.get("data"):
        d = imp["data"]
        if isinstance(d, list) and d:
            d = d[0]
        if isinstance(d, dict) and d.get("company_name"):
            return d["company_name"]
    sm = site_report.get("summary")
    if sm and sm.get("data"):
        d = sm["data"]
        if isinstance(d, list) and d:
            d = d[0]
        if isinstance(d, dict) and d.get("name"):
            return d["name"]
    return None


def _person_names(site_report: dict) -> list[str]:
    out: list[str] = []
    imp = site_report.get("impressum")
    if imp and imp.get("data"):
        d = imp["data"]
        if isinstance(d, list) and d:
            d = d[0]
        rb = d.get("represented_by") if isinstance(d, dict) else None
        if rb:
            out.extend(rb if isinstance(rb, list) else [rb])
    return [n for n in out if n and isinstance(n, str)]


def _impressum_dict(site_report: dict) -> dict:
    imp = site_report.get("impressum") or {}
    d = imp.get("data")
    if isinstance(d, list):
        d = d[0] if d else None
    return d if isinstance(d, dict) else {}


def _effective_register_input(
    hr_no: Optional[str],
    register_court: Optional[str],
    site_report: dict,
    *,
    prefer_impressum: bool = False,
) -> dict:
    imp = _impressum_dict(site_report)
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


async def run_full_pipeline(
    url: str,
    *,
    max_pages: int = 60,
    max_product_pages: int = 6,
    with_profile: bool = True,
    skip_enrichment: bool = False,
    hr_no: Optional[str] = None,
    register_court: Optional[str] = None,
    company_name: Optional[str] = None,
) -> Dict[str, Any]:
    # Stage 1: site crawl
    site_report = await run_site_extractor(
        url,
        max_pages=max_pages,
        max_product_pages=max_product_pages,
    )

    # Fact-check supplied Handelsregister data against the crawled Impressum.
    # A URL-backed request should not fail solely because caller-supplied register
    # fields differ from the site's Impressum; keep the diagnostic and trust the
    # Impressum register for downstream enrichment instead.
    identity_match = None
    if (hr_no or "").strip() or (register_court or "").strip():
        from enrichment.company_finder import _verify
        identity_match = _verify(hr_no, register_court, _impressum_dict(site_report))

    effective_register = _effective_register_input(
        hr_no,
        register_court,
        site_report,
        prefer_impressum=(identity_match or {}).get("verified") is False,
    )
    effective_hr_no = effective_register.get("hr_no")
    effective_register_court = effective_register.get("register_court")

    enrichment: Dict[str, Any] = {
        "wikidata": [],
        "tavily": {
            "competitor_snippets": [],
            "news": [],
            "risk_events": [],
            "insolvency": {"insolvenzverfahren": False, "insolvenz": False, "confirmed": False,
                           "source": "tavily", "answer": "", "evidence": []},
        },
        "sanctions": [],
    }

    if not skip_enrichment:
        company_name = (company_name or "").strip() or _company_name(site_report) or url
        persons = _person_names(site_report)

        # Stages 2-4 in parallel
        wd_task = asyncio.create_task(wikidata_lookup(company_name))
        tv_task = asyncio.create_task(
            tavily_enrich(company_name, own_domain=url,
                          hr_no=effective_hr_no, register_court=effective_register_court)
        )
        sn_task = asyncio.create_task(check_sanctions(company_name, persons))
        wd, tv, sn = await asyncio.gather(wd_task, tv_task, sn_task)
        enrichment["wikidata"] = wd
        enrichment["tavily"] = tv
        enrichment["sanctions"] = sn

    full = {**site_report, "enrichment": enrichment, "profile": None}
    if effective_hr_no or effective_register_court:
        full["register_input"] = effective_register
    if identity_match is not None:
        full["identity_match"] = identity_match

    # Stage 5: synthesis (lazy import to avoid loading openai unless needed)
    if with_profile:
        try:
            from synthesis.profile_generator import synthesize_profile
            full["profile"] = await synthesize_profile(full)
        except ImportError as e:
            print(f"[pipeline] synthesis module not available: {e}")
        except Exception as e:  # noqa: BLE001 — pipeline must not crash on synthesis
            print(f"[pipeline] synthesis failed: {e}")
            full["profile"] = {"error": str(e)}

    return full


if __name__ == "__main__":
    import argparse
    import sys

    ap = argparse.ArgumentParser(description="Full B2B profile pipeline")
    ap.add_argument("url")
    ap.add_argument("--out")
    ap.add_argument("--max-pages", type=int, default=60)
    ap.add_argument("--max-product-pages", type=int, default=6)
    ap.add_argument("--no-profile", action="store_true", help="Skip synthesis stage")
    ap.add_argument("--skip-enrichment", action="store_true", help="Site crawl only")
    args = ap.parse_args()

    report = asyncio.run(run_full_pipeline(
        args.url,
        max_pages=args.max_pages,
        max_product_pages=args.max_product_pages,
        with_profile=not args.no_profile,
        skip_enrichment=args.skip_enrichment,
    ))
    text = json.dumps(report, indent=2, ensure_ascii=False, default=str)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"Wrote {args.out}", file=sys.stderr)
    else:
        print(text)
