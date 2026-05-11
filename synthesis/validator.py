"""
Programmatic validator for B2B profile output (prompt v4.1.0-b2b-profil).

8 rules. Every violation is reported as a precise message that gets fed back
to the LLM in the retry prompt so it can fix the specific issue.

Public API:
    validate(text) -> ValidationResult(passed: bool, violations: list[str], parsed: dict)
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Dict, List, Optional

# ---------- regex --------------------------------------------------------

BANNED_ADJECTIVES = re.compile(
    r"\b(innovativ\w*|hochwertig\w*|kundenorientiert\w*|"
    r"leistungsstark\w*|zukunftssicher\w*|umfassend\w*|exzellent\w*|hervorragend\w*|"
    r"erstklassig\w*|marktführend\w*|wegweisend\w*|bahnbrechend\w*|"
    r"maßgeschneidert\w*|dynamisch\w*|nachhaltig\w*)\b",
    re.I,
)

MISSING_DATA_PHRASES = re.compile(
    r"(liegen?\s+nicht\s+vor|werden?\s+keine\s+namen|keine\s+(daten|angaben|informationen)\s+(vor|verfügbar)|"
    r"nicht\s+(bekannt|ermittelt|verfügbar|öffentlich)|"
    r"daten\s+fehlen|informationen\s+fehlen)",
    re.I,
)

REDUNDANT_FACTS = re.compile(
    r"\b(gegründet\s+(im\s+jahr|in)|seit\s+\d{4}|im\s+jahr\s+\d{4}\s+gegründet|"
    r"\d+\s+mitarbeiter|mitarbeiterzahl|"
    r"bonitäts?[-_ ]?score|bonitäts?index|bonitäts?note|bonitäts?bewertung\s+(des\s+unternehmens|liegt)|"
    r"kreditlimit\s+(von|liegt)|boni-?score|score\s+(von|liegt))\b",
    re.I,
)

LIST_BULLETS = re.compile(r"^[\s]*[-•*]\s+\S", re.M)

EMOJI_RX = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F000-\U0001F02F"
    "\U0001F0A0-\U0001F0FF"
    "]",
    flags=re.UNICODE,
)

SENTENCE_SPLIT_RX = re.compile(r"(?<=[.!?])\s+(?=[A-ZÄÖÜ])")
HEADER_RX = re.compile(r"^\*\*([^*]+)\*\*\s*$", re.M)

REQUIRED_HEADERS = ["UNTERNEHMEN", "MARKT", "RISIKO", "FAZIT"]


# ---------- result type --------------------------------------------------


@dataclass
class ValidationResult:
    passed: bool
    violations: List[str] = field(default_factory=list)
    parsed: Dict[str, str] = field(default_factory=dict)
    meta_line: Optional[str] = None


# ---------- helpers ------------------------------------------------------


def _strip_md(s: str) -> str:
    """Strip ** for word counting."""
    return re.sub(r"\*+", "", s)


def _word_count(sentence: str) -> int:
    cleaned = _strip_md(sentence).strip()
    if not cleaned:
        return 0
    return len(re.findall(r"\b[\w'-]+\b", cleaned, flags=re.UNICODE))


def _split_sentences(paragraph: str) -> List[str]:
    paragraph = paragraph.replace("\n", " ").strip()
    if not paragraph:
        return []
    parts = SENTENCE_SPLIT_RX.split(paragraph)
    return [p.strip() for p in parts if p.strip()]


def parse_blocks(text: str) -> Dict[str, str]:
    """Extract meta-line + 4 named blocks. Returns {} if structure invalid."""
    text = text.strip()
    lines = text.split("\n")

    meta_idx = None
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s.startswith("**") and s.endswith("**") and "·" in s:
            meta_idx = i
            break
    if meta_idx is None:
        return {}

    out: Dict[str, str] = {"_meta": lines[meta_idx].strip()}
    current_block: Optional[str] = None
    buffer: List[str] = []

    for ln in lines[meta_idx + 1:]:
        s = ln.strip()
        m = HEADER_RX.match(s)
        if m:
            if current_block is not None:
                out[current_block] = "\n".join(buffer).strip()
                buffer = []
            current_block = m.group(1).strip().upper()
            continue
        if current_block is not None:
            buffer.append(ln)
    if current_block is not None:
        out[current_block] = "\n".join(buffer).strip()
    return out


# ---------- rules --------------------------------------------------------


def validate(text: str) -> ValidationResult:
    violations: List[str] = []

    if not text or not text.strip():
        return ValidationResult(False, ["Output ist leer."], {})

    parsed = parse_blocks(text)
    meta = parsed.get("_meta")

    # R5/R6 — Format & block presence
    if not meta:
        violations.append("Format: Meta-Zeile (**B2B/B2C · Produkt · Rolle**) fehlt oder ohne ** umrahmt.")
    else:
        if not re.match(r"^\*\*.+\*\*$", meta):
            violations.append("Format: Meta-Zeile muss mit ** beginnen UND enden.")
        if "·" not in meta:
            violations.append("Format: Meta-Zeile muss '·' als Trenner enthalten.")

    for header in REQUIRED_HEADERS:
        if header not in parsed:
            violations.append(f"Block fehlt: {header}.")
        elif not parsed[header].strip():
            violations.append(f"Block leer: {header}.")

    # If structural integrity broken, return early — running content rules on garbage is noise.
    if violations and ("Block fehlt" in " ".join(violations) or not meta):
        return ValidationResult(False, violations, parsed, meta)

    # R8 — Emoji ban
    if EMOJI_RX.search(text):
        violations.append("Verbot: Emojis im Text.")

    # R7 — No bullet lists
    for m in LIST_BULLETS.finditer(text):
        violations.append(f"Verbot: Aufzählungspunkt/Bindestrich-Liste in Zeile: {m.group(0).strip()[:60]!r}.")
        break  # one is enough

    # Per-block content rules
    for header in REQUIRED_HEADERS:
        block = parsed.get(header, "")
        if not block:
            continue

        # R1 — sentence length ≤15 words
        for sent in _split_sentences(block):
            wc = _word_count(sent)
            if wc > 15:
                violations.append(
                    f"R1 Satzlänge in {header}: {wc} Wörter (max 15). Satz: {sent[:80]!r}."
                )

        # R2 — banned PR adjectives
        for m in BANNED_ADJECTIVES.finditer(block):
            violations.append(f"R2 PR-Adjektiv in {header}: {m.group(0)!r}.")

        # R3 — missing-data phrasing
        for m in MISSING_DATA_PHRASES.finditer(block):
            violations.append(f"R3 Fehlende-Daten-Phrase in {header}: {m.group(0)!r}.")

        # R4 — redundancy ban
        for m in REDUNDANT_FACTS.finditer(block):
            violations.append(f"R4 Redundanz mit Dashboard in {header}: {m.group(0)!r}.")

        # R7b — extra: any line break inside block other than paragraph break
        if "\n\n" in block.strip():
            violations.append(f"R-Format: {header} enthält Absatztrennung — muss EIN Fließtext sein.")
        # heuristic: many short lines suggest sentence-per-line layout
        nonempty_lines = [ln for ln in block.split("\n") if ln.strip()]
        if len(nonempty_lines) >= 3:
            violations.append(
                f"R-Format: {header} hat {len(nonempty_lines)} Zeilen. "
                "Pflicht: Sätze als zusammenhängender Absatz, KEIN Zeilenumbruch nach jedem Satz."
            )

    return ValidationResult(
        passed=len(violations) == 0,
        violations=violations,
        parsed=parsed,
        meta_line=meta,
    )


# ---------- self test ---------------------------------------------------

if __name__ == "__main__":
    GOOD = (
        "**B2B · Cloud-HR-Software für Mittelstand · Marktführer DACH**\n\n"
        "**UNTERNEHMEN**\n"
        "Personio betreibt eine SaaS-Plattform für Personalverwaltung. "
        "Zielgruppe sind Unternehmen mit 10 bis 2.000 Beschäftigten in Europa. "
        "Die Module decken Lohnabrechnung und Bewerbermanagement ab. "
        "Der Umsatz stammt aus monatlichen Abos pro Mitarbeiter. "
        "Die Kundenbasis liegt schwerpunktmäßig in Deutschland und Großbritannien.\n\n"
        "**MARKT**\n"
        "Treiber ist der Druck zur Digitalisierung deutscher HR-Abteilungen. "
        "Direkte Wettbewerber sind HRworks, sage HR und Workday im Mittelstand. "
        "International dringen Rippling und Deel in den europäischen Markt vor.\n\n"
        "**RISIKO**\n"
        "Der Mittelstand reagiert empfindlich auf Konjunkturabkühlungen. "
        "Wachstum bei US-Wettbewerbern setzt die Preisstellung unter Druck. "
        "Eine Datenpanne würde Vertrauen kosten.\n\n"
        "**FAZIT**\n"
        "Die Plattform ist tief in deutschen Mittelstand-Workflows verankert. "
        "Die Konkurrenz aus den USA verlangt schnellere Produktinnovationen. "
        "Eine Übernahme durch einen größeren Anbieter bleibt wahrscheinlich."
    )
    BAD = (
        "**B2B · innovative HR-Lösung · Marktführer**\n\n"
        "**UNTERNEHMEN**\n"
        "Personio bietet eine hochwertige innovative HR-Software mit kundenorientierter "
        "Ausrichtung und maßgeschneiderten Funktionen für moderne Unternehmen weltweit.\n\n"
        "**MARKT**\n"
        "Daten liegen nicht vor.\n\n"
        "**RISIKO**\n"
        "Gegründet im Jahr 2015 mit über 1500 Mitarbeitern weltweit.\n\n"
        "**FAZIT**\n"
        "Innovativ und zukunftssicher."
    )
    for label, sample in [("GOOD", GOOD), ("BAD", BAD)]:
        r = validate(sample)
        print(f"=== {label}: passed={r.passed}, violations={len(r.violations)} ===")
        for v in r.violations:
            print(f"  - {v}")
