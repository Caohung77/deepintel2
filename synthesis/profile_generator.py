"""
Profile generator: LLM call with validator-driven retry loop.

Builds a compact data block from the full pipeline report, calls gpt-4o,
validates the output, and retries up to 3 times feeding violations back.

Public API:
    await synthesize_profile(full_report) -> dict
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from openai import AsyncOpenAI

from synthesis.prompt_b2b_v4_1 import PROMPT_VERSION, build_prompt, build_retry_prompt
from synthesis.validator import validate

MAX_RETRIES = 3


def _unwrap(section: Any) -> Any:
    """Unwrap {url, data:[{...}]} to inner dict."""
    if not section:
        return None
    if isinstance(section, dict) and "data" in section:
        d = section["data"]
        if isinstance(d, list) and d:
            return d[0]
        return d
    return section


def build_data_block(report: Dict[str, Any]) -> str:
    """Compact context for the LLM. Strip noise, keep facts."""
    summary = _unwrap(report.get("summary"))
    impressum = _unwrap(report.get("impressum"))
    governance = _unwrap(report.get("governance"))
    catalog_pages = report.get("products_services") or []

    products: List[dict] = []
    for page in catalog_pages:
        d = _unwrap(page)
        if isinstance(d, dict):
            for it in (d.get("items") or [])[:8]:
                products.append({
                    "name": it.get("name"),
                    "type": it.get("type"),
                    "category": it.get("category"),
                    "description": (it.get("description") or "")[:160],
                })

    enrichment = report.get("enrichment", {})
    wikidata = enrichment.get("wikidata", [])
    tavily = enrichment.get("tavily", {}) or {}
    sanctions = enrichment.get("sanctions", [])

    payload = {
        "source_url": report.get("source_url"),
        "site_summary": summary,
        "impressum": impressum,
        "governance": governance,
        "products_services": products[:20],
        "wikidata_match": wikidata[0] if wikidata else None,
        "competitor_search_snippets": [
            {"title": s.get("title"), "snippet": s.get("snippet"), "url": s.get("url")}
            for s in (tavily.get("competitor_snippets") or [])[:6]
        ],
        "recent_news": [
            {"title": n.get("title"), "snippet": n.get("snippet"), "date": n.get("published_date"), "risk_tag": n.get("risk_tag")}
            for n in (tavily.get("news") or [])[:6]
        ],
        "risk_events_found": [
            {"title": r.get("title"), "snippet": r.get("snippet"), "tag": r.get("risk_tag")}
            for r in (tavily.get("risk_events") or [])[:6]
        ],
        "insolvency": {
            "insolvenzverfahren": (tavily.get("insolvency") or {}).get("insolvenzverfahren", False),
            "insolvenz": (tavily.get("insolvency") or {}).get("insolvenz", False),
            # Tavily's free-text "answer" is intentionally omitted — it echoes the
            # dominant index narrative and can contradict the attributed booleans.
            "evidence": [
                {"title": e.get("title"), "url": e.get("url")}
                for e in ((tavily.get("insolvency") or {}).get("evidence") or [])[:3]
            ],
        },
        "sanctions_hits": sanctions,
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


async def _call_llm(system: str, user: str) -> str:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY not set")

    model = os.getenv("SYNTHESIS_MODEL", "openai/gpt-4o")
    # litellm-style "openai/gpt-4o" → "gpt-4o" for native OpenAI client
    if "/" in model:
        model = model.split("/", 1)[1]

    client = AsyncOpenAI(api_key=api_key)
    resp = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0.2,
        max_tokens=1500,
    )
    return resp.choices[0].message.content or ""


async def synthesize_profile(report: Dict[str, Any]) -> Dict[str, Any]:
    data_block = build_data_block(report)

    system, user = build_prompt(data_block)
    last_text: Optional[str] = None
    last_violations: List[str] = []
    attempts = 0

    while attempts <= MAX_RETRIES:
        attempts += 1
        if attempts == 1:
            text = await _call_llm(system, user)
        else:
            sys_r, user_r = build_retry_prompt(data_block, last_text or "", last_violations)
            text = await _call_llm(sys_r, user_r)

        result = validate(text)
        last_text = text
        last_violations = result.violations

        if result.passed:
            return {
                "prompt_version": PROMPT_VERSION,
                "rendered_markdown": text.strip(),
                "meta_line": result.meta_line,
                "blocks": {k: v for k, v in result.parsed.items() if not k.startswith("_")},
                "validation": {
                    "passed": True,
                    "violations": [],
                    "attempts": attempts,
                },
            }

    # All retries exhausted
    return {
        "prompt_version": PROMPT_VERSION,
        "rendered_markdown": (last_text or "").strip(),
        "meta_line": (validate(last_text or "").meta_line if last_text else None),
        "blocks": {k: v for k, v in validate(last_text or "").parsed.items() if not k.startswith("_")} if last_text else {},
        "validation": {
            "passed": False,
            "violations": last_violations,
            "attempts": attempts,
        },
    }
