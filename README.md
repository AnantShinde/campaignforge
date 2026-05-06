# CampaignForge

**Automated social ad campaign creative generation — brief in, brand-compliant assets out.**

A global consumer goods company launches hundreds of localised social ad campaigns every month across dozens of markets. Today this is done manually — briefs, agencies, revisions, legal review, approvals — taking weeks per campaign. CampaignForge replaces that pipeline with a fully automated system: submit a JSON brief, get three production-ready images and localised copy in under 30 seconds.

---

## What it does

Submit a campaign brief → the system generates:

- **Localised ad copy** — headline, body, CTA in the target language
- **3 aspect-ratio images** — 1:1 (Instagram), 9:16 (TikTok/Reels), 16:9 (YouTube)
- **Two-pass compliance report** — pre-generation text check + post-generation vision check
- **Approval workflow** — campaigns land in `pending_review` for human sign-off

```
POST /brief  →  campaign_id
                    │
            ┌───────▼────────┐
            │  SQS + Lambda  │  (async, max 10 concurrent)
            └───────┬────────┘
                    │
            ┌───────▼──────────────────────────────────────┐
            │  GenerateCampaign Lambda                      │
            │                                              │
            │  CopyGen (GPT-4o mini) ─┐                    │
            │                         ├─ parallel          │
            │  Pass 1 Compliance  ────┘                    │
            │       │                                      │
            │  Pass 1 fail? → compliance_blocked (stop)    │
            │       │                                      │
            │  Imagen 4 Fast (3 ratios concurrently) ──┐   │
            │  Text Overlay (Pillow) ──────────────────┘   │
            │  Pass 2 Compliance (vision, GPT-4o mini)      │
            │  PersistAssets → DynamoDB + S3               │
            └──────────────────────────────────────────────┘
                    │
            GET /campaigns/{id}  →  images + copy + compliance report
```

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                        AWS us-east-1                        │
│                                                             │
│  API Gateway (HTTP v2)  ←──  Cognito JWT Auth               │
│         │                                                   │
│    Lambda (×4 API)                                          │
│    SubmitBrief · GetCampaigns · UpdateApproval · GetInsights│
│         │                                                   │
│    SQS Standard Queue  (NFR6: idempotent retries)           │
│         │  maxConcurrency=10                                │
│         ▼                                                   │
│    Lambda: GenerateCampaign  (300s, 1024 MB)                │
│         │                                                   │
│    ┌────┴──────────────┐                                    │
│    │  OpenAI GPT-4o mini│  copy gen + 2-pass compliance     │
│    │  Google Imagen 4  │  image generation (3 ratios)       │
│    │  Pillow           │  text overlay                      │
│    └────┬──────────────┘                                    │
│         │                                                   │
│    S3 (outputs)  ─── DynamoDB (CampaignTable)               │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

### Infrastructure stacks (AWS CDK, Python)

| Stack | Resources |
|---|---|
| `Secrets` | Secrets Manager — OpenAI key, Google AI key |
| `Storage` | S3 ×3 (rag-docs, assets-input, outputs) + DynamoDB |
| `Pipeline` | SQS + GenerateCampaign Lambda + ESM |
| `Api` | HTTP API v2 + Cognito User Pool + 4 API Lambdas |

---

## Tech stack

| Layer | Technology |
|---|---|
| IaC | AWS CDK (Python) |
| API | API Gateway HTTP v2 + Cognito JWT |
| Queue | AWS SQS (mandated by NFR6) |
| Copy generation | OpenAI GPT-4o mini |
| Image generation | Google Imagen 4 Fast (`imagen-4.0-fast-generate-001`) |
| Compliance | OpenAI GPT-4o mini — 2-pass (text pre-gen + vision post-gen) |
| Text overlay | Pillow |
| Storage | AWS S3 (versioning enabled, CORS on outputs) |
| Database | AWS DynamoDB (PAY_PER_REQUEST, TTL, sparse GSI) |
| Auth | AWS Cognito User Pool |
| Runtime | Python 3.12 (Lambda x86_64) |

---

## Compliance pipeline

Two independent passes using GPT-4o mini:

**Pass 1 — Pre-generation (text, ~3s)**
Runs in parallel with copy generation. Checks the brief's core message for prohibited claims, legal disclaimers, brand voice, cultural sensitivity, and PII risk. If `overall = fail`, the pipeline aborts before Imagen 4 is called — saving image generation cost on briefs that would never pass anyway.

