"""
Orchestrates branch classification + branch outlook synthesis.

Two LLM calls:
  1. classify_branch — pick branch_key (gemini-2.5-flash-lite, cheap+fast).
  2. synthesize_outlook — write Branchen-Ausblick + Auswirkung (gpt-4o for quality).

Public API:
    await analyze_branch(company_data, company_name) -> dict
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, Optional

from openai import AsyncOpenAI

from enrichment.sectorbench import (
    BRANCH_KEYS,
    get_branch,
    get_branch_news,
)
from synthesis.prompt_branch_v1 import (
    BRANCH_OPTIONS,
    branch_outlook_prompt,
    classify_branch_prompt,
)


async def _call_gemini(system: str, user: str, *, model: str = "gemini-2.5-flash-lite",
                       temperature: float = 0.0, max_tokens: int = 200) -> str:
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not set")
    client = AsyncOpenAI(
        api_key=api_key,
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
    )
    resp = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=temperature,
        max_tokens=max_tokens,
        extra_body={"reasoning_effort": "none"},
    )
    return (resp.choices[0].message.content or "").strip()


async def _call_openai(system: str, user: str, *, model: Optional[str] = None,
                       temperature: float = 0.2, max_tokens: int = 1200) -> str:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY not set")
    model_name = model or os.getenv("BRANCH_OUTLOOK_MODEL", "gpt-4o")
    if "/" in model_name:
        model_name = model_name.split("/", 1)[1]
    client = AsyncOpenAI(api_key=api_key)
    resp = await client.chat.completions.create(
        model=model_name,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=temperature,
        max_tokens=max_tokens,
    )
    return (resp.choices[0].message.content or "").strip()


_BRANCH_KEY_RX = re.compile(r"\b(" + "|".join(BRANCH_KEYS) + r")\b", re.I)


def _parse_branch_key(raw: str) -> Optional[str]:
    if not raw:
        return None
    # Strip code fences / markdown emphasis
    cleaned = raw.strip().strip("`").strip("*").strip().lower()
    if cleaned in BRANCH_KEYS:
        return cleaned
    m = _BRANCH_KEY_RX.search(raw)
    if m:
        return m.group(1).lower()
    return None


async def classify_branch(company_data: Dict[str, Any]) -> Optional[str]:
    system, user = classify_branch_prompt(company_data)
    raw = await _call_gemini(system, user, max_tokens=20)
    key = _parse_branch_key(raw)
    if key:
        return key
    # one retry with strict reminder
    raw2 = await _call_gemini(
        system + "\n\nWICHTIG: Antworte AUSSCHLIESSLICH mit dem branch_key in Kleinbuchstaben.",
        user,
        max_tokens=20,
    )
    return _parse_branch_key(raw2)


def _has_sections(text: str) -> bool:
    return "**BRANCHEN-AUSBLICK**" in text and ("**AUSWIRKUNG" in text)


async def synthesize_outlook(company_name: str, company_data: Dict[str, Any],
                              branch_score: Dict[str, Any],
                              branch_news: Optional[Dict[str, Any]]) -> str:
    system, user = branch_outlook_prompt(company_name, company_data, branch_score, branch_news)
    text = await _call_openai(system, user, temperature=0.2)
    # If model produced wrong shape, retry once with strict reminder
    if not _has_sections(text):
        text = await _call_openai(
            system + "\n\nWICHTIG: Verwende EXAKT die beiden fett markierten Überschriften "
                     "**BRANCHEN-AUSBLICK** und **AUSWIRKUNG AUF [FIRMA]**.",
            user,
        )
    return text


async def analyze_branch(company_data: Dict[str, Any],
                         company_name: Optional[str] = None) -> Dict[str, Any]:
    """Full branch analysis pipeline. Returns {branch_key, branch_score, branch_news, outlook_markdown}."""
    branch_key = await classify_branch(company_data)
    if not branch_key:
        return {"error": "branch classifier could not pick a key"}

    branch_score = await get_branch(branch_key)
    if not branch_score:
        return {
            "branch_key": branch_key,
            "error": "SectorBench API: branch score unavailable (token missing or 404)",
        }

    branch_news = await get_branch_news(branch_key)

    name = company_name or company_data.get("name") or "dem Unternehmen"
    outlook_markdown = await synthesize_outlook(name, company_data, branch_score, branch_news)

    return {
        "branch_key": branch_key,
        "branch_name_de": branch_score.get("branch_name_de"),
        "branch_name_en": branch_score.get("branch_name_en"),
        "branch_score": branch_score,
        "branch_news": branch_news,
        "outlook_markdown": outlook_markdown,
    }
