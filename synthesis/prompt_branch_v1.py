"""
Prompts for branch classification + branch outlook + impact-on-company.

Two LLM calls:
  1. classify_branch_prompt — pick one of 10 SectorBench branch_keys.
  2. branch_outlook_prompt — write 2 short sections in German:
       - Branchen-Ausblick (factual, sourced from SectorBench news)
       - Auswirkung auf <Firma> (concrete impact on the analysed company)

Style rules mirror v4.1.0-b2b-profil:
  - ≤15 words/sentence
  - no PR adjectives
  - no missing-data filler phrases
  - no redundancy with dashboard (Gründungsjahr, Mitarbeiterzahl, Score)
"""

# Authoritative branch list (must mirror enrichment.sectorbench.BRANCH_KEYS).
BRANCH_OPTIONS = [
    ("automotive", "Automobilindustrie"),
    ("healthcare", "Gesundheitswesen"),
    ("construction", "Bauwirtschaft"),
    ("renewable_energy", "Erneuerbare Energien"),
    ("logistics", "Logistik & Transport"),
    ("fintech", "FinTech & Finanzdienstleistungen"),
    ("it_services", "IT & Informationsdienstleistungen"),
    ("retail", "Einzelhandel"),
    ("hospitality", "Gastgewerbe"),
    ("manufacturing", "Verarbeitendes Gewerbe"),
]


CLASSIFY_SYSTEM = (
    "Du bist ein präziser Branchen-Klassifizierer. "
    "Gegeben sind Fakten über ein Unternehmen. Wähle genau EINE Branche aus der Liste, "
    "die das Kerngeschäft am besten beschreibt. Gib NUR den branch_key zurück, sonst nichts."
)


def classify_branch_prompt(company_data: dict) -> tuple[str, str]:
    """Build (system, user) for branch classification."""
    options = "\n".join(f"  - {key}  =  {name}" for key, name in BRANCH_OPTIONS)
    facts = []
    if company_data.get("name"):
        facts.append(f"Name: {company_data['name']}")
    if company_data.get("industry"):
        facts.append(f"Industry: {company_data['industry']}")
    if company_data.get("elevator_pitch"):
        facts.append(f"Pitch: {company_data['elevator_pitch']}")
    if company_data.get("what_they_do"):
        facts.append(f"What they do: {company_data['what_they_do']}")
    if company_data.get("core_products_services"):
        prods = "; ".join(it.get("name", "") for it in company_data["core_products_services"][:10])
        facts.append(f"Products: {prods}")
    if company_data.get("target_customers"):
        facts.append(f"Customers: {', '.join(company_data['target_customers'])}")
    if company_data.get("business_model"):
        facts.append(f"Business model: {company_data['business_model']}")

    user = (
        f"Wähle EINE Branche für dieses Unternehmen:\n\n"
        f"VERFÜGBARE BRANCHEN:\n{options}\n\n"
        f"UNTERNEHMENSFAKTEN:\n" + "\n".join(facts) + "\n\n"
        "Antworte nur mit dem branch_key (z.B. 'fintech'). Kein Satz, keine Erklärung."
    )
    return CLASSIFY_SYSTEM, user


OUTLOOK_SYSTEM = (
    "Du bist ein erfahrener Branchenanalyst für B2B-Entscheider. "
    "Aus offiziellen Branchen-Daten (SectorBench) und Unternehmensfakten erstellst du "
    "ZWEI kurze Sektionen für den Leser. Der Leser hat wenig Zeit.\n\n"
    "REGELN — STRIKT BEFOLGEN:\n"
    "1. Sachlich, faktenbasiert. KEINE PR-Sprache (innovativ, hochwertig, marktführend).\n"
    "2. Jeder Satz max. 15 Wörter. Lieber zwei kurze Sätze als ein Schachtelsatz.\n"
    "3. Erfinde NICHTS. Nutze ausschließlich gegebene Daten. Lass Information weg, wenn sie fehlt.\n"
    "4. KEINE Floskeln wie 'Daten liegen nicht vor', 'keine Angaben verfügbar'.\n"
    "5. Erwähne NICHT: Gründungsjahr, Mitarbeiterzahl, Bonitäts-Score, Kreditlimit.\n"
    "6. Sektion 1 (BRANCHEN-AUSBLICK) beschreibt die Branche allgemein — nicht das Unternehmen.\n"
    "7. Sektion 2 (AUSWIRKUNG) verbindet Branchentrends konkret mit dem Geschäft des Unternehmens.\n\n"
    "FORMAT — exakt dieses Markdown, ohne Vorwort:\n\n"
    "**BRANCHEN-AUSBLICK**\n"
    "[4-6 Sätze als Fließtext in einem Absatz. Wichtigste aktuelle Treiber, Risiken, kurzfristiger Ausblick.]\n\n"
    "**AUSWIRKUNG AUF [FIRMA]**\n"
    "[3-5 Sätze als Fließtext in einem Absatz. Konkrete Konsequenzen für dieses Unternehmen — "
    "Chancen, Bedrohungen, was zu beobachten ist. Nenne Verknüpfungen explizit "
    "(z.B. 'Da das Unternehmen X verkauft und der Trend Y abnimmt, …').]"
)


