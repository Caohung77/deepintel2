# deepintel2 Public API

Bearer-token-gated REST API. Send a company **domain** (or, when there is no
website, a **Handelsregister number + court**) and receive a structured analysis:
elevator pitch, products & services, Impressum, insolvency check, sector outlook,
B2B-Entscheider-Profil. When a domain is sent together with Handelsregister data,
the Impressum is fact-checked against it (mismatch → 422).

- **Base URL (production):** `https://deepintel.boniforce.de`
- **Base URL (local dev):** `http://localhost:8000`
- **Authentication:** `Authorization: Bearer <DEEPINTEL_API_TOKEN>`
- **Content-Type:** `application/json`
- **Interactive Swagger UI:** `https://deepintel.boniforce.de/api/docs`
- **OpenAPI JSON:** `https://deepintel.boniforce.de/api/openapi.json`

## Endpoints

### `GET /api/health`

Unauthenticated liveness probe.

```bash
curl https://deepintel.boniforce.de/api/health
# → {"status":"ok","version":"0.1.2"}
```

### `POST /api/analyze`

Synchronous analysis. Returns the full structured bundle. Typical latency
**15–30 seconds** depending on which optional stages are enabled.

#### Request

| Field | Type | Required | Default | Notes |
|---|---|---|---|---|
| `domain` | string | –¹ | `null` | Bare domain or full URL. `siemens.com`, `https://www.siemens.com/` both accepted. Primary input. Omit it for a Handelsregister-/name-based insolvency check (no website crawl). |
| `hr_no` | string | –¹ | `null` | Handelsregister number, e.g. `HRA 12345`. With a `domain` → the crawled Impressum is fact-checked against it (mismatch → 422). Without a `domain` → resolves the company for the insolvency search. Also sharpens the insolvency query; echoed in `register_input`. |
| `register_court` | string | – | `null` | Registergericht / register city (the Amtsgericht), e.g. `Stuttgart`. Used together with `hr_no` for the fact-check / resolution and to sharpen the insolvency query. |
| `company_name` | string | –¹ | `null` | Optional. Overrides the auto-derived name for enrichment; usable as the search subject when no `domain`/`hr_no` is given. |
| `options.with_profile` | bool | – | `true` | Generate German 4-block B2B-Entscheider-Profil via gpt-4o. |
| `options.with_enrichment` | bool | – | `true` | Tavily competitor/news search + OpenSanctions screening. |
| `options.with_branch` | bool | – | `true` | Classify into SectorBench branch + outlook + impact-on-company. |

¹ **At least one** of `domain`, `hr_no`, or `company_name` is required (else HTTP 400).

#### Minimal call

```bash
curl -X POST https://deepintel.boniforce.de/api/analyze \
  -H "Authorization: Bearer dpi_..." \
  -H "Content-Type: application/json" \
  -d '{"domain":"siemens.com"}'
```

#### Identity fact-check (domain + Handelsregister)

The normal payload — `domain` plus `hr_no` + `register_court`. The crawled Impressum's register number + court are checked against the supplied values:

```bash
curl -X POST https://deepintel.boniforce.de/api/analyze \
  -H "Authorization: Bearer dpi_..." \
  -H "Content-Type: application/json" \
  -d '{"domain":"https://www.nill-ritz.de/","hr_no":"HRB 206341","register_court":"Stuttgart"}'
```

- **match** → full analysis; response carries `identity_match.verified = true` + the site's `site_register`.
- **contradiction** → **HTTP 422**, `error_kind = "identity_mismatch"`, no company data — the site belongs to a different company.
- **Impressum has no register data** → analysis proceeds with `identity_match.verified = null` (could not confirm).

#### Without a website — insolvency + enrichment

No `domain`? The company is checked **without crawling any site** (it likely has none). Supply `hr_no` (+ `register_court`); a name is resolved from the register so the search has a subject:

```bash
curl -X POST https://deepintel.boniforce.de/api/analyze \
  -H "Authorization: Bearer dpi_..." \
  -H "Content-Type: application/json" \
  -d '{"hr_no":"HRB 206341","register_court":"Stuttgart"}'
```

Runs Tavily insolvency + competitors/news + sanctions. `metrics.name_only = true`. A `company_name` may be supplied instead of (or alongside) `hr_no`. If nothing can be resolved → HTTP 422 `error_kind = "unresolved"`. No domain + no name + no hr_no → HTTP 400.

`identity_match` (present whenever `hr_no`/`register_court` were checked against a crawled Impressum):

