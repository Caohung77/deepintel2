# MVP Spec — B2B Decision-Maker Profile Generator

**Version:** 0.1
**Date:** 2026-05-10
**Scope:** Free-source MVP. No paid APIs, no Creditreform, no Bundesanzeiger PDFs.
**Output target:** German B2B profile per prompt schema `v4.1.0-b2b-profil`.

---

## 1. Goal

Given a company website URL, produce:
1. Structured JSON record with company data (current scraper output, extended).
2. **`AnalysisProfile`** — 4-block German narrative obeying all style rules from prompt `v4.1.0-b2b-profil`.

Single command. Single CLI run or single Streamlit click.

---

## 2. In Scope (MVP) / Out of Scope

**In scope:**
- Same-domain crawl (already built)
- LLM extraction of overview, products, governance, Impressum (already built)
- Tavily web search enrichment: competitors, news, industry trends
- EU consolidated sanctions list check (CSV/XML download, fuzzy match)
- OFAC SDN list check (CSV download, fuzzy match)
- Handelsregister-OpenSearch (`unternehmensregister.de` HTML, no captcha endpoints only)
- OpenCorporates free-tier lookup (rate-limited, anonymous)
- Synthesis layer (LLM `gpt-4o` + prompt v4.1.0)
- Programmatic validator (sentence length, banned phrases, redundancy ban, format)
- Streamlit tab "Analyse" rendering profile + sources

**Out of scope (future tiers):**
- Creditreform / Bisnode / Bürgel APIs
- Bundesanzeiger PDF Jahresabschluss parser
- LinkedIn scraping
- DPMA / EUIPO patent registries
- Multi-language output (DE only for MVP)
- Authentication, multi-user, persistence DB

---

## 3. Architecture

```
┌─────────────────────────────────────────────────────────┐
│  Stage 1: Site Crawl  (existing)                         │
│  └─ company_extractor.run() → overview, products,        │
│     governance, impressum, subpages                      │
├─────────────────────────────────────────────────────────┤
│  Stage 2: Web Enrichment                                 │
│  ├─ Tavily search "competitors of <name>"                │
│  ├─ Tavily search "<name> Insolvenz | Klage | Skandal"   │
│  └─ Tavily news "<name>" (last 90 days)                  │
├─────────────────────────────────────────────────────────┤
│  Stage 3: Registry Lookup                                │
│  ├─ OpenCorporates search by name + jurisdiction         │
│  └─ Unternehmensregister name match (best-effort)        │
├─────────────────────────────────────────────────────────┤
│  Stage 4: Compliance Check                               │
│  ├─ EU sanctions XML (cached daily)                      │
│  └─ OFAC SDN CSV (cached daily)                          │
│      └─ fuzzy match company_name + represented_by names  │
├─────────────────────────────────────────────────────────┤
│  Stage 5: Synthesis                                      │
│  ├─ Build context bundle (all stages)                    │
│  ├─ LLM call → 4-block German profile                    │
│  └─ Validator → retry on violation (max 3x)              │
├─────────────────────────────────────────────────────────┤
│  Stage 6: Output                                         │
│  ├─ JSON: full structured record                         │
│  └─ Markdown: AnalysisProfile.text + source footnotes    │
└─────────────────────────────────────────────────────────┘
```

Each stage is a coroutine returning a typed dict. Stages 2–4 run concurrently via `asyncio.gather` after Stage 1.

---

## 4. Module Layout

```
deepintel2/
├── company_extractor.py          # Stage 1 (existing, extend)
├── enrichment/
│   ├── __init__.py
│   ├── tavily_client.py          # Stage 2
│   ├── opencorporates.py         # Stage 3
│   ├── unternehmensregister.py   # Stage 3
│   └── sanctions.py              # Stage 4 (EU + OFAC)
├── synthesis/
│   ├── __init__.py
│   ├── profile_generator.py      # Stage 5: LLM call
│   ├── prompt_b2b_v4_1.py        # German prompt template + few-shots
│   └── validator.py              # rule engine
├── pipeline.py                   # orchestrator: run all stages
├── app.py                        # Streamlit (extend, add tab)
├── cache/                        # disk cache for sanctions lists
└── docs/design/MVP_B2B_PROFILE_SPEC.md
```

