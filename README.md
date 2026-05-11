# deepintel2 — B2B Company Profile Generator (MVP)

End-to-end pipeline that turns a company website URL into a structured B2B
decision-maker profile (German, prompt v4.1.0-b2b-profil).

## Pipeline

1. **Site crawl** (crawl4ai) — extracts overview, products, governance, Impressum.
2. **Tavily** — competitor snippets, recent news (90d), risk events.
3. **Wikidata** — corporate facts (HQ, country, CEOs, ISIN, VAT, register no.).
4. **OpenSanctions** — fuzzy match against EU + OFAC + UN + UK consolidated list.
5. **Synthesis** (gpt-4o) — produces a 4-block German profile.
6. **Validator** — 8 programmatic rules; up to 3 LLM retries with violation feedback.

All free sources. Costs: ~$0.02–0.05 per profile (LLM calls only).

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -U crawl4ai pydantic streamlit rapidfuzz httpx openai python-dotenv
.venv/bin/crawl4ai-setup
```

Set keys in `.env`:

```
OPENAI_API_KEY=sk-...
TAVILY_API_KEY=tvly-...
LLM_PROVIDER=openai/gpt-4o-mini
SYNTHESIS_MODEL=openai/gpt-4o
```

## Usage

### Streamlit UI

```bash
.venv/bin/streamlit run app.py
```

→ http://localhost:8501

8 tabs: Analyse · Summary · Products · Governance · Impressum · Enrichment · Subpages · Raw JSON.

### CLI

```bash
# Full pipeline + B2B profile
.venv/bin/python pipeline.py https://www.example-gmbh.de --out report.json

# Site crawl only
.venv/bin/python pipeline.py https://www.example-gmbh.de --skip-enrichment --no-profile

# Site + enrichment, skip synthesis
.venv/bin/python pipeline.py https://www.example-gmbh.de --no-profile
```

### Standalone module smoke tests

```bash
.venv/bin/python -m enrichment.sanctions "Wagner Group"
.venv/bin/python -m enrichment.tavily_client "Personio SE & Co. KG" personio.com
.venv/bin/python -m enrichment.wikidata "Personio"
.venv/bin/python -m synthesis.validator
```

## Module layout

```
deepintel2/
├── company_extractor.py        Stage 1 (site crawl + LLM extract)
├── pipeline.py                 Orchestrator (CLI entrypoint)
├── app.py                      Streamlit UI
├── enrichment/
│   ├── sanctions.py            OpenSanctions consolidated check
│   ├── tavily_client.py        3-query web search
│   └── wikidata.py             SPARQL corporate facts
├── synthesis/
│   ├── prompt_b2b_v4_1.py      German prompt template
│   ├── validator.py            8-rule output validator
│   └── profile_generator.py    LLM call + retry loop
├── cache/                      Disk caches (sanctions, Tavily, Wikidata)
└── docs/design/MVP_B2B_PROFILE_SPEC.md
```

## Validator rules (German prompt compliance)

| Rule | Check |
|---|---|
| R1 | No sentence > 15 words |
| R2 | No PR adjectives (innovativ, hochwertig, kundenorientiert, …) |
| R3 | No "Daten liegen nicht vor"-style phrases |
| R4 | No redundancy with dashboard (Gründung, Mitarbeiter, Score, Limit) |
| R5 | Format: meta-line + 4 fett headers, exact spacing |
| R6 | All 4 blocks (UNTERNEHMEN, MARKT, RISIKO, FAZIT) present + non-empty |
| R7 | No bullets / dash lists; one paragraph per block |
| R8 | No emojis |

## Limitations (MVP)

- DE/EN companies only (German output language)
- Best-effort Wikidata coverage — small private firms rarely have entries
- Unternehmensregister scrape skipped (captcha)
- No Bundesanzeiger PDF financials, no Creditreform Bonität
- Tavily free tier rate-limit ~1000 queries/month
- Sanctions screening is informational, not legal compliance verdict
- No persistence DB; reports are JSON files

## Troubleshooting

**`OPENAI_API_KEY not set`** → Add to `.env`, restart shell or use `.venv/bin/python` (which auto-loads `.env`).

**`[tavily] TAVILY_API_KEY not set`** → Add to `.env`.

**Sanctions list takes 2 minutes to download first run** → 62MB CSV. Cached 24h after.

**Wikidata SPARQL timeout** → Transient. Pipeline continues without it.

**Validator never passes** → Check `validation.violations` in raw JSON; surface in Analyse tab as warning.

## Licence / compliance note

OpenSanctions data is CC-BY 4.0. Wikidata is CC0. Tavily and OpenAI usage subject to their terms. All source data is publicly accessible; processing limited to B2B due-diligence under GDPR Art. 6(1)(f).