| Field | Type | Meaning |
| --- | --- | --- |
| `verified` | bool/null | `true` = confirmed; `false` = contradicted (→ 422); `null` = no register data on site. |
| `checks` | object | Per-field result `{hr_no, register_court}` — `true`/`false`/`null`. |
| `site_register` | object | What the site's Impressum states (`register_number`, `register_court`). |
| `reason` | string | Human-readable verdict. |

#### Fast variant (skip expensive stages)

```bash
curl -X POST https://deepintel.boniforce.de/api/analyze \
  -H "Authorization: Bearer dpi_..." \
  -H "Content-Type: application/json" \
  -d '{
    "domain":"siemens.com",
    "options":{
      "with_profile": false,
      "with_enrichment": false,
      "with_branch": false
    }
  }'
```

Latency drops to ~5 s. Returns extracted + impressum only.

## Response

### 200 OK

Successful analysis. Fields are stable; consumers should ignore unknown keys.

The response carries both the structured data **and** a `text` field — an ordered array of typed plain-text blocks (no Markdown) so the client decides how to render each one. Block types:

| `type` | Fields | Meaning |
| --- | --- | --- |
| `title` | `text` | Company name (top). |
| `subtitle` | `text` | Tagline. |
| `heading` | `text` | Section heading (e.g. "Insolvenz-Check", "Impressum"). |
| `paragraph` | `text` | Free text paragraph. |
| `bullet` | `text` | A list item. |
| `keyvalue` | `label`, `value` | A labelled fact (e.g. label "Insolvent", value "nein"). |
| `link` | `label`, `url` | A source/reference link. |

```jsonc
"text": [
  { "type": "title", "text": "Nill + Ritz CNC-Technik GmbH" },
  { "type": "paragraph", "text": "Nill+Ritz stellt vollautomatische Markierstationen ..." },
  { "type": "heading", "text": "Insolvenz-Check" },
  { "type": "keyvalue", "label": "Insolvenzverfahren (laufend)", "value": "JA" },
  { "type": "keyvalue", "label": "Insolvent", "value": "nein" },
  { "type": "link", "label": "Breit gefächerte Insolvenzen ...", "url": "https://..." }
]
```

Use the structured JSON for machine processing, the `text` blocks for display.

```jsonc
{
  "source_url": "https://siemens.com/",
  "register_input": null,            // echo of supplied hr_no/register_court, or null. e.g. {"hr_no":"HRA 12345","register_court":"Stuttgart"}
  "identity_match": null,            // present only when hr_no/court were checked vs the Impressum; see table below
  "text": [ /* ordered typed plain-text blocks — see "text" section below */ ],
  "extracted": {
    "name": "Siemens AG",
    "tagline": "Technology to transform the everyday",
    "elevator_pitch": "Siemens AG entwickelt Automatisierungs-, Digitalisierungs- und Energietechnik für Industrie und Infrastruktur.",
    "what_they_do": "...",                           // 4-7 sentences, German
    "industry": "Industrieautomatisierung",
    "headquarters": "München, Deutschland",
    "founded": "1847",
    "employee_count": "...",
    "website": "https://siemens.com",
    "languages": ["de", "en"],
    "business_model": "Projektgeschäft, Lizenzen, Wartungsverträge",
    "target_customers": ["...", "..."],
    "core_products_services": [
      { "name": "...", "description": "...", "category": "..." }
    ],
    "key_claims": ["..."]
  },
  "impressum": {
    "url": "https://www.siemens.com/de/de/general/legal.html",
    "data": {
      "company_name": "Siemens AG",
      "street": "Werner-von-Siemens-Straße 1",
      "postal_code": "80333",
      "city": "München",
      "country": "Deutschland",
      "represented_by": ["Roland Busch", "Ralf P. Thomas", "..."],
      "phone": "+49 89 ...",
      "email": "...",
      "register_court": "Amtsgericht München",
      "register_number": "HRB 6684",
      "vat_id": "DE 129273398",
      "responsible_for_content": "..."
    }
  },
  "enrichment": {
    "tavily": {
      "competitor_snippets": [ { "title":"...", "url":"...", "snippet":"..." } ],
      "news": [ { "title":"...", "url":"...", "published_date":"2026-04-..." } ],
      "risk_events": [],
      "insolvency": {                                // German insolvency check
        "insolvenzverfahren": true,                  // proceeding currently active (vorläufig/eröffnet)
        "insolvenz": false,                          // already insolvent / concluded / liquidated
        "answer": "Tavily's free-text summary",      // returned, but NOT used for the booleans (may contradict them)
        "evidence": [ { "title":"...", "url":"..." } ]// supporting sources (court Bekanntmachung, registers)
      }
    },
    "sanctions": []                                  // hits if name on OpenSanctions
  },
  "branch": {
    "branch_key": "manufacturing",
    "branch_name_de": "Verarbeitendes Gewerbe",
    "branch_name_en": "Manufacturing (General)",
    "branch_score": {
      "composite_score": 53.1,
      "risk_level": "medium",
      "confidence": "high",
      "rank": 5,
      "dimensions": { "financial_health": 52.3, "market_dynamics": 42.9, "..." : "..." }
    },
    "branch_news": { "executive_overview": "...", "key_developments": [], "..." : "..." },
    "outlook_markdown": "**BRANCHEN-AUSBLICK**\n...\n\n**AUSWIRKUNG AUF Siemens AG**\n..."
  },
  "profile": {
    "prompt_version": "v4.1.0-b2b-profil",
    "rendered_markdown": "**B2B · ... · ...**\n\n**UNTERNEHMEN**\n...\n\n**MARKT**\n...\n\n**RISIKO**\n...\n\n**FAZIT**\n...",
    "meta_line": "**B2B · ... · ...**",
    "blocks": {
      "UNTERNEHMEN": "...", "MARKT": "...", "RISIKO": "...", "FAZIT": "..."
    },
    "validation": { "passed": true, "violations": [], "attempts": 1 }
  },
  "metrics": {
    "fetch_ms": 230,
    "home_llm_ms": 5800,
    "impressum_llm_ms": 1400,
    "enrichment_ms": 4100,
    "profile_ms": 9200,
    "branch_ms": 8400,
    "total_ms": 22900,
    "api_elapsed_ms": 22950
  }
}
```

