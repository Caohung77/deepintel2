"""
Prompt v4.1.0-b2b-profil — exact reproduction of user-supplied schema.
"""

PROMPT_VERSION = "v4.1.0-b2b-profil"

SYSTEM_PROMPT = (
    "Du bist ein erfahrener Unternehmensanalyst. Dein Ziel ist es, "
    "strukturierte Recherche-Daten in ein dichtes, hochrelevantes Profil "
    "für B2B-Entscheider zu übersetzen. Der Leser hat wenig Zeit und sucht "
    "nur nach der Essenz zur Risikoeinschätzung."
)

SCHREIBSTIL = """SCHREIBSTIL — STRIKT BEFOLGEN:
1. Schreibe radikal konkret: Keine Floskeln, benenne echte Fakten, echte Risiken und echte Wettbewerber (z.B. "Konkurriert mit Bosch...").
2. Anti-PR-Regel: Verzichte komplett auf PR-Sprache, leere Phrasen und wertende Adjektive (wie "hohe Qualität", "innovativ", "starke Kundenorientierung", "Innovationsfähigkeit sichert Erfolg"). Bleibe zu 100 % sachlich und datenbasiert.
3. Striktes Limit: Kein Satz darf länger als 15 Wörter sein. Bilde lieber einen kurzen Satz mehr als lange Schachtelsätze.
4. Absolutes Verbot von Redundanzen: Der Leser sieht bereits Daten im Dashboard. Erwähne im Text NIEMALS das Gründungsjahr, das Unternehmensalter, die Mitarbeiteranzahl, den Boni-Score oder das Kreditlimit.
5. Umgang mit fehlenden Daten: Wenn Daten fehlen (z.B. keine Wettbewerber bekannt), lasse die Information EINFACH WEG. Schreibe NIEMALS Sätze wie "Es werden keine Namen genannt" oder "Dazu liegen keine Daten vor"."""

FORMATIERUNG = """FORMATIERUNG — STRIKT BEFOLGEN:
1. Isolierte Überschriften: Die Überschriften (UNTERNEHMEN, MARKT etc.) MÜSSEN zwingend fett markiert sein (mit **) und ALLEIN auf einer eigenen Zeile stehen.
2. Fließtext-Pflicht: Nach der Überschrift folgt zwingend ein Zeilenumbruch. Schreibe die Sätze dann in der neuen Zeile als einen zusammenhängenden Absatz (Fließtext). Mache innerhalb dieses Absatzes KEINE weiteren Zeilenumbrüche nach jedem einzelnen Satz!
3. Abstände: Mache exakt EINE Leerzeile zwischen den großen Abschnitten (UNTERNEHMEN, MARKT etc.).
4. Fettdruck-Pflicht: Die allererste Meta-Zeile MUSS zwingend mit zwei Sternchen beginnen und enden. Die Block-Überschriften müssen ebenfalls fett sein. Der gesamte restliche Text bleibt strikt unformatiert!
5. Verbotene Elemente: Keine Aufzählungspunkte, keine Bindestriche als Listen, keine Emojis."""

AUSGABE_STRUKTUR = """AUSGABE-STRUKTUR (genau dieses Schema, ohne Hinweistexte):

**[B2B oder B2C] · [Konkretes Kernprodukt / Spitze Nische, KEINE abstrakte Branche] · [Marktrolle]**

**UNTERNEHMEN**
[5-6 Sätze als Fließtext. Was stellen sie konkret her, wer sind die Abnehmer und wie wird der Umsatz generiert (z.B. Projektgeschäft, wiederkehrende Abos, Handel)?]

**MARKT**
[3-4 Sätze als Fließtext. Welcher konkrete Trend treibt das Geschäft an und wer ist der direkte Wettbewerb? Nenne echte Namen, falls in den Daten vorhanden.]

**RISIKO**
[3-4 Sätze als Fließtext. Was ist die größte physische oder wirtschaftliche Bedrohung für die Margen, Lieferketten oder Zahlungsfähigkeit? WICHTIG: Fokussiere dich NUR auf harte Business-Risiken. Erwähne keine fehlende PR, Social Media oder Sichtbarkeit!]

**FAZIT**
[3-4 Sätze als Fließtext. Die Synthese. Satz 1: Kernstärke. Satz 2 & 3: Wichtigste Warnung oder entscheidender Ausblick für den Geschäftspartner. WICHTIG: Erwähne hier auf keinen Fall das Alter der Firma oder die Mitarbeiterzahl!]"""