**Pass 2 — Post-generation (vision, ~5s)**
Runs after text overlay is composited onto the image. Checks the rendered creative for brand color consistency, text legibility, product prominence, and visual appropriateness. A hard fail sets `approval_status = compliance_blocked`, requiring a member of the `compliance_override` Cognito group to unblock.

---

## API

All endpoints require `Authorization: Bearer <cognito_id_token>`.

| Method | Path | Description |
|---|---|---|
| `POST` | `/brief` | Submit a campaign brief |
| `POST` | `/brief/batch` | Submit up to 50 briefs |
| `GET` | `/campaigns` | List all campaigns |
| `GET` | `/campaigns/{id}` | Poll campaign status + get assets |
| `PATCH` | `/campaigns/{id}/approval` | Approve / reject |
| `GET` | `/insights` | Analytics — cost, compliance rates, top markets |

### Brief payload

```json
{
  "product_name": "Ergo Desk Pro",
  "region": "united states",
  "audience": "professionals aged 25-45",
  "message": "Work smarter, perform better",
  "language": "en"
}
```

### Response (complete campaign)

```json
{
  "campaign_id": "d65bd540-...",
  "status": "complete",
  "approval_status": "pending_review",
  "ad_copy": [{
    "lang": "en",
    "headline": "Elevate Your Workspace Experience",
    "body": "Designed to support your productivity and comfort...",
    "cta": "Discover the possibilities"
  }],
  "images": {
    "1x1":  { "url": "https://...", "platform": "Instagram" },
    "9x16": { "url": "https://...", "platform": "TikTok/Reels" },
    "16x9": { "url": "https://...", "platform": "YouTube" }
  },
  "compliance_pass1": { "overall": "pass", "checks": [...] },
  "compliance_pass2": { "overall": "pass", "checks": [...] }
}
```

---

## Cost estimates

| Component | Per campaign | Per month (500 campaigns) |
|---|---|---|
| Imagen 4 Fast (3 images) | ~$0.12 | ~$60 |
| GPT-4o mini (copy + 2× compliance) | ~$0.006 | ~$3 |
| S3 storage | ~$0.001 | ~$1.65 |
| Lambda, SQS, DynamoDB | ~$0.001 | negligible |
| **Total** | **~$0.13** | **~$65** |

> Compare to agency model: weeks per campaign, thousands per market. Fully automated in under 30 seconds.

---

## Prerequisites

