"""
deepintel2 public REST API.

Single endpoint:
  POST /api/analyze   { "domain": "...", "options": {...} }  -> JSON analysis

Auth: Bearer token in `DEEPINTEL_API_TOKEN` env var.
Same fast_extract pipeline that powers the Streamlit UI.

Run standalone:
  uvicorn api.server:app --host 0.0.0.0 --port 8000

Inside Docker: started by docker/entrypoint.sh.
"""

from __future__ import annotations

import os
import re
import time
from typing import Optional
from urllib.parse import urlparse

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from fast_extractor import fast_extract

API_TITLE = "deepintel2 Public API"
API_VERSION = "0.2.6"
API_DESC = (
    "Company-website intelligence API. "
    "Send a domain, receive a structured B2B analysis "
    "(elevator pitch, products & services, Impressum, "
    "sector classification + outlook, 4-block decision-maker profile)."
)

app = FastAPI(
    title=API_TITLE,
    version=API_VERSION,
    description=API_DESC,
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
    root_path=os.getenv("DEEPINTEL_API_ROOT", ""),
)


# ---- Auth -----------------------------------------------------------------

_EXPECTED_TOKEN = os.getenv("DEEPINTEL_API_TOKEN", "").strip()


def require_token(authorization: Optional[str] = Header(default=None)) -> None:
    if not _EXPECTED_TOKEN:
        raise HTTPException(
            status_code=503,
            detail="API token not configured on the server (DEEPINTEL_API_TOKEN unset).",
        )
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing or malformed Authorization header. Use: 'Authorization: Bearer <token>'.")
    token = authorization.split(" ", 1)[1].strip()
    if token != _EXPECTED_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid API token.")


# ---- Request / response models -------------------------------------------


class AnalyzeOptions(BaseModel):
    with_profile: bool = Field(
        True, description="Generate 4-block German B2B-Entscheider-Profil (gpt-4o)."
    )
    with_enrichment: bool = Field(
        True, description="Run Tavily competitor/news search + OpenSanctions screening."
    )
    with_branch: bool = Field(
        True, description="Classify into SectorBench branch + outlook + impact on company."
    )


class AnalyzeRequest(BaseModel):
    domain: Optional[str] = Field(
        default=None,
        description="Company domain or full URL. Examples: 'siemens.com', 'https://www.siemens.com/'. "
                    "Primary input (safest). When given with 'hr_no'/'register_court', the "
                    "crawled Impressum is fact-checked against them (mismatch → 422). Optional — "
                    "if omitted, the request becomes a name/HR-based insolvency + enrichment "
                    "check (no website crawl).",
        examples=["siemens.com", "https://www.boniforce.de"],
    )
    hr_no: Optional[str] = Field(
        default=None,
        description="Handelsregister number, e.g. 'HRA 12345' or 'HRB 67890'. "
                    "Sharpens the insolvency search for exact company disambiguation.",
        examples=["HRA 12345"],
    )
    register_court: Optional[str] = Field(
        default=None,
        description="Registergericht / register city (the Amtsgericht), e.g. 'Stuttgart'. "
                    "Sharpens the insolvency search.",
        examples=["Stuttgart"],
    )
    company_name: Optional[str] = Field(
        default=None,
        description="Authoritative company name. Overrides the name derived from the "
                    "site/Impressum for all enrichment (Tavily, sanctions, branch, insolvency).",
        examples=["Beispiel GmbH"],
    )
    options: Optional[AnalyzeOptions] = Field(
        default=None,
        description="Optional flags to disable expensive enrichment for faster responses.",
    )


# ---- Domain normalisation ------------------------------------------------

_BARE_DOMAIN_RX = re.compile(r"^(?:https?://)?([\w.-]+\.[a-z]{2,})(?:[:/].*)?$", re.I)


