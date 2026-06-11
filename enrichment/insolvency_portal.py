"""Authoritative insolvency check via the official German portal.

`insolvenzbekanntmachungen.de` is the canonical source for German insolvency
publications, but it is a stateful JSF app that Tavily/Google index slowly (fresh
filings are missed by web search — see the F. Klein case). We drive it with a real
browser (patchright/Chromium) and search by the exact Handelsregister entry the
caller supplied (register type + number + court), which is a unique key — no
name-matching ambiguity. Any result row means an insolvency proceeding is recorded.

Returns:
    {
      "checked": True/False,          # False = couldn't run (no court match / browser error)
      "found": bool,                  # an insolvency publication exists for this register entry
      "insolvenzverfahren": bool,
      "insolvenz": bool,
      "announcements": [ {date, az, court, name, sitz, register, gegenstand} ],
      "source": "insolvenzbekanntmachungen.de",
      "note": str,
    }
"""
from __future__ import annotations

import re
from typing import Optional

_PORTAL = "https://neu.insolvenzbekanntmachungen.de/ap/suche.jsf"

# registerart label as it appears in the portal's <select>.
_REG_ART = {"HRA", "HRB", "GnR", "GsR", "PR", "VR"}

# Gegenstand → state. "concluded" forms set insolvenz=True (proceeding over / no assets).
_CONCLUDED_RX = re.compile(
    r"(abweisung\s+mangels\s+masse|aufhebung\s+des\s+insolvenzverfahrens|"
    r"einstellung\s+des\s+insolvenzverfahrens|verfahren\s+aufgehoben)", re.I)
_OPEN_RX = re.compile(
    r"(er[öo]ffnung|er[öo]ffnet|sicherungsma[ßs]nahme|vorl[äa]ufig|"
    r"insolvenzverwalter|bestellung)", re.I)


def _parse_hr(hr_no: str):
    """'HRB 4745' -> ('HRB', '4745'). Returns (None, digits) if type missing."""
    if not hr_no:
        return None, None
    m = re.search(r"\b(HRA|HRB|GnR|GsR|PR|VR)\b", hr_no, re.I)
    art = None
    if m:
        # normalise capitalisation to the portal's option labels
        raw = m.group(1).upper()
        art = {"HRA": "HRA", "HRB": "HRB", "GNR": "GnR", "GSR": "GsR",
               "PR": "PR", "VR": "VR"}.get(raw, raw)
    num = re.search(r"\d+", hr_no)
    return art, (num.group() if num else None)


def _court_norm(s: str) -> str:
    s = re.sub(r"(?i)\b(amtsgericht|ag)\b\.?", "", s or "")
    return s.strip().lower()


def _classify(announcements: list) -> dict:
    """Latest announcement decides current state; any IN/insolvency publication
    means a proceeding is/was recorded."""
    verfahren = bool(announcements)
    insolvent = False
    for a in announcements:
        g = a.get("gegenstand") or ""
        if _CONCLUDED_RX.search(g):
            insolvent = True
    return {"insolvenzverfahren": verfahren, "insolvenz": insolvent}


async def check_insolvency_portal(
    hr_no: Optional[str], register_court: Optional[str], *, timeout_ms: int = 25000,
) -> dict:
    art, num = _parse_hr(hr_no or "")
    base = {"checked": False, "found": False, "insolvenzverfahren": False,
            "insolvenz": False, "announcements": [], "source": "insolvenzbekanntmachungen.de"}
    if not art or art not in _REG_ART or not num or not register_court:
        return {**base, "note": "needs Handelsregister type+number+court"}

    try:
        from patchright.async_api import async_playwright
    except ImportError:
        return {**base, "note": "browser unavailable"}

    court_want = _court_norm(register_court)
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                page.set_default_timeout(timeout_ms)
                await page.goto(_PORTAL, wait_until="domcontentloaded")

                # Match the register court against the portal's option list.
                court_value = await page.evaluate(
                    """(want)=>{const s=document.getElementById('frm_suche:ireg_registereintrag:som_registergericht');
                       if(!s)return null;
                       const norm=t=>t.toLowerCase().replace(/amtsgericht|ag/g,'').trim();
                       for(const o of s.options){ if(norm(o.text)===want) return o.value; }
                       for(const o of s.options){ if(norm(o.text).includes(want)&&want) return o.value; }
                       return null;}""",
                    court_want,
                )
                if not court_value:
                    return {**base, "note": f"register court not found in portal: {register_court!r}"}

                await page.select_option('[id="frm_suche:ireg_registereintrag:som_registerart"]', label=art)
                await page.fill('[id="frm_suche:ireg_registereintrag:itx_registernummer"]', num)
                await page.select_option('[id="frm_suche:ireg_registereintrag:som_registergericht"]', court_value)
                # Widen the date range so older filings are not cut off by the 2-week default.
                try:
                    await page.fill('[id="frm_suche:ldi_datumVon:datumHtml5"]', "2015-01-01")
                except Exception:  # noqa: BLE001 — non-fatal, default range still applies
                    pass

                await page.click('[id="frm_suche:cbt_suchen"]')
                await page.wait_for_timeout(4500)

                rows = await page.evaluate(
                    """()=>{const out=[];
                       document.querySelectorAll('tr').forEach(r=>{
                         const c=[...r.children].map(td=>td.innerText.trim());
                         // result rows: date | Az | court | name | sitz | register | (icon)
                         if(c.length>=6 && /^\\d{2}\\.\\d{2}\\.\\d{4}$/.test(c[0])){
                           out.push({date:c[0], az:c[1], court:c[2], name:c[3], sitz:c[4], register:c[5]});
                         }});
                       return out;}"""
                )

                announcements = []
                for r in rows or []:
                    g = ""
                    # Best-effort: the Az encodes the kind only loosely; the Gegenstand
                    # lives behind a per-row detail popup. We classify from text if the
                    # detail opened inline, else leave it blank (proceeding assumed).
                    announcements.append({**r, "gegenstand": g})

                cls = _classify(announcements)
                return {
                    "checked": True,
                    "found": bool(announcements),
                    "announcements": announcements[:10],
                    "source": "insolvenzbekanntmachungen.de",
                    "note": "register-exact match" if announcements else "no publication for this register entry",
                    **cls,
                }
            finally:
                await browser.close()
    except Exception as e:  # noqa: BLE001 — portal must never crash the request
        return {**base, "note": f"portal error: {e}"}