### Insolvency check (`enrichment.tavily.insolvency`)

Returned inside the standard `POST /api/analyze` response (no separate endpoint). Requires `options.with_enrichment = true` (the default); with enrichment disabled the booleans stay `false` and `evidence` is empty.

**Disambiguation:** supply request fields `hr_no` (Handelsregister number) and `register_court` (Amtsgericht) to pin the search to the exact entity — they are woven into the insolvency query (`Ist die Firma "X" (HRA 12345, Amtsgericht Stuttgart) insolvent?`). Recommended when the company name is common across multiple cities. Echoed back in `register_input` + the Insolvenz-Check `text` blocks.

| Field | Type | Meaning |
| --- | --- | --- |
| `insolvenzverfahren` | bool | An insolvency **proceeding is currently active** — preliminary or opened (`vorläufiger Insolvenzverwalter`, `Insolvenzverfahren eröffnet`, `Sicherungsmaßnahmen`, court `Az. n IN n/yy`). |
| `insolvenz` | bool | Company is **already insolvent / proceeding concluded / liquidated** (`ist insolvent`, `zahlungsunfähig`, `liquidiert`, `aufgelöst`). |
| `answer` | string | Tavily's free-text summary of the insolvency question. Returned for context but **not** used to derive the booleans — it can contradict them (it echoes the dominant search narrative), so rely on the booleans + `evidence`, not this. |
| `evidence` | array | Up to 3 supporting sources `{title, url}` (court Bekanntmachungen, registers, news) for human verification. |

Typical states:
- Healthy: `{"insolvenzverfahren": false, "insolvenz": false}`
- In proceeding: `{"insolvenzverfahren": true, "insolvenz": false}`
- Already insolvent / wound up: `{"insolvenzverfahren": true, "insolvenz": true}`

**How it works:** a Tavily search (`search_depth=advanced`, `time_range=year`) asks whether the company is insolvent; signals are attributed to the queried company by court-record proximity, so other firms co-listed on multi-company insolvency pages do not produce false positives.

**Caveat:** this is a screening signal, not a legal record. The official `insolvenzbekanntmachungen.de` portal is not crawlable, so detection relies on third-party republishers — recall is strong but not guaranteed, and filings older than ~12 months may be missed. Always check `evidence[]` before acting. For authoritative status use a credit-register source.

### 422 Unprocessable Entity — unreachable or empty site

```bash
curl -X POST https://deepintel.boniforce.de/api/analyze \
  -H "Authorization: Bearer dpi_..." \
  -H "Content-Type: application/json" \
  -d '{"domain":"www.credozeitarbeit.de"}'
```

```json
{
  "source_url": "https://www.credozeitarbeit.de/",
  "final_url": "https://www.credozeitarbeit.de/",
  "error": "Leider ist keine Analyse möglich, da die Firmenwebseite nicht erreichbar ist oder nicht genügend relevante Informationen liefert.",
  "error_kind": "empty",
  "error_detail": "body 0 chars",
  "http_status": 200,
  "metrics": { "fetch_ms": 716 }
}
```