def normalise_url(value: str) -> str:
    """Accept 'siemens.com', 'www.siemens.com', 'https://siemens.com/...' and produce a full URL."""
    raw = (value or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty domain.")
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw.lstrip("/")
    parsed = urlparse(raw)
    if not parsed.netloc or "." not in parsed.netloc:
        raise HTTPException(status_code=400, detail=f"Not a valid domain: {value!r}")
    # Strip credentials if any
    netloc = parsed.netloc.split("@")[-1].split(":")[0]
    if not _BARE_DOMAIN_RX.match(netloc):
        raise HTTPException(status_code=400, detail=f"Not a valid domain: {value!r}")
    return f"{parsed.scheme}://{netloc}{parsed.path or '/'}"


# ---- Endpoints -----------------------------------------------------------


@app.get("/api/health", tags=["meta"])
async def health() -> dict:
    """Liveness probe. Does NOT validate the API token."""
    return {"status": "ok", "version": API_VERSION}


_MD_LINK_RX = re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)")


def _clean_inline(s: str) -> str:
    """Strip inline Markdown to plain text."""
    if not s:
        return ""
    s = _MD_LINK_RX.sub(r"\1 (\2)", s)
    s = re.sub(r"`+", "", s)
    s = re.sub(r"\*\*|__|\*|_", "", s)
    return s.strip()


def _kv(label: str, value) -> Optional[dict]:
    if not value:
        return None
    if isinstance(value, list):
        value = ", ".join(str(v) for v in value)
    return {"type": "keyvalue", "label": label, "value": str(value)}


def _md_to_blocks(md: str) -> list:
    """Convert a Markdown sub-section (branch outlook, B2B profile) into typed
    plain-text blocks: heading / bullet / paragraph."""
    blocks: list = []
    for raw in (md or "").splitlines():
        ln = raw.strip()
        if not ln:
            continue
        h = re.match(r"^#{1,6}\s*(.+)$", ln)
        if h:
            blocks.append({"type": "heading", "text": _clean_inline(h.group(1))})
            continue
        # a line that is entirely bold → treat as heading
        hb = re.match(r"^\*\*(.+?)\*\*:?$", ln)
        if hb:
            blocks.append({"type": "heading", "text": _clean_inline(hb.group(1))})
            continue
        bl = re.match(r"^[-*•]\s+(.+)$", ln)
        if bl:
            blocks.append({"type": "bullet", "text": _clean_inline(bl.group(1))})
            continue
        blocks.append({"type": "paragraph", "text": _clean_inline(ln)})
    return blocks


