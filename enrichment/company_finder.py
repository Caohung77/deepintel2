"""Handelsregister identity helpers.

Two uses:
  - `_verify(hr_no, register_court, impressum)` — fact-check a crawled Impressum's
    register number + court against caller-supplied values (used on the URL path as
    non-fatal diagnostic metadata).
  - `_resolve_name(hr_no, register_court)` — when only an HR number is supplied (no
    URL, no name), distill a company name from a register web search so the
    insolvency/enrichment search has a subject.

Matching is strict on the HR number (type + digits) and fuzzy on the court name.
"""
from __future__ import annotations

import json
import os
import re
from typing import Optional

import httpx
from rapidfuzz import fuzz

_HR_RX = re.compile(r"\b(HRA|HRB|GnR|PR|VR|GsR)\s*\.?\s*(\d+)", re.I)


def _parse_hr(value: Optional[str]):
    """('HRA', '12345') from 'HRA 12345'; (None, digits) if only a number is present."""
    if not value:
        return None
    m = _HR_RX.search(value)
    if m:
        return (m.group(1).upper(), m.group(2))
    d = re.search(r"\d+", value)
    return (None, d.group()) if d else None


def _court_norm(value: Optional[str]) -> str:
    if not value:
        return ""
    value = re.sub(r"(?i)\b(amtsgericht|ag)\b\.?", "", value)
    return value.strip().lower()


def _hr_match(user_hr: Optional[str], imp_hr: Optional[str]) -> Optional[bool]:
    """True/False if comparable, None if either side can't be parsed."""
    u, i = _parse_hr(user_hr), _parse_hr(imp_hr)
    if not u or not i:
        return None
    if u[0] and i[0] and u[0] != i[0]:   # HRA vs HRB with same digits → different register
        return False
    return u[1] == i[1]


def _court_match(user_c: Optional[str], imp_c: Optional[str]) -> Optional[bool]:
    if not user_c or not imp_c:
        return None
    return fuzz.token_sort_ratio(_court_norm(user_c), _court_norm(imp_c)) >= 85


def _verify(hr_no: Optional[str], register_court: Optional[str], imp: Optional[dict]) -> dict:
    """Compare supplied register data against an Impressum dict.

    verified=True  → at least one supplied field confirmed, none contradicted.
    verified=False → a supplied field contradicts the site (mismatch).
    verified=None  → site carries no comparable register data (cannot confirm).
    """
    imp = imp or {}
    hr_res = _hr_match(hr_no, imp.get("register_number")) if hr_no else None
    court_res = _court_match(register_court, imp.get("register_court")) if register_court else None
    checks = {"hr_no": hr_res, "register_court": court_res}
    comparable = [r for r in (hr_res, court_res) if r is not None]
    if any(r is False for r in comparable):
        return {"verified": False, "reason": "register mismatch", "checks": checks,
                "site_register": {"register_number": imp.get("register_number"),
                                  "register_court": imp.get("register_court")}}
    if comparable and all(comparable):
        return {"verified": True, "reason": "register match", "checks": checks,
                "site_register": {"register_number": imp.get("register_number"),
                                  "register_court": imp.get("register_court")}}
    return {"verified": None, "reason": "no comparable register data on site", "checks": checks,
            "site_register": {"register_number": imp.get("register_number"),
                              "register_court": imp.get("register_court")}}


async def _resolve_name(hr_no: Optional[str], register_court: Optional[str]) -> Optional[str]:
    """Resolve a company NAME from a Handelsregister number + court via a web search.

    Searching by HR number surfaces register directories (northdata, companyhouse, …),
    not the company's own site — but Tavily's `answer` + result titles state the name,
    which a small LLM call distills. The name is only a *candidate* for the
    insolvency/enrichment search.
    """
    key = os.getenv("TAVILY_API_KEY")
    if not key or not hr_no:
        return None
    from enrichment.tavily_client import _norm_results, _tavily_search
    # Keep the query minimal — just HR number + city. Extra terms ("Amtsgericht",
    # "Firma Unternehmen") pollute the results and make Tavily's answer name the wrong
    # company (e.g. HRB 4745 Bad Kreuznach resolved wrong with extra words; clean → F. Klein GmbH).
    query = " ".join(p for p in (hr_no, register_court) if p)
    try:
        async with httpx.AsyncClient() as cx:
            raw = await _tavily_search(cx, query, key, max_results=6, include_answer=True)
    except Exception as e:  # noqa: BLE001
        print(f"[finder] register search failed: {e}")
        return None
    answer = (raw.get("answer") or "").strip()
    titles = " | ".join((r.get("title") or "") for r in _norm_results(raw)[:6])
    if not answer and not titles:
        return None

    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        return None
    from openai import AsyncOpenAI
    client = AsyncOpenAI(
        api_key=api_key,
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
    )
    msg = (
        f"Handelsregister-Nr.: {hr_no}\nRegistergericht: {register_court or '?'}\n"
        f"Web-Antwort: {answer}\nTreffer-Titel: {titles}\n\n"
        "Welcher Firmenname (inkl. Rechtsform, z.B. GmbH/AG/KG) gehört zu dieser "
        'Handelsregisternummer? Antworte als JSON {"company_name": "..."} '
        'oder {"company_name": null}, wenn nicht eindeutig.'
    )
    try:
        resp = await client.chat.completions.create(
            model=os.getenv("FAST_MODEL", "gemini-2.5-flash-lite"),
            messages=[{"role": "user", "content": msg}],
            temperature=0.0, top_p=0.0,
            response_format={"type": "json_object"},
            max_tokens=200, extra_body={"reasoning_effort": "none"},
        )
        content = (resp.choices[0].message.content or "").strip()
        content = re.sub(r"^```(?:json)?\s*", "", content)
        content = re.sub(r"\s*```$", "", content)
        name = (json.loads(content) or {}).get("company_name")
        return name.strip() if name and name.strip() else None
    except Exception as e:  # noqa: BLE001
        print(f"[finder] name resolve failed: {e}")
        return None