def _format_branch_facts(branch_score: dict, branch_news: dict | None) -> str:
    lines: list[str] = []
    name_de = branch_score.get("branch_name_de") or branch_score.get("branch_key")
    lines.append(f"Branche: {name_de} ({branch_score.get('branch_key')})")
    if (cs := branch_score.get("composite_score")) is not None:
        lines.append(f"Composite-Score: {cs} (Skala 0-100)")
    if (rl := branch_score.get("risk_level")):
        lines.append(f"Risk Level: {rl}")
    if (rank := branch_score.get("rank")):
        lines.append(f"Rang (von 10): {rank}")
    dims = branch_score.get("dimensions") or {}
    if dims:
        dim_pairs = [f"{k}={v}" for k, v in dims.items() if v is not None]
        if dim_pairs:
            lines.append("Dimensionen: " + ", ".join(dim_pairs))

    if branch_news:
        if (eo := branch_news.get("executive_overview")):
            lines.append(f"\nExecutive Overview:\n{eo}")
        kd = branch_news.get("key_developments") or []
        if kd:
            lines.append("\nKey Developments:")
            for k in kd[:6]:
                title = k.get("title", "")
                summ = k.get("summary", "")
                imp = k.get("impact", "")
                lines.append(f"- {title}: {summ}  Impact: {imp}")
        if (ia := branch_news.get("impact_assessment")):
            lines.append(f"\nImpact Assessment:\n{ia}")
        rw = branch_news.get("risk_watchlist") or []
        if rw:
            lines.append("\nRisk Watchlist:")
            for r in rw[:5]:
                lines.append(f"- [{r.get('severity', '?')}] {r.get('item', '')}")
        if (nw := branch_news.get("next_week_outlook")):
            lines.append(f"\nNext-Week Outlook:\n{nw}")
    return "\n".join(lines)


def branch_outlook_prompt(company_name: str, company_data: dict,
                          branch_score: dict, branch_news: dict | None) -> tuple[str, str]:
    """Build (system, user) for branch outlook + company impact synthesis."""
    branch_block = _format_branch_facts(branch_score, branch_news)
    company_block = [
        f"Firma: {company_name or '?'}",
    ]
    if company_data.get("elevator_pitch"):
        company_block.append(f"Pitch: {company_data['elevator_pitch']}")
    if company_data.get("what_they_do"):
        company_block.append(f"Was sie tun: {company_data['what_they_do']}")
    if company_data.get("target_customers"):
        company_block.append(f"Zielkunden: {', '.join(company_data['target_customers'])}")
    if company_data.get("business_model"):
        company_block.append(f"Geschäftsmodell: {company_data['business_model']}")
    company_text = "\n".join(company_block)

    sys_msg = OUTLOOK_SYSTEM.replace("[FIRMA]", company_name or "DIESES UNTERNEHMEN")
    user = (
        "BRANCHEN-DATEN (SectorBench, Stand aktuelles Briefing):\n\n"
        f"{branch_block}\n\n"
        "UNTERNEHMENSFAKTEN:\n\n"
        f"{company_text}\n\n"
        "Schreibe JETZT die beiden Sektionen exakt im vorgegebenen Format. "
        "Kein Vorwort, keine Erklärung, kein abschließender Kommentar."
    )
    return sys_msg, user
