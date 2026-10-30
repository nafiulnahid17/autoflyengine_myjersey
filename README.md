# My Jersey Autofly Engine

Lightweight, private orchestration service that runs the existing Artboard AI workflow in one click. It does **not** contain vectorization, image generation, or OpenRouter provider logic, and it does not require any change to the Artboard AI repository.

## What it does

- Accepts a written brief and delegates Artboard AI generation.
- Accepts separate front/back files and delegates the complete Artboard job.
- Accepts one combined image, asks Artboard AI to identify exactly one front and one back panel, and proceeds only when the result is unambiguous.
- Polls Artboard job status and exposes a simplified Autofly status.
- Proxies result bundles and allowlisted files without exposing the Artboard token.
- Resumes monitoring confirmed remote jobs after an Autofly restart.
- Refuses unsafe automatic retries when a job-creation response is ambiguous.
- Routes Quality and Cost-Effective modes to separate Artboard deployments when configured.
- Uses Artboard AI analysis to choose direct source tracing for flat artwork or AI flat-art reconstruction for mockups/photos. An uncertain decision stops safely for manual Artboard review.

## Important contract

Autofly never edits or imports the Artboard AI codebase. It uses only these existing private endpoints:

- `POST /api/engine/generate`
- `POST /api/engine/sources`
- `POST /api/engine/sources/{id}/detect-panels?ai=true`
- `POST /api/engine/sources/{id}/analyze?ai=true`
- `POST /api/engine/jobs`
- `GET /api/engine/jobs/{id}`
- `POST /api/engine/jobs/{id}/cancel`
- `GET /api/engine/jobs/{id}/bundle`
- `GET /api/engine/jobs/{id}/files/{filename}`

One combined image must visibly contain both front and back. If Artboard AI cannot suggest exactly one of each, Autofly stops and asks the user to upload separate panels or continue in Artboard AI. It never guesses a panel assignment.

## Quality and Cost-Effective modes

The current Artboard API does not accept a per-job model tier. Autofly therefore implements truthful mode routing through optional dedicated Artboard deployment URLs:

- `ARTBOARD_QUALITY_URL` - Artboard deployment configured with premium OpenRouter models.
- `ARTBOARD_COST_URL` - Artboard deployment configured with value models.
- `ARTBOARD_ENGINE_URL` - required default/fallback deployment.

If dedicated URLs are not configured, both choices use the default Artboard deployment. `/api/autofly/capabilities` reports this clearly; the service never claims that the model tier changed when it did not.

## API

### Health and capabilities

```text
GET /api/healthz
GET /api/autofly/capabilities
```

### From a written brief

```http
POST /api/autofly/jobs/from-brief
Authorization: Bearer <AUTOFLY_ENGINE_TOKEN>
Content-Type: application/json

{
  "brief": "Create a black and cyan geometric jersey design",
  "front_notes": "Small crest area on the left chest",
  "back_notes": "Leave space for player name and number",
  "mode": "quality",
  "dpi": 330,
  "formats": ["svg", "eps"]
}
```

### From separate front and back files

Multipart fields:

- `front`: front panel file
- `back`: back panel file
- `options_json`: JSON matching `ProcessingOptions`

```text
POST /api/autofly/jobs/from-panels
```

Example `options_json`:

```json
{
  "mode": "quality",
  "dpi": 600,
  "formats": ["svg", "eps"],
  "native_ai_mode": "disabled",
  "reconstruction_mode": "auto",
  "run_ocr": true
}
```

### From one combined front/back image

Multipart fields:

- `file`: combined image
- `options_json`: same options as above

```text
POST /api/autofly/jobs/from-combined
```

### Status and outputs

```text
GET  /api/autofly/jobs/{job_id}
POST /api/autofly/jobs/{job_id}/cancel
GET  /api/autofly/jobs/{job_id}/bundle
GET  /api/autofly/jobs/{job_id}/files/{filename}
```

## Local development

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install '.[test]'
cp .env.example .env
pytest
python -m autofly_engine.server
```

The server binds to localhost by default. A non-loopback bind fails unless `AUTOFLY_ENGINE_TOKEN` is configured.

## Railway configuration

Set at minimum:

```text
AUTOFLY_HOST=0.0.0.0
AUTOFLY_ENGINE_TOKEN=<long-random-private-token>
ARTBOARD_ENGINE_URL=<private Artboard base URL>
ARTBOARD_ENGINE_TOKEN=<Artboard engine token>
```

Railway supplies `PORT`. `railpack.json` installs the package and starts the server.

Use separate Railway services or deployments for Quality and Cost-Effective Artboard model environments if real tier separation is required. They may use the same unchanged Artboard repository with different environment variables.

## Security and reliability

- Keep all tokens server-side.
- Put the service behind the authenticated My Jersey backend; the engine token is not user authentication.
- Inputs are forwarded to Artboard and are not permanently stored by Autofly.
- Local storage contains job metadata only.
- Downloads are proxied; the private Artboard token never reaches the browser.
- Automatic retries are limited to status polling. Ambiguous job submissions are not repeated because that could create duplicate paid work.
- Artboard outputs remain review-required according to the existing Artboard contract.

## Current boundary

Autofly is a safe one-click orchestrator, not a replacement for Artboard AI. The existing Artboard engine still owns artwork generation, reconstruction, OCR, vectorization, validation, SVG/EPS creation, and native Illustrator handoff.