---

## 5. Data Sources (Free Only)

### 5.1 Tavily (already configured)

- **Endpoint:** `mcp__tavily__tavily-search`, `mcp__tavily__tavily-extract`
- **Queries per company (3 calls):**
  1. `"<company_name>" Wettbewerber OR competitors site:!<own_domain>`
  2. `"<company_name>" (Insolvenz OR Klage OR Skandal OR Rückruf OR Bußgeld)`
  3. `"<company_name>" news` (filter: last 90 days)
- **Output:** list of `{title, url, snippet, published_date}`
- **Cost:** ~$0.005 × 3 = $0.015 per company (Tavily free tier 1000/mo).

### 5.2 EU Consolidated Sanctions List

- **Source:** `https://webgate.ec.europa.eu/fsd/fsf/public/files/xmlFullSanctionsList_1_1/content?token=dG9rZW4tMjAxNw`
- **Format:** XML, ~30 MB
- **Refresh:** daily, cache to `cache/eu_sanctions.xml` with mtime check
- **Match:** RapidFuzz `partial_ratio` on `company_name` and each `represented_by` name. Threshold ≥ 90.
- **Output:** `[{list: "EU", entity, match_score, sanction_type, listed_date}]`

### 5.3 OFAC SDN

- **Source:** `https://www.treasury.gov/ofac/downloads/sdn.csv`
- **Format:** CSV, ~5 MB
- **Refresh:** daily, cache to `cache/ofac_sdn.csv`
- **Match:** same RapidFuzz logic.
- **Output:** `[{list: "OFAC", entity, match_score, program, listed_date}]`

### 5.4 OpenCorporates

- **Endpoint:** `https://api.opencorporates.com/v0.4/companies/search?q=<name>&jurisdiction_code=de`
- **Auth:** anonymous (free tier, ~50 req/day per IP)
- **Output:** `[{name, company_number, jurisdiction, status, incorporation_date, registered_address}]`
- **Use:** verify legal entity exists, capture register number to cross-check Impressum.

### 5.5 Unternehmensregister (best effort)

- **Endpoint:** `https://www.unternehmensregister.de/ureg/search1.0/search.html?submitaction=showDocument&id=...`
- **Reality:** captcha + dynamic JS. MVP attempts simple HTML name search only. If blocked, skip gracefully — log warning, return null.
- **Output:** `[{name, hrb, court, status}]` or null.

---

## 6. Schemas (additions to existing pydantic models)

```python
class CompetitorMention(BaseModel):
    name: str
    url: Optional[str]
    source: str          # "tavily" | "user"
    snippet: Optional[str]

class NewsItem(BaseModel):
    title: str
    url: str
    snippet: Optional[str]
    published_date: Optional[str]
    risk_tag: Optional[str]   # "litigation" | "insolvency" | "scandal" | "general"

class SanctionsHit(BaseModel):
    list_name: str            # "EU" | "OFAC"
    matched_entity: str
    match_score: float
    program: Optional[str]
    listed_date: Optional[str]

class RegistryRecord(BaseModel):
    source: str               # "opencorporates" | "unternehmensregister"
    legal_name: str
    company_number: Optional[str]
    jurisdiction: Optional[str]
    status: Optional[str]
    incorporation_date: Optional[str]
    registered_address: Optional[str]

class EnrichmentBundle(BaseModel):
    competitors: List[CompetitorMention] = []
    news: List[NewsItem] = []
    sanctions: List[SanctionsHit] = []
    registry: List[RegistryRecord] = []

class AnalysisProfile(BaseModel):
    meta_line: str            # "**B2B · ... · ...**"
    unternehmen: str          # 5-6 sentences as one paragraph
    markt: str
    risiko: str
    fazit: str
    rendered_markdown: str    # full assembled output
    sources: List[Dict[str, str]]   # [{claim, source_url}]
    validation: Dict[str, Any]      # {passed: bool, violations: [], retries: int}

class CompanyReport(BaseModel):
    source_url: str
    summary: Optional[Dict]
    products_services: List[Dict]
    governance: Optional[Dict]
    impressum: Optional[Dict]
    subpages: List[Dict]
    enrichment: EnrichmentBundle
    profile: Optional[AnalysisProfile]
```