- AWS account with CLI configured (`aws configure --profile campaignforge`)
- [OpenAI API key](https://platform.openai.com/api-keys) (GPT-4o mini access)
- [Google AI API key](https://aistudio.google.com/app/apikey) (paid tier for Imagen 4)
- Python 3.12
- Node.js 18+ (for CDK CLI)

---

## Setup & deployment

```bash
# 1. Clone and install CDK dependencies
git clone <repo-url>
cd FireflyCampaign
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Bootstrap CDK (once per account/region)
./scripts/bootstrap.sh dev

# 3. Package Lambda dependencies for Linux
./scripts/package_lambdas.sh

# 4. Deploy all stacks
AWS_PROFILE=campaignforge ./scripts/deploy.sh dev

# 5. Inject API keys
./scripts/inject_openai_key.sh dev

AWS_PROFILE=campaignforge aws secretsmanager put-secret-value \
  --secret-id "campaignforge/dev/google-api-key" \
  --secret-string "AIza..."
```

---

## Testing

```bash
# Smoke test — 1 image, ~$0.04, ~30s
AWS_PROFILE=campaignforge ./scripts/test_campaign.sh dev

# Full E2E — 3 images, results saved to results/test_{timestamp}/
AWS_PROFILE=campaignforge ./scripts/test_campaign.sh dev "Ergo Desk Pro" "united states" en

# Unit tests (no AWS required)
python -m pytest tests/unit/ -v
```

Unit tests cover:
- CDK template assertions — S3 versioning (NFR5), DynamoDB TTL, SQS visibility timeout, Cognito group creation
- Image generation handler — 1 brief → 3 images, ratio filter, retry logic, IMAGE_VARIATION mode

---

## Project structure

```
├── app.py                        # CDK app — stack wiring
├── stacks/
│   ├── secrets_stack.py          # Secrets Manager
│   ├── storage_stack.py          # S3 + DynamoDB
│   ├── pipeline_stack.py         # SQS + GenerateCampaign Lambda
│   └── api_stack.py              # HTTP API v2 + Cognito + API Lambdas
├── lambdas/
│   ├── generate_campaign/        # Core pipeline (one Lambda)
│   │   ├── main.py               # SQS handler + orchestrator
│   │   ├── copy_gen.py           # GPT-4o mini ad copy generation
│   │   ├── compliance.py         # GPT-4o mini 2-pass compliance
│   │   ├── image_gen.py          # Imagen 4 Fast image generation
│   │   ├── text_overlay.py       # Pillow text compositing
│   │   └── config.py             # Boto3 clients + env vars
│   └── api/
│       ├── submit_brief/         # POST /brief
│       ├── get_campaigns/        # GET /campaigns
│       ├── update_approval/      # PATCH /campaigns/{id}/approval
│       └── get_insights/         # GET /insights
├── scripts/
│   ├── bootstrap.sh              # One-time CDK + AWS setup
│   ├── deploy.sh                 # Deploy all stacks
│   ├── package_lambdas.sh        # Build Linux-compatible Lambda packages
│   ├── inject_openai_key.sh      # Inject OpenAI key to Secrets Manager
│   ├── sync_kb.sh                # Upload RAG docs + trigger KB ingestion
│   ├── test_campaign.sh          # Full E2E test with image download
│   ├── quick_test.sh             # Smoke test (1 image, ~$0.04)
│   └── teardown.sh               # Destroy all stacks
└── tests/
    ├── unit/
    │   ├── test_firefly_campaign_stack.py   # CDK assertions
    │   └── test_image_gen.py               # Image gen unit tests
    └── integration/
        └── test_e2e_campaign.py            # Live API integration test
```

---

## Key design decisions

**SQS over direct Lambda invocation** — mandated by NFR6 ("SQS retries must not duplicate work"). Visibility timeout (360s) exceeds Lambda timeout (300s), preventing duplicate processing on retry. DLQ after 3 failures.

**Two-pass compliance saves cost** — Pass 1 runs before image generation. A `fail` aborts the pipeline, avoiding ~$0.12 in Imagen 4 calls on briefs that would never ship. Pass 2 runs vision checks on the rendered composite, catching visual brand violations invisible from text alone.

**Single Lambda over Step Functions** — the monolithic `GenerateCampaign` Lambda with internal `ThreadPoolExecutor` parallelism matches the candidate architecture. Step Functions adds operational overhead with no benefit at this scale.

**DynamoDB PAY_PER_REQUEST** — zero cost at idle, no capacity planning. Sparse GSI on `approval_status` + `created_at` powers the reviewer dashboard without full table scans.

**Google Imagen 4 Fast for images** — native support for exact 1:1, 9:16, 16:9 aspect ratios. No approximation needed. Selected over DALL-E 3 (no native 9:16) and Nova Canvas (requires Bedrock model access approval).

---

## Functional requirements coverage

| ID | Requirement | Status |
|---|---|---|
| FR1 | Accept JSON/YAML brief | ✅ |
| FR2 | Generate 3 aspect ratios | ✅ 1:1, 9:16, 16:9 via Imagen 4 |
| FR3 | Localised text overlay | ✅ Pillow compositing |
| FR4 | Organised folder structure | ✅ `outputs/{id}/{product}/{ratio}.png` |
| FR5 | Brand compliance | ✅ GPT-4o mini Pass 1 + Pass 2 |
| FR6 | Legal compliance | ✅ Prohibited terms, PII, disclaimer checks |
| FR7 | Approval workflow | ✅ `pending_review` → `approved`/`rejected` |
| FR8 | Analytics | ✅ `GET /insights` — cost, regions, compliance rates |
| FR9 | Batch submission | ✅ `POST /brief/batch` (max 50) |
| FR10 | Brief import (JSON/YAML) | ✅ Validated on submit |
| FR11 | Programmatic download | ✅ S3 CORS + presigned URLs (7-day TTL) |
| FR12 | Learning over time (RAG) | Planned — Bedrock KB + OpenSearch architecture ready |

---

## Teardown

```bash
# Destroys all stacks (S3/DynamoDB/Secrets are RETAINED)
AWS_PROFILE=campaignforge ./scripts/teardown.sh dev
```

---

## License

MIT
