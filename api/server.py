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
API_VERSION = "0.2.2"
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
    domain: str = Field(
        ...,
        description="Company domain or full URL. Examples: 'siemens.com', 'https://www.siemens.com/'.",
        examples=["siemens.com", "https://www.boniforce.de"],
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


def _md_kv(lines: list, label: str, value) -> None:
    if value:
        if isinstance(value, list):
            value = ", ".join(str(v) for v in value)
        lines.append(f"- **{label}:** {value}")


def render_markdown(result: dict) -> str:
    """Human-readable Markdown rendering of the analysis bundle, returned in the
    `markdown` field next to the structured JSON."""
    e = result.get("extracted") or {}
    L: list = []

    name = e.get("name") or result.get("source_url") or "Unternehmen"
    L.append(f"# {name}")
    if e.get("tagline"):
        L.append(f"*{e['tagline']}*")
    if e.get("elevator_pitch"):
        L += ["", e["elevator_pitch"]]

    # Insolvency — surfaced first, it is high-stakes risk info.
    ins = ((result.get("enrichment") or {}).get("tavily") or {}).get("insolvency") or {}
    if ins:
        L += ["", "## ⚖️ Insolvenz-Check"]
        L.append(f"- **Insolvenzverfahren (laufend):** {'JA 🔴' if ins.get('insolvenzverfahren') else 'nein 🟢'}")
        L.append(f"- **Insolvent:** {'JA 🔴' if ins.get('insolvenz') else 'nein 🟢'}")
        for ev in (ins.get("evidence") or [])[:3]:
            if ev.get("url"):
                L.append(f"  - Beleg: [{ev.get('title') or ev['url']}]({ev['url']})")

    if e.get("what_they_do"):
        L += ["", "## Was sie machen", e["what_they_do"]]

    facts: list = []
    _md_kv(facts, "Branche", e.get("industry"))
    _md_kv(facts, "HQ", e.get("headquarters"))
    _md_kv(facts, "Gegründet", e.get("founded"))
    _md_kv(facts, "Mitarbeiter", e.get("employee_count"))
    _md_kv(facts, "Geschäftsmodell", e.get("business_model"))
    _md_kv(facts, "Sprachen", e.get("languages"))
    if facts:
        L += ["", "## Eckdaten"] + facts

    if e.get("target_customers"):
        L += ["", "## Zielkunden", ", ".join(e["target_customers"])]

    ps = e.get("core_products_services") or []
    if ps:
        L += ["", f"## Produkte & Services ({len(ps)})"]
        for it in ps:
            line = f"- **{it.get('name') or '—'}**"
            if it.get("category"):
                line += f" — _{it['category']}_"
            L.append(line)
            if it.get("description"):
                L.append(f"  {it['description']}")

    imp = (result.get("impressum") or {}).get("data") if result.get("impressum") else None
    if imp:
        L += ["", "## Impressum"]
        _md_kv(L, "Firma", imp.get("company_name"))
        addr = ", ".join(p for p in [imp.get("street"), imp.get("postal_code"),
                                     imp.get("city"), imp.get("country")] if p)
        _md_kv(L, "Adresse", addr)
        _md_kv(L, "Registergericht", imp.get("register_court"))
        _md_kv(L, "HRB/HRA", imp.get("register_number"))
        _md_kv(L, "USt-IdNr.", imp.get("vat_id"))
        _md_kv(L, "E-Mail", imp.get("email"))
        _md_kv(L, "Telefon", imp.get("phone"))

    branch = result.get("branch") or {}
    if branch and not branch.get("error") and branch.get("outlook_markdown"):
        bn = branch.get("branch_name_de") or branch.get("branch_key") or ""
        L += ["", f"## Branche & Ausblick — {bn}", branch["outlook_markdown"]]

    prof = result.get("profile") or {}
    if prof.get("rendered_markdown"):
        L += ["", "## B2B-Entscheider-Profil", prof["rendered_markdown"]]

    return "\n".join(L).strip()


@app.post(
    "/api/analyze",
    tags=["analysis"],
    summary="Analyse a company website",
    description=(
        "Submits a domain through the full fast-mode pipeline and returns the "
        "structured analysis synchronously (~15-30 seconds). Includes home-page "
        "extraction, Impressum, optional Tavily/news enrichment, OpenSanctions "
        "check, SectorBench branch outlook, and a German B2B-Entscheider-Profil."
    ),
    response_description="Analysis bundle: extracted facts, impressum, enrichment, profile, branch.",
    dependencies=[Depends(require_token)],
)
async def analyze(req: AnalyzeRequest, request: Request) -> dict:
    url = normalise_url(req.domain)
    opts = req.options or AnalyzeOptions()

    t0 = time.time()
    try:
        result = await fast_extract(
            url,
            with_profile=opts.with_profile,
            with_enrichment=opts.with_enrichment,
            with_branch=opts.with_branch,
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

    # Markdown rendering alongside the structured JSON.
    result["markdown"] = render_markdown(result)

    return result


@app.exception_handler(HTTPException)
async def http_exc_handler(_request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.detail, "status_code": exc.status_code},
    )