Clients should check for the `error` field at the top level (without `extracted`) before consuming the rest.

### 422 Unprocessable Entity — Handelsregister mismatch / unresolved

Domain's Impressum contradicts the supplied `hr_no`/`register_court` (wrong company), or no company could be resolved from register data alone:

```json
{
  "source_url": "https://www.example-gmbh.de/",
  "error": "Supplied Handelsregister number / register court do not match the website's Impressum (no match) — the site likely belongs to a different company.",
  "error_kind": "identity_mismatch",
  "identity_match": {
    "verified": false,
    "reason": "register mismatch",
    "checks": { "hr_no": false, "register_court": false },
    "site_register": { "register_number": "HRB 6684", "register_court": "Amtsgericht München" }
  },
  "register_input": { "hr_no": "HRB 99999", "register_court": "Hamburg" }
}
```

(`error_kind = "unresolved"` is returned when only register data was given and no company could be resolved from it.)

### 400 Bad Request — invalid or missing identifier

```json
{ "error": "Not a valid domain: 'foo bar baz'", "status_code": 400 }
```

Also returned when none of `domain` / `hr_no` / `company_name` is supplied.

### 401 / 403 — missing or wrong token

```json
{ "error": "Invalid API token.", "status_code": 403 }
```

### 500 — internal error

```json
{ "error": "internal_error", "detail": "..." }
```

## Auth setup

The server expects `DEEPINTEL_API_TOKEN` in its environment.
Generate a token:

```bash
python -c "import secrets; print('dpi_' + secrets.token_urlsafe(32))"
```

Add it to the operator's `.env` and restart the container:

```bash
nano /root/deepintel/.env
docker compose -f /root/deepintel/docker-compose.yml restart
```

Distribute the token to each consuming SaaS as a shared secret. Rotate by
issuing a new token and revoking the old one (overwrite env, restart).

## Rate limiting

No hard limit at the API layer in v0.2.x. Bottleneck is downstream:
OpenAI (gpt-4o) ~10 req/min on tier 1, Gemini ~15 req/s. Tavily free tier
1000 queries/month. Plan calls accordingly or pass `with_enrichment=false`
for high-volume use.

## Determinism / reproducibility

The analysis is tuned for **consistent results across repeated requests** for the
same site (so e.g. `core_products_services` doesn't reshuffle between calls):

- **Extraction (Gemini)** runs at `temperature=0` + `top_p=0` (greedy decoding),
  and the prompt instructs the model to list products/services in page order
  without reordering. Note: Gemini's OpenAI-compatible endpoint does **not**
  accept a `seed` parameter, so determinism relies on greedy decoding.
- **Synthesis (OpenAI gpt-4o — profile & branch outlook)** runs at
  `temperature=0` + `top_p=1` + a fixed `seed` (server env `LLM_SEED`, default
  `7`), which OpenAI honours for reproducible output.

Caveat: this makes repeats highly stable (verified identical in testing) but not
a 100% hard guarantee — Gemini retains minor server-side nondeterminism, and a
site whose content changes will naturally yield different results. There is no
caching layer for the extraction itself; every request re-runs the models.

## Common integration patterns

### Python (httpx)

```python
import httpx

token = "dpi_..."
r = httpx.post(
    "https://deepintel.boniforce.de/api/analyze",
    headers={"Authorization": f"Bearer {token}"},
    json={"domain": "siemens.com"},
    timeout=60.0,
)
r.raise_for_status()
data = r.json()
print(data["extracted"]["elevator_pitch"])
```

### Node.js (fetch)

```javascript
const res = await fetch("https://deepintel.boniforce.de/api/analyze", {
  method: "POST",
  headers: {
    "Authorization": `Bearer ${process.env.DEEPINTEL_API_TOKEN}`,
    "Content-Type": "application/json",
  },
  body: JSON.stringify({ domain: "siemens.com" }),
});
if (!res.ok) throw new Error(`HTTP ${res.status}`);
const data = await res.json();
console.log(data.extracted.elevator_pitch);
```

### n8n

Use the **HTTP Request** node:

- Method: `POST`
- URL: `https://deepintel.boniforce.de/api/analyze`
- Authentication: `Generic Credential Type` → `Header Auth`
  - Header name: `Authorization`
  - Header value: `Bearer dpi_...`
- Body Content Type: `JSON`
- JSON Body: `{"domain": "{{ $json.domain }}"}`
- Response Format: `JSON`
- Timeout: `60000` ms