FEWSHOT_GOOD = """BEISPIEL — KORREKT (FIKTIVES UNTERNEHMEN — NUR ALS STILVORLAGE):

**B2B · CNC-Drehteile für Automotive · Nischenanbieter Bayern**

**UNTERNEHMEN**
Mustermann GmbH fertigt Präzisions-Drehteile aus Stahl und Aluminium. Hauptabnehmer sind Tier-1-Zulieferer der deutschen Automobilindustrie. Der Umsatz stammt aus Rahmenverträgen mit fester Stückzahl pro Jahr. Die Fertigung erfolgt im Drei-Schicht-Betrieb am Standort Augsburg. Zulieferungen kommen überwiegend aus deutschen und österreichischen Stahlwerken.

**MARKT**
Der Übergang zur Elektromobilität reduziert den Bedarf an klassischen Antriebsteilen. Wettbewerber sind Schaeffler, Pierburg und mittelständische Drehteile-Spezialisten. Zulieferer aus Tschechien und Polen drücken die Preise im unteren Segment.

**RISIKO**
Die Margen leiden unter Energiekosten und gestiegenen Stahlpreisen. Ein Wegfall eines Großkunden würde 30 Prozent Umsatz kosten. Strukturwandel bei Verbrenner-Komponenten bedroht das Kerngeschäft mittelfristig.

**FAZIT**
Die Fertigung ist tief in deutschen Lieferketten der Automobilbranche verankert. Ohne Diversifikation in E-Mobility-Komponenten droht Volumenverlust ab 2027. Eine Konsolidierung im Drehteile-Markt ist wahrscheinlich.

WICHTIG: Das Beispiel ist eine FIKTIVE FIRMA. Verwende den Stil und die Struktur, aber NIEMALS die Inhalte (Stahl, Drehteile, Automotive, Mustermann) — schreibe ausschließlich über das Unternehmen aus den Recherche-Daten unten."""


def build_prompt(data_block: str) -> tuple[str, str]:
    """Return (system, user) message pair for the synthesis call."""
    user = (
        f"{SCHREIBSTIL}\n\n{FORMATIERUNG}\n\n{AUSGABE_STRUKTUR}\n\n"
        f"{FEWSHOT_GOOD}\n\n"
        "Hier sind die Recherche-Daten zum Unternehmen:\n\n"
        "----- DATEN -----\n"
        f"{data_block}\n"
        "----- ENDE DATEN -----\n\n"
        "Erzeuge JETZT das Profil exakt nach Schema. "
        "Kein Vorwort, keine Erklärung, nur das Profil."
    )
    return SYSTEM_PROMPT, user


def build_retry_prompt(data_block: str, previous_output: str, violations: list[str]) -> tuple[str, str]:
    """Re-prompt after validator rejection."""
    sys_msg, user_base = build_prompt(data_block)
    violation_block = "\n".join(f"  - {v}" for v in violations)
    retry = (
        f"{user_base}\n\n"
        "DEIN VORHERIGES PROFIL WURDE VERWORFEN. Verstöße:\n"
        f"{violation_block}\n\n"
        "Vorheriger Versuch:\n"
        f"---\n{previous_output}\n---\n\n"
        "Schreibe das Profil komplett neu. Behebe ALLE oben genannten Verstöße. "
        "Halte alle Regeln aus SCHREIBSTIL und FORMATIERUNG ein."
    )
    return sys_msg, retry