def build_text_blocks(result: dict) -> list:
    """Structured plain-text rendering: an ordered list of typed blocks so the
    client decides how to display each (heading/bullet/keyvalue/link/...).
    Block types: title, subtitle, heading, paragraph, bullet,
    keyvalue {label, value}, link {label, url}."""
    e = result.get("extracted") or {}
    B: list = []

    name = e.get("name") or result.get("source_url") or "Unternehmen"
    B.append({"type": "title", "text": name})
    if e.get("tagline"):
        B.append({"type": "subtitle", "text": e["tagline"]})
    if e.get("elevator_pitch"):
        B.append({"type": "paragraph", "text": e["elevator_pitch"]})

    # Insolvency — surfaced first, it is high-stakes risk info.
    ins = ((result.get("enrichment") or {}).get("tavily") or {}).get("insolvency") or {}
    if ins:
        B.append({"type": "heading", "text": "Insolvenz-Check"})
        B.append({"type": "keyvalue", "label": "Insolvenzverfahren (Verdacht, Nachrichten)",
                  "value": "JA" if ins.get("insolvenzverfahren") else "nein"})
        B.append({"type": "keyvalue", "label": "Insolvent (amtlich bestätigt)",
                  "value": "JA" if ins.get("insolvenz") else "nein"})
        B.append({"type": "keyvalue", "label": "Amtlich geprüft (insolvenzbekanntmachungen.de)",
                  "value": "JA" if ins.get("confirmed") else "nein"})
        reg_in = result.get("register_input") or {}
        if reg_in.get("hr_no"):
            B.append({"type": "keyvalue", "label": "Handelsregister", "value": reg_in["hr_no"]})
        if reg_in.get("register_court"):
            B.append({"type": "keyvalue", "label": "Registergericht (Eingabe)", "value": reg_in["register_court"]})
        for ev in (ins.get("evidence") or [])[:3]:
            if ev.get("url"):
                B.append({"type": "link", "label": ev.get("title") or "Beleg", "url": ev["url"]})

    if e.get("what_they_do"):
        B.append({"type": "heading", "text": "Was sie machen"})
        B.append({"type": "paragraph", "text": e["what_they_do"]})

    facts = [b for b in (
        _kv("Branche", e.get("industry")),
        _kv("HQ", e.get("headquarters")),
        _kv("Gegründet", e.get("founded")),
        _kv("Mitarbeiter", e.get("employee_count")),
        _kv("Geschäftsmodell", e.get("business_model")),
        _kv("Sprachen", e.get("languages")),
    ) if b]
    if facts:
        B.append({"type": "heading", "text": "Eckdaten"})
        B += facts

    if e.get("target_customers"):
        B.append({"type": "heading", "text": "Zielkunden"})
        for tc in e["target_customers"]:
            B.append({"type": "bullet", "text": str(tc)})

    ps = e.get("core_products_services") or []
    if ps:
        B.append({"type": "heading", "text": f"Produkte & Services ({len(ps)})"})
        for it in ps:
            label = it.get("name") or "-"
            if it.get("category"):
                label += f" ({it['category']})"
            B.append({"type": "bullet", "text": label})
            if it.get("description"):
                B.append({"type": "paragraph", "text": it["description"]})

    imp = (result.get("impressum") or {}).get("data") if result.get("impressum") else None
    if imp:
        B.append({"type": "heading", "text": "Impressum"})
        addr = ", ".join(p for p in [imp.get("street"), imp.get("postal_code"),
                                     imp.get("city"), imp.get("country")] if p)
        B += [b for b in (
            _kv("Firma", imp.get("company_name")),
            _kv("Adresse", addr),
            _kv("Registergericht", imp.get("register_court")),
            _kv("HRB/HRA", imp.get("register_number")),
            _kv("USt-IdNr.", imp.get("vat_id")),
            _kv("E-Mail", imp.get("email")),
            _kv("Telefon", imp.get("phone")),
        ) if b]

    branch = result.get("branch") or {}
    if branch and not branch.get("error") and branch.get("outlook_markdown"):
        bn = branch.get("branch_name_de") or branch.get("branch_key") or ""
        B.append({"type": "heading", "text": f"Branche & Ausblick - {bn}"})
        B += _md_to_blocks(branch["outlook_markdown"])

    prof = result.get("profile") or {}
    if prof.get("rendered_markdown"):
        B.append({"type": "heading", "text": "B2B-Entscheider-Profil"})
        B += _md_to_blocks(prof["rendered_markdown"])

    return B


