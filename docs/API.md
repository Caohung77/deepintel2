# deepintel2 Public API

Bearer-token-gated REST API. Send a company domain, receive a structured
analysis: elevator pitch, products & services, Impressum, sector outlook,
B2B-Entscheider-Profil.

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
| `domain` | string | ✓ | — | Bare domain or full URL. `siemens.com`, `https://www.siemens.com/` both accepted. |
| `options.with_profile` | bool | – | `true` | Generate German 4-block B2B-Entscheider-Profil via gpt-4o. |
| `options.with_enrichment` | bool | – | `true` | Tavily competitor/news search + OpenSanctions screening. |
| `options.with_branch` | bool | – | `true` | Classify into SectorBench branch + outlook + impact-on-company. |

#### Minimal call

```bash
curl -X POST https://deepintel.boniforce.de/api/analyze \
  -H "Authorization: Bearer dpi_..." \
  -H "Content-Type: application/json" \
  -d '{"domain":"siemens.com"}'
```

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

### 400 Bad Request — invalid domain

```json
{ "error": "Not a valid domain: 'foo bar baz'", "status_code": 400 }
```

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

No hard limit at the API layer in v0.1.x. Bottleneck is downstream:
OpenAI (gpt-4o) ~10 req/min on tier 1, Gemini ~15 req/s. Tavily free tier
1000 queries/month. Plan calls accordingly or pass `with_enrichment=false`
for high-volume use.

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
