"""
Streamlit UI for the full B2B profile pipeline.

Architecture:
  Streamlit writes jobs/<id>.req.json. A separate WORKER process started
  by run_app.sh BEFORE Streamlit polls for them, runs the pipeline, and
  writes jobs/<id>.out.json. Streamlit polls for the result.

  This decoupling is required because spawning Playwright/Chromium from
  a process that already initialised Apple's Network.framework (Streamlit's
  HTTP server) crashes (SIGSEGV) on macOS 26 due to a broken atfork handler.

Run:
  ./run_app.sh
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parent
JOBS_DIR = Path(os.environ.get("DEEPINTEL_JOBS_DIR", "/tmp/deepintel2_jobs"))
JOBS_DIR.mkdir(parents=True, exist_ok=True)
WORKER_LOG = Path("/tmp/deepintel2_logs/worker.log")


def submit_job(url: str, *, mode: str = "fast",
               max_pages: int = 60, max_product_pages: int = 6,
               with_profile: bool = True, skip_enrichment: bool = False) -> str:
    job_id = uuid.uuid4().hex[:12]
    spec = {
        "url": url,
        "mode": mode,
        "max_pages": max_pages,
        "max_product_pages": max_product_pages,
        "with_profile": with_profile,
        "skip_enrichment": skip_enrichment,
    }
    req_path = JOBS_DIR / f"{job_id}.req.json"
    tmp = req_path.with_suffix(req_path.suffix + ".tmp")
    tmp.write_text(json.dumps(spec), encoding="utf-8")
    tmp.replace(req_path)
    return job_id


def poll_job(job_id: str, timeout_s: int = 600) -> tuple[dict | None, str]:
    """Block on a job until done or timeout. Returns (report, log_or_error)."""
    status_path = JOBS_DIR / f"{job_id}.status"
    out_path = JOBS_DIR / f"{job_id}.out.json"
    err_path = JOBS_DIR / f"{job_id}.err"

    placeholder = st.empty()
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        status = status_path.read_text(encoding="utf-8").strip() if status_path.exists() else "queued"
        log_tail = ""
        try:
            log_tail = WORKER_LOG.read_text(encoding="utf-8", errors="ignore").splitlines()[-3:]
            log_tail = "\n".join(log_tail)
        except OSError:
            pass
        placeholder.markdown(f"**Status:** `{status}`\n\n```\n{log_tail}\n```")

        if status == "done" and out_path.exists():
            placeholder.empty()
            try:
                report = json.loads(out_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as e:
                return None, f"Failed to read result: {e}"
            # Cleanup
            for p in (out_path, status_path, err_path):
                try:
                    p.unlink()
                except OSError:
                    pass
            return report, "ok"

        if status == "error":
            placeholder.empty()
            err = err_path.read_text(encoding="utf-8") if err_path.exists() else "(no detail)"
            for p in (status_path, err_path):
                try:
                    p.unlink()
                except OSError:
                    pass
            return None, err

        time.sleep(1.5)

    placeholder.empty()
    return None, f"Timeout after {timeout_s}s. Worker may still be running — check {WORKER_LOG}."


PID_FILE = JOBS_DIR / "worker.pid"


def worker_alive() -> bool:
    """Check whether worker daemon is running using its pid file."""
    if not PID_FILE.exists():
        return False
    try:
        pid = int(PID_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    try:
        os.kill(pid, 0)  # signal 0 = check existence, no-op if alive
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False

st.set_page_config(page_title="B2B Company Profile", page_icon="🏢", layout="wide")

st.title("🏢 B2B Company Profile Generator")
st.caption("Crawl4AI · Tavily · Wikidata · OpenSanctions · GPT-4o synthesis")

# ---- input ----------------------------------------------------------------
mode = st.radio(
    "Mode",
    options=["fast", "full"],
    index=0,
    format_func=lambda m: {
        "fast": "⚡ Fast (~5s) — landing page only, rich extract",
        "full": "🔬 Full B2B profile (~90s) — crawl + enrichment + synthesis",
    }[m],
    horizontal=True,
)

with st.form("scrape_form"):
    col_url, col_btn = st.columns([4, 1])
    with col_url:
        url = st.text_input("Company website URL", placeholder="https://www.example-gmbh.de")
    with col_btn:
        submitted = st.form_submit_button("Run", type="primary", use_container_width=True)

    if mode == "full":
        with st.expander("Advanced options (full mode)"):
            c1, c2, c3 = st.columns(3)
            with c1:
                max_pages = st.slider("Max pages to crawl", 10, 150, 60, step=10)
            with c2:
                max_product_pages = st.slider("Max product pages", 1, 20, 6)
            with c3:
                with_profile = st.toggle("Generate B2B profile (gpt-4o)", value=True)
                skip_enrichment = st.toggle("Skip enrichment (debug)", value=False)
    else:
        max_pages = 60
        max_product_pages = 6
        with_profile = True
        skip_enrichment = False

if "report" not in st.session_state:
    st.session_state.report = None

if not worker_alive():
    st.error(
        "⚠️ Worker daemon not running. The pipeline runs in a separate worker process.\n\n"
        "Start the app via `./run_app.sh` (which launches the worker before Streamlit). "
        "Do NOT start with `streamlit run app.py` directly."
    )

if submitted and url:
    if not worker_alive():
        st.error("Cannot submit job: worker is not running.")
    else:
        with st.status(f"Submitting {mode} job to worker…", expanded=True) as status:
            st.write(f"Target: `{url}`  ·  mode: `{mode}`")
            job_id = submit_job(
                url,
                mode=mode,
                max_pages=max_pages,
                max_product_pages=max_product_pages,
                with_profile=with_profile,
                skip_enrichment=skip_enrichment,
            )
            st.write(f"Job ID: `{job_id}`")
            report, log = poll_job(job_id, timeout_s=60 if mode == "fast" else 600)
            if report is None:
                status.update(label="Pipeline failed", state="error")
                st.error("Worker reported failure:")
                st.code(log[-4000:], language="text")
            else:
                st.session_state.report = report
                status.update(label="Done ✅", state="complete")


# ---- helpers --------------------------------------------------------------


def _unwrap(section: Any) -> Any:
    if not section:
        return None
    if isinstance(section, dict) and "data" in section:
        d = section["data"]
        if isinstance(d, list) and d:
            return d[0]
        return d
    return section


def _kv(label: str, value: Any) -> None:
    if value is None or value == "" or value == []:
        return
    if isinstance(value, list):
        value = ", ".join(str(v) for v in value)
    st.markdown(f"**{label}:** {value}")


# ---- render ---------------------------------------------------------------
report = st.session_state.report

if report and ("extracted" in report or report.get("error_kind")):
    # ===================== Fast mode rendering =====================
    e = report.get("extracted") or {}
    metrics = report.get("metrics") or {}

    if report.get("error"):
        kind = report.get("error_kind", "unknown")
        st.error(f"### ❌ Keine Daten verfügbar\n\n{report['error']}")
        with st.container(border=True):
            c1, c2 = st.columns(2)
            with c1:
                _kv("URL", report.get("source_url"))
                _kv("Final URL nach Redirect", report.get("final_url"))
                _kv("HTTP Status", report.get("http_status"))
            with c2:
                _kv("Fehler-Typ", kind)
                _kv("Detail", report.get("error_detail"))
                if metrics.get("fetch_ms"):
                    _kv("Fetch-Zeit", f"{metrics['fetch_ms']} ms")
        st.info(
            "Mögliche Ursachen je nach Fehler-Typ:\n"
            "- **dns**: Domain existiert nicht (Tippfehler? Domain abgelaufen?).\n"
            "- **connect / timeout**: Server offline oder Firewall blockt.\n"
            "- **http_status**: Server antwortet mit Fehler (z.B. 403, 500).\n"
            "- **empty / no_content**: Parking-Domain, leere Konfiguration, oder reine JS-SPA "
            "ohne Server-Side-Rendering.\n"
            "- **non_html**: URL zeigt direkt auf eine Datei oder API."
        )
    else:
        col1, col2 = st.columns([3, 1])
        with col1:
            st.subheader(e.get("name") or "Company")
            if e.get("tagline"):
                st.caption(e["tagline"])
        with col2:
            if metrics.get("total_ms"):
                st.metric("Time", f"{metrics['total_ms'] / 1000:.1f}s")

        # ----- B2B Analyse-Profil (top of page) -----
        profile = report.get("profile") or {}
        if profile.get("rendered_markdown"):
            v = profile.get("validation", {})
            attempts = v.get("attempts", "?")
            with st.container(border=True):
                st.markdown("### 🧠 B2B-Entscheider-Profil")
                if v.get("passed"):
                    st.caption(f"✅ Validator passed (attempt {attempts}, prompt v4.1.0)")
                else:
                    st.warning(f"⚠️ Validator failed after {attempts} attempts — degraded output.")
                    with st.expander(f"Violations ({len(v.get('violations', []))})"):
                        for vio in v.get("violations", []):
                            st.markdown(f"- {vio}")
                st.markdown(profile["rendered_markdown"])
            st.divider()

        # ----- Branch outlook -----
        branch = report.get("branch") or {}
        if branch and not branch.get("error"):
            with st.container(border=True):
                st.markdown(f"### 🏭 Branche & Ausblick — {branch.get('branch_name_de', branch.get('branch_key'))}")
                bs = branch.get("branch_score") or {}
                badge_cols = st.columns(4)
                with badge_cols[0]:
                    if bs.get("composite_score") is not None:
                        st.metric("Composite Score", f"{bs['composite_score']:.0f} / 100")
                with badge_cols[1]:
                    if bs.get("rank"):
                        st.metric("Rang", f"#{bs['rank']} / 10")
                with badge_cols[2]:
                    if bs.get("risk_level"):
                        st.metric("Risk Level", bs["risk_level"])
                with badge_cols[3]:
                    if bs.get("confidence"):
                        st.metric("Confidence", bs["confidence"])
                if branch.get("outlook_markdown"):
                    st.markdown(branch["outlook_markdown"])
                with st.expander("Branchen-Rohdaten (SectorBench)"):
                    if branch.get("branch_news"):
                        st.markdown("**Executive Overview:**")
                        st.write(branch["branch_news"].get("executive_overview", "—"))
                        kd = branch["branch_news"].get("key_developments") or []
                        if kd:
                            st.markdown("**Key Developments:**")
                            for it in kd:
                                st.markdown(f"- **{it.get('title','')}** — {it.get('summary','')}")
                    st.markdown("**Dimensions:**")
                    st.json(bs.get("dimensions", {}))
            st.divider()
        elif branch and branch.get("error"):
            st.warning(f"Branch analysis: {branch['error']}")

        if e.get("elevator_pitch"):
            st.info(e["elevator_pitch"])

        if e.get("what_they_do"):
            st.markdown("### Was machen sie")
            st.write(e["what_they_do"])

        cols = st.columns(3)
        with cols[0]:
            _kv("Industry", e.get("industry"))
            _kv("HQ", e.get("headquarters"))
        with cols[1]:
            _kv("Founded", e.get("founded"))
            _kv("Employees", e.get("employee_count"))
        with cols[2]:
            _kv("Business model", e.get("business_model"))
            _kv("Languages", e.get("languages"))

        if e.get("target_customers"):
            st.markdown("### Zielkunden")
            st.markdown(" · ".join(f"`{t}`" for t in e["target_customers"]))

        if e.get("core_products_services"):
            st.markdown(f"### Produkte & Services ({len(e['core_products_services'])})")
            for it in e["core_products_services"]:
                with st.container(border=True):
                    st.markdown(f"**{it.get('name', '—')}**")
                    if it.get("category"):
                        st.caption(it["category"])
                    if it.get("description"):
                        st.write(it["description"])

        if e.get("key_claims"):
            with st.expander(f"Konkrete Aussagen von der Seite ({len(e['key_claims'])})"):
                for c in e["key_claims"]:
                    st.markdown(f"- {c}")

        # ----- Impressum -----
        imp_block = report.get("impressum")
        if imp_block and imp_block.get("data"):
            st.markdown("### ⚖️ Impressum")
            imp = imp_block["data"]
            c1, c2 = st.columns(2)
            with c1:
                _kv("Firma", imp.get("company_name"))
                addr_parts = [imp.get("street"), imp.get("postal_code"), imp.get("city"), imp.get("country")]
                addr = ", ".join(p for p in addr_parts if p)
                if addr:
                    _kv("Adresse", addr)
                _kv("Vertretungsberechtigte", imp.get("represented_by"))
                _kv("V.i.S.d.P.", imp.get("responsible_for_content"))
                _kv("Aufsichtsbehörde", imp.get("supervisory_authority"))
            with c2:
                _kv("Telefon", imp.get("phone"))
                _kv("E-Mail", imp.get("email"))
                _kv("Registergericht", imp.get("register_court"))
                _kv("HRB / HRA", imp.get("register_number"))
                _kv("USt-IdNr.", imp.get("vat_id"))
                _kv("Steuernummer", imp.get("tax_number"))
            if imp_block.get("url"):
                st.caption(f"Quelle: [{imp_block['url']}]({imp_block['url']})")
        elif imp_block:
            st.warning("Impressum-Seite gefunden, aber keine Felder extrahiert.")
        else:
            st.info("Kein Impressum gefunden.")

        with st.expander("Raw JSON + metrics"):
            st.json(report)
            st.download_button(
                "Download report.json",
                data=json.dumps(report, indent=2, ensure_ascii=False, default=str),
                file_name="report.json",
                mime="application/json",
            )

elif report:
    # ===================== Full mode rendering =====================
    summary = _unwrap(report.get("summary"))
    impressum = _unwrap(report.get("impressum"))
    governance = _unwrap(report.get("governance"))
    catalog_pages = report.get("products_services") or []
    subpages = report.get("subpages") or []
    enrichment = report.get("enrichment") or {}
    profile = report.get("profile") or {}

    tabs = st.tabs([
        "🧠 Analyse",
        "📋 Summary",
        "📦 Products & Services",
        "👥 Governance",
        "⚖️ Impressum",
        "📰 Enrichment",
        "🔗 Subpages",
        "🧾 Raw JSON",
    ])

    # -------- Analyse --------
    with tabs[0]:
        if not profile or "rendered_markdown" not in profile:
            st.info("Profile not generated. Run with 'Generate B2B profile' enabled.")
        else:
            v = profile.get("validation", {})
            attempts = v.get("attempts", "?")
            if v.get("passed"):
                st.success(f"Profile generated. Validator passed (attempt {attempts}).")
            else:
                st.warning(f"Validator did not pass after {attempts} attempts. Showing best-effort output.")
                with st.expander(f"Validator violations ({len(v.get('violations', []))})"):
                    for vi in v.get("violations", []):
                        st.markdown(f"- {vi}")
            st.divider()
            st.markdown(profile.get("rendered_markdown", ""))
            st.caption(f"Prompt: `{profile.get('prompt_version', '?')}`")

    # -------- Summary --------
    with tabs[1]:
        if summary:
            st.subheader(summary.get("name") or "Company")
            if summary.get("tagline"):
                st.caption(summary["tagline"])
            st.write(summary.get("summary") or "_No summary extracted._")
            st.divider()
            c1, c2 = st.columns(2)
            with c1:
                _kv("Industry", summary.get("industry"))
                _kv("Founded", summary.get("founded"))
                _kv("Headquarters", summary.get("headquarters"))
            with c2:
                _kv("Employees", summary.get("employee_count"))
                _kv("Website", summary.get("website"))
                _kv("Languages", summary.get("languages"))
        else:
            st.info("No summary extracted.")

    # -------- Products --------
    with tabs[2]:
        total = 0
        for page in catalog_pages:
            data = _unwrap(page)
            items = (data or {}).get("items") if isinstance(data, dict) else None
            if not items:
                continue
            st.markdown(f"**Source:** [{page['url']}]({page['url']})")
            for it in items:
                total += 1
                with st.container(border=True):
                    name = it.get("name", "—")
                    typ = it.get("type", "")
                    st.markdown(f"**{name}**  ·  _{typ}_")
                    if it.get("category"):
                        st.caption(f"Category: {it['category']}")
                    if it.get("description"):
                        st.write(it["description"])
                    if it.get("url"):
                        st.markdown(f"[Open]({it['url']})")
        if total == 0:
            st.info("No products/services extracted.")
        else:
            st.success(f"{total} items across {len(catalog_pages)} pages.")

    # -------- Governance --------
    with tabs[3]:
        if governance and any(governance.get(k) for k in ("executives", "board_members", "ownership", "parent_company")):
            for label, key in [("Executives", "executives"), ("Board members", "board_members")]:
                people = governance.get(key)
                if people:
                    st.subheader(label)
                    for p in people:
                        st.markdown(f"- **{p.get('name', '—')}** — {p.get('role') or '—'}")
            _kv("Ownership", governance.get("ownership"))
            _kv("Parent company", governance.get("parent_company"))
        else:
            st.info("No governance data extracted.")

    # -------- Impressum --------
    with tabs[4]:
        if impressum:
            c1, c2 = st.columns(2)
            with c1:
                st.subheader("Entity")
                _kv("Company", impressum.get("company_name"))
                addr = ", ".join(p for p in [impressum.get("street"), impressum.get("postal_code"), impressum.get("city"), impressum.get("country")] if p)
                if addr:
                    _kv("Address", addr)
                _kv("Represented by", impressum.get("represented_by"))
                _kv("Responsible for content", impressum.get("responsible_for_content"))
            with c2:
                st.subheader("Contact & Registration")
                _kv("Phone", impressum.get("phone"))
                _kv("Email", impressum.get("email"))
                _kv("Website", impressum.get("website"))
                _kv("Register court", impressum.get("register_court"))
                _kv("Register number", impressum.get("register_number"))
                _kv("VAT ID", impressum.get("vat_id"))
                _kv("Tax number", impressum.get("tax_number"))
            src = report.get("impressum", {}).get("url")
            if src:
                st.caption(f"Source: {src}")
        else:
            st.warning("No Impressum/legal-notice page found. Data left empty to avoid hallucination.")

    # -------- Enrichment --------
    with tabs[5]:
        # Sanctions
        sanctions = enrichment.get("sanctions") or []
        if sanctions:
            st.error(f"⚠️ {len(sanctions)} sanctions hit(s)")
            for h in sanctions:
                with st.container(border=True):
                    st.markdown(f"**{h['matched_entity']}**  ·  list: `{h['list_name']}`  ·  score: {h['match_score']:.0f}")
                    if h.get("matched_alias"):
                        st.caption(f"matched alias: {h['matched_alias']}")
                    if h.get("countries"):
                        st.caption(f"countries: {h['countries']}")
                    if h.get("program"):
                        with st.expander("Programme detail"):
                            st.write(h["program"][:1000])
        else:
            st.success("No sanctions hits.")

        st.divider()

        # Wikidata
        st.subheader("Wikidata")
        wd = enrichment.get("wikidata") or []
        if wd:
            top = wd[0]
            c1, c2 = st.columns(2)
            with c1:
                _kv("Legal name", top.get("legal_name") or top.get("name"))
                _kv("Country", top.get("country"))
                _kv("Headquarters", top.get("headquarters"))
                _kv("Industries", top.get("industries"))
            with c2:
                _kv("Inception", top.get("incorporation_date"))
                _kv("CEOs", top.get("ceos"))
                _kv("Founders", top.get("founders"))
                _kv("Parents", top.get("parents"))
                _kv("ISIN", top.get("isin"))
                _kv("Register no.", top.get("register_number"))
                _kv("VAT ID", top.get("vat_id"))
            if top.get("qid"):
                st.caption(f"Wikidata: https://www.wikidata.org/wiki/{top['qid']}")
        else:
            st.info("No Wikidata match.")

        st.divider()

        # Tavily news + competitors
        tavily = enrichment.get("tavily") or {}
        st.subheader("Competitor search snippets")
        cs = tavily.get("competitor_snippets") or []
        if cs:
            for it in cs[:6]:
                with st.container(border=True):
                    st.markdown(f"**[{it['title']}]({it['url']})**")
                    if it.get("snippet"):
                        st.caption(it["snippet"])
        else:
            st.info("No competitor data.")

        st.subheader("Recent news (last 90 days)")
        news = tavily.get("news") or []
        if news:
            for it in news[:8]:
                tag = it.get("risk_tag")
                tag_md = f"  ·  ⚠️ `{tag}`" if tag else ""
                with st.container(border=True):
                    st.markdown(f"**[{it['title']}]({it['url']})**{tag_md}")
                    if it.get("published_date"):
                        st.caption(it["published_date"])
                    if it.get("snippet"):
                        st.write(it["snippet"])
        else:
            st.info("No recent news.")

        st.subheader("Risk-event mentions")
        risks = tavily.get("risk_events") or []
        if risks:
            for it in risks[:6]:
                with st.container(border=True):
                    st.markdown(f"**[{it['title']}]({it['url']})**  ·  ⚠️ `{it.get('risk_tag')}`")
                    if it.get("snippet"):
                        st.write(it["snippet"])
        else:
            st.success("No litigation/insolvency/scandal signals detected.")

    # -------- Subpages --------
    with tabs[6]:
        st.write(f"**{len(subpages)} pages discovered**")
        for p in subpages:
            title = p.get("title") or "(no title)"
            st.markdown(f"- [{title}]({p['url']})")

    # -------- Raw JSON --------
    with tabs[7]:
        st.json(report)
        st.download_button(
            "Download report.json",
            data=json.dumps(report, indent=2, ensure_ascii=False, default=str),
            file_name="report.json",
            mime="application/json",
        )

else:
    st.info("Enter a company URL above and click **Run**.")