@app.post(
    "/api/analyze",
    tags=["analysis"],
    summary="Analyse a company website",
    description=(
        "Submits a domain through the full fast-mode pipeline and returns the "
        "structured analysis synchronously (~15-30 seconds). Includes home-page "
        "extraction, Impressum, optional Tavily/news enrichment, OpenSanctions "
        "check, SectorBench branch outlook, and a German B2B-Entscheider-Profil.\n\n"
        "**Identity check** — pass `hr_no` (Handelsregister number, e.g. 'HRA 12345') "
        "and `register_court` (Amtsgericht / register city, e.g. 'Stuttgart'). With a "
        "`domain`, the crawled Impressum is fact-checked against them — a contradiction "
        "returns **422 `identity_mismatch`** (the site belongs to a different company). "
        "Without a `domain`, the request runs an insolvency + enrichment check on the "
        "company (name resolved from the register if needed); both values also sharpen "
        "the insolvency search and are echoed in `register_input`.\n\n"
        "**Response highlights**\n"
        "- `extracted` — facts (name, products `core_products_services`, etc.).\n"
        "- `enrichment.tavily.insolvency` — German insolvency check, two tiers by source: "
        "`insolvenzverfahren` (NEWS soft signal — reported, not amtlich → investigate) + "
        "`insolvenz` (AMTLICH bestätigt — set only by the official portal "
        "insolvenzbekanntmachungen.de, needs `hr_no`+`register_court`), plus `confirmed` "
        "(portal queried), `source`, and `evidence[]`.\n"
        "- `text` — ordered array of typed plain-text blocks "
        "(`title|subtitle|heading|paragraph|bullet|keyvalue|link`) for direct display.\n\n"
        "**Determinism:** extraction runs at `temperature=0`/`top_p=0` (Gemini, greedy — "
        "no seed support) and synthesis at `temperature=0` + fixed `seed` (OpenAI), so "
        "repeated requests for the same site stay consistent."
    ),
    response_description="Analysis bundle: extracted, impressum, enrichment (incl. insolvency), profile, branch, and text blocks.",
    responses={
        200: {
            "content": {
                "application/json": {
                    "example": {
                        "source_url": "https://www.nill-ritz.de/",
                        "register_input": {"hr_no": "HRA 12345", "register_court": "Stuttgart"},
                        "extracted": {
                            "name": "Nill + Ritz CNC-Technik GmbH",
                            "industry": "Maschinenbau",
                            "core_products_services": [
                                {"name": "Laserbeschriftungsanlage", "category": "Markiersysteme"}
                            ],
                        },
                        "enrichment": {
                            "tavily": {
                                "insolvency": {
                                    "insolvenzverfahren": True,
                                    "insolvenz": False,
                                    "confirmed": False,
                                    "source": "tavily",
                                    "answer": "",
                                    "evidence": [{"title": "...", "url": "https://..."}],
                                }
                            },
                            "sanctions": [],
                        },
                        "text": [
                            {"type": "title", "text": "Nill + Ritz CNC-Technik GmbH"},
                            {"type": "heading", "text": "Insolvenz-Check"},
                            {"type": "keyvalue", "label": "Insolvenzverfahren (Verdacht, Nachrichten)", "value": "JA"},
                            {"type": "keyvalue", "label": "Insolvent (amtlich bestätigt)", "value": "nein"},
                            {"type": "keyvalue", "label": "Amtlich geprüft (insolvenzbekanntmachungen.de)", "value": "nein"},
                            {"type": "link", "label": "Beleg", "url": "https://..."},
                        ],
                        "metrics": {"total_ms": 22900},
                    }
                }
            }
        }
    },
    dependencies=[Depends(require_token)],
)
async def analyze(req: AnalyzeRequest, request: Request) -> dict:
    if not ((req.domain or "").strip() or (req.company_name or "").strip() or (req.hr_no or "").strip()):
        raise HTTPException(
            status_code=400,
            detail="Provide a company website (domain), a Handelsregister number (hr_no), or a company_name.",
        )
    url = normalise_url(req.domain) if (req.domain or "").strip() else ""
    opts = req.options or AnalyzeOptions()

    t0 = time.time()
    try:
        result = await fast_extract(
            url,
            with_profile=opts.with_profile,
            with_enrichment=opts.with_enrichment,
            with_branch=opts.with_branch,
            hr_no=req.hr_no,
            register_court=req.register_court,
            company_name=req.company_name,
        )
    except Exception as exc:  # noqa: BLE001 — surface to client as 500
        return JSONResponse(
            status_code=500,
            content={"error": "internal_error", "detail": str(exc)},
        )

    elapsed_ms = round((time.time() - t0) * 1000)
    result.setdefault("metrics", {})["api_elapsed_ms"] = elapsed_ms

    # If fast_extract reported a structured fetch error, mirror it as HTTP 422
    # (unprocessable entity) so clients can branch easily.
    if result.get("error") and "extracted" not in result:
        return JSONResponse(status_code=422, content=result)

    # Structured plain-text blocks alongside the data JSON.
    result["text"] = build_text_blocks(result)

    return result


@app.exception_handler(HTTPException)
async def http_exc_handler(_request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.detail, "status_code": exc.status_code},
    )