---

## 7. Synthesis Layer

### 7.1 Prompt template (`prompt_b2b_v4_1.py`)

System prompt and output structure exactly per user-supplied JSON schema `v4.1.0-b2b-profil`. Provided as a Python constant. Not paraphrased.

User prompt = JSON-serialised `CompanyReport` minus the `profile` field, plus an explicit "DATA" header.

### 7.2 LLM call

- **Model:** `openai/gpt-4o` (reasoning needed; `mini` produces too much PR fluff in tests).
- **Temperature:** 0.2
- **Max tokens:** 1500
- **Retry policy:** see validator.

### 7.3 Validator (`validator.py`)

Programmatic rules. Run after each LLM call. On any violation, append the violation list to the user prompt and retry. Hard cap: 3 retries.

| Rule | Check | Failure action |
|---|---|---|
| R1 — Sentence length | Tokenize each block by `[.!?]`, count words excluding markdown. Reject if any sentence >15 words. | Add violation: "Sentence X has Y words." |
| R2 — Banned phrases | Regex `(innovativ\|hochwertig\|qualitativ\|kundenorientiert\|stark\|umfassend\|leistungsstark\|zukunftssicher)` → reject. | Add violation: "Banned phrase: <word>." |
| R3 — Missing-data phrasing | Regex `(liegt(en)? nicht vor\|werden keine Namen\|keine Daten\|nicht bekannt\|nicht ermittelt)` → reject. | Add violation: "Forbidden missing-data phrase." |
| R4 — Redundancy ban | Regex `(gegründet\|seit\s+\d{4}\|Mitarbeiter(zahl)?\|Bonität\|Score\|Kreditlimit)` in narrative blocks → reject. | Add violation: "Redundant fact: <match>." |
| R5 — Format | Must contain exactly 5 lines starting with `**` (meta + 4 headers). Each header alone on its line. Exactly one blank line between blocks. | Add violation: "Format: <detail>." |
| R6 — Block presence | Each of UNTERNEHMEN, MARKT, RISIKO, FAZIT must have ≥1 paragraph. | Add violation: "Empty block: <name>." |
| R7 — No bullets/dashes | Regex `^[\s]*[-•*]\s` per line in narrative → reject. | Add violation: "List/bullet detected." |
| R8 — Emoji ban | Unicode emoji range scan → reject. | Add violation: "Emoji detected." |

If 3 retries fail → return last attempt with `validation.passed=false`. Surface in UI as warning.

### 7.4 Source attribution

After validation passes, run a second small LLM call (`gpt-4o-mini`) that takes the profile text + structured `CompanyReport` and outputs `[{claim, source_url}]`. Render as footnotes in UI. Optional for MVP if budget tight — can ship without.

---

## 8. UI Changes (Streamlit)

Add **two** tabs to `app.py`:

1. **🧠 Analyse** — renders `AnalysisProfile.rendered_markdown` with `st.markdown(..., unsafe_allow_html=False)`. Shows validator badge: ✅ passed / ⚠️ failed-with-warnings.
2. **📰 Enrichment** — three subsections: competitors (table), news (list with date + risk tag), sanctions (red banner if any hit).

Existing tabs unchanged.

Add a "Profile language" select (DE only for MVP — placeholder for later).

Add a "Skip enrichment" checkbox (debug mode, runs Stage 1 only).

---

## 9. Build Phases (estimate: 2–3 days)

| Phase | Deliverable | Effort |
|---|---|---|
| **P1** Sanctions module | `sanctions.py` with EU + OFAC fetch, cache, fuzzy match, unit tests | 3 h |
| **P2** Tavily client | `tavily_client.py` 3-query search + result normaliser | 2 h |
| **P3** OpenCorporates + Registry | `opencorporates.py`, `unternehmensregister.py` (best-effort) | 3 h |
| **P4** Pipeline orchestrator | `pipeline.py` running stages with `asyncio.gather` | 2 h |
| **P5** Prompt template | `prompt_b2b_v4_1.py` constants + 2 few-shot examples | 2 h |
| **P6** Validator | `validator.py` with 8 rules + tests | 3 h |
| **P7** Profile generator | `profile_generator.py` with retry loop | 2 h |
| **P8** UI integration | 2 new Streamlit tabs | 2 h |
| **P9** End-to-end test | 5 real companies (DE GmbH mix), manual review | 2 h |
| **P10** Doc + CLI flag | `--profile` flag on CLI, README update | 1 h |

Total: ~22 h.

---

## 10. Acceptance Criteria

A run on `https://www.personio.com` (or similar mid-size DE GmbH) must produce:

- [ ] `CompanyReport.profile.rendered_markdown` containing all 4 blocks
- [ ] `validation.passed = true`
- [ ] No sentence >15 words anywhere in narrative
- [ ] No banned PR adjectives
- [ ] No "Daten liegen nicht vor"-style phrases
- [ ] No mention of founding year, employee count, or score in narrative blocks
- [ ] At least 1 named competitor in MARKT block (when Tavily returns ≥1)
- [ ] Sanctions check completed (clean or hits, never null)
- [ ] Streamlit "Analyse" tab renders the profile cleanly
- [ ] Total runtime <90 s per company on warm cache

---

## 11. Risks & Mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| Tavily returns weak/junk competitor results | High | Filter snippets containing company own domain; require ≥2 distinct sources per competitor |
| LLM keeps producing PR adjectives despite rules | Medium | Few-shot examples in prompt; up to 3 retries with explicit violation list |
| Sentence-length tokenizer miscounts (abbrev, urls) | Medium | Use spaCy German sentence splitter, not regex |
| Sanctions fuzzy match → false positives on common names | Medium | Require ≥2 of: name + register number + city to confirm hit |
| OpenCorporates rate-limit (50/day anon) | Medium | Cache by `(jurisdiction, name)` for 30 days |
| Unternehmensregister captcha blocks scrape | High | Treat as best-effort; never block pipeline if it fails |
| GPT-4o cost on retries | Low | Hard cap 3 retries; fall back to mini if budget hit |

---

## 12. Open Decisions for User Before Build

1. **GPT-4o vs gpt-4o-mini for synthesis** — 4o is ~10x cost (~$0.05 vs $0.005 per company) but mini fails the no-PR rule in pilot tests. Recommend 4o.
2. **Source attribution second-call** — adds latency + $0.005. Ship without for MVP, add later? Recommend yes (skip).
3. **Cache TTLs** — sanctions 24h, Tavily 7d, OpenCorporates 30d, site crawl 24h. OK?
4. **Failure mode** — if validator never passes after 3 retries, show degraded profile with warning, or hide Analyse tab? Recommend show with warning.
5. **Register secondary domains** — if `personio.de` and `personio.com` both exist, pick which? Recommend `.de` for DE companies (Impressum lives there).

---

## 13. Non-Goals (Explicit)

- No database persistence (in-memory + JSON file output only)
- No auth / multi-user
- No batch mode / queue (one URL at a time)
- No PDF report generation
- No language other than German for synthesis
- No personal data export beyond what is on Impressum (publicly disclosed)

---

## 14. Compliance Note

All sources used are publicly accessible and lawful for B2B due-diligence purposes under GDPR Art. 6(1)(f) (legitimate interest). Personal data extracted is limited to executives/representatives whose names are publicly disclosed via legal notice. No private personal data scraped. Sanctions matching is informational only; not a legal compliance verdict.
