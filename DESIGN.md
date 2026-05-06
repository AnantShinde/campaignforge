# CampaignForge
## System Design & Implementation

---

## 1. Problem Statement

A global consumer goods company launches hundreds of localised social ad campaigns every month across dozens of markets. Today this is done manually:

- A creative team writes briefs
- Agencies produce assets per region
- Legal and brand teams review
- Stakeholders in each market approve
- Assets get scheduled and published

This process is slow (weeks per campaign), expensive (agencies + revisions), inconsistent (off-brand creative slips through), and impossible to learn from (siloed performance data).

**My goal:** design and build a system that takes a campaign brief as input and produces brand-compliant, legally-vetted, localised creative assets for three social platforms — fully automated, under 30 seconds.

---

## 2. Requirements

### Functional Requirements

| ID | Requirement |
|---|---|
| FR1 | Accept a campaign brief (JSON) with product name, target region, audience, core message, and language |
| FR2 | Generate hero images in three aspect ratios: 1:1 (Instagram), 9:16 (TikTok/Reels), 16:9 (YouTube) |
| FR3 | Overlay localised text on each generated image |
| FR4 | Save all assets at `outputs/{campaign_id}/{product_name}/{ratio}.png` |
| FR5 | Enforce brand compliance on all generated creative |
| FR6 | Enforce legal compliance — no prohibited claims or terms |
| FR7 | Support an approval workflow — campaigns land in `pending_review` before being considered final |
| FR8 | Support batch submission — up to 50 briefs per request |
| FR9 | Programmatic download — direct browser download via S3 CORS and Blob fetching |

### Non-Functional Requirements

| ID | Requirement |
|---|---|
| NFR1 | Scale horizontally without infrastructure changes |
| NFR2 | Generation of one campaign (3 images) must complete within 10 minutes |
| NFR3 | Brand and legal guardrails must apply to 100% of outputs |
| NFR4 | Reproducible across environments via Infrastructure as Code |
| NFR5 | All generated assets must be durable — S3 with versioning enabled |
| NFR6 | Generation must be idempotent — SQS retries must not duplicate work |

### Out of Scope

- Ad platform integration (Meta, TikTok, Google) — publish step is manual
- A/B testing of creatives
- Real-time performance data ingestion

---

## 3. Back-of-the-Envelope Estimation

### Volume

- Campaigns per month: ~500
- Images per campaign: 3 aspect ratios
- Total images per month: 500 × 3 = **1,500 images**

### Compute & Throughput

- Imagen 4 Fast generation: ~8–12s per image
- 3 images fired concurrently via `ThreadPoolExecutor` → ~12s wall-clock
- GPT-4o mini (copy gen + 2 compliance passes): ~8s concurrent with image gen
- **Total wall-clock: ~20–30s per campaign**

### Storage

- Average image size: ~1 MB
- Monthly storage: 1,500 × 1 MB = ~1.5 GB
- S3 cost (us-east-1): ~$0.03/month

### AI Inference Cost

| Component | Per campaign | Per month (500) |
|---|---|---|
| Google Imagen 4 Fast (3 images) | ~$0.12 | ~$60 |
| GPT-4o mini (copy + compliance ×2) | ~$0.006 | ~$3 |
| S3 + DynamoDB + Lambda | ~$0.001 | negligible |
| **Total** | **~$0.13** | **~$65** |

---

## 4. High-Level Design

```
POST /brief  →  202 Accepted + campaign_id
                    │
             SQS Standard Queue
             (NFR6: idempotent, maxConcurrency=10)
                    │
             GenerateCampaign Lambda (300s, 1024 MB)
                    │
        ┌───────────┴───────────┐
        │                       │
   CopyGen                Pass 1 Compliance
   (GPT-4o mini)          (GPT-4o mini, text)
   ad copy + prompt            │
        │              Pass 1 = fail?
        └───────────────┤
                         │ yes → compliance_blocked, stop
                         │ no  ↓
                    Imagen 4 Fast
                 (3 ratios, concurrent)
                         │
                    Text Overlay
                    (Pillow, headline)
                         │
                    Pass 2 Compliance
                    (GPT-4o mini, vision)
                         │
                    PersistAssets
                    (S3 + DynamoDB)
                         │
              approval_status: pending_review
                         │
             GET /campaigns/{id} → assets + report
```

### Architecture Layers

| Layer | Components | Responsibility |
|---|---|---|
| P1 User | API Gateway HTTP v2 + Cognito | Authenticated brief ingestion |
| P2 Queue | SQS Standard | Decoupling + idempotent retry (NFR6) |
| P3 Generation | GenerateCampaign Lambda | Copy, images, compliance, persistence |
| P4 AI | GPT-4o mini + Google Imagen 4 | Language model + image model |
| P5 Storage | S3 (outputs) + DynamoDB | Durable asset storage + campaign state |

---

## 5. Deep Dive

### 5.A Data Flow

**Flow 1 — Generation (user-initiated)**

1. Client calls `POST /brief` with campaign JSON
2. `SubmitBrief` Lambda validates, writes to DynamoDB (`status: queued`), sends to SQS
3. Returns `202 Accepted + { campaign_id }`
4. Client polls `GET /campaigns/{id}` every 3s
5. SQS triggers `GenerateCampaign` Lambda (via ESM, maxConcurrency=10)
6. Pipeline runs: Copy + Pass1 → Image gen → Text overlay → Pass2 → Persist
7. Poll returns `status: complete, approval_status: pending_review` → client renders assets

**Flow 2 — Approval (reviewer-initiated)**

1. Reviewer receives notification that a campaign is `pending_review`
2. Reviews generated images and compliance report
3. Approves or rejects

### 5.B Two-Pass Compliance Architecture

I designed a two-pass compliance gate using GPT-4o mini. The sequence is deliberate:

**Pass 1 — Pre-generation text check (~3s, runs in parallel with CopyGen)**

Checks the brief's core message before any image is generated:
- No prohibited superlatives (`guaranteed`, `#1`, `proven`, `cure`, `risk-free`)
- Claims modest enough or disclaimer present
- Premium, aspirational brand voice
- Culturally appropriate for target region
- No PII, phone numbers, or URLs

If `overall = fail` → pipeline aborts. No Imagen 4 calls are made. This saves ~$0.12 per rejected campaign — meaningful at scale.

**Pass 2 — Post-generation vision check (~5s, after text overlay)**

GPT-4o mini reviews the rendered composite image:
- Brand colour consistency
- Headline text legibility
- Product prominence
- Visual appropriateness for all audiences

A Pass 2 `fail` sets `approval_status = compliance_blocked`. Human override required.

**Why two passes:** Pass 1 saves cost on hard failures. Pass 2 catches visual issues invisible from text alone — off-colour renders, illegible overlays, inappropriate generated content.

```
Brief text  →  Pass 1 (text)  →  [abort if fail]  →  Imagen 4  →  Overlay  →  Pass 2 (vision)
                 ‖ parallel                                                          ‖
              CopyGen                                                         compliance_blocked
```

### 5.C Component Design

**GenerateCampaign Lambda** — the single execution unit, 300s timeout, 1024 MB.

Internal structure:
```
generate_campaign/
  main.py          — SQS handler + orchestrator
  copy_gen.py      — GPT-4o mini ad copy + image prompt
  compliance.py    — GPT-4o mini Pass 1 (text) + Pass 2 (vision)
  image_gen.py     — Google Imagen 4 Fast, 3 ratios concurrent
  text_overlay.py  — Pillow headline compositing
  config.py        — boto3 clients + cached API clients
```

**Internal concurrency — ThreadPoolExecutor:**

```python
# Level 1: CopyGen and Pass1 run in parallel
with ThreadPoolExecutor(max_workers=2) as pool:
    copy_future = pool.submit(copy_gen.generate, data)
    p1_future   = pool.submit(compliance.pass1_check, data)

# Level 2: All 3 image ratios generated concurrently
with ThreadPoolExecutor(max_workers=3) as pool:
    futures = {pool.submit(_generate_one, ratio, ...): ratio
               for ratio in active_ratios}
```

Wall-clock reduced by ~60% vs sequential.

**SQS Queue configuration (NFR6):**

```
Queue type:         Standard (ordering not required)
Visibility timeout: 360s  (Lambda timeout 300s + 60s buffer)
Retention:          1 day
DLQ:                3 retries before dead-letter
maxConcurrency:     10    (caps concurrent Lambda executions)
```

### 5.D Database Design

**DynamoDB — CampaignTable**

Primary Key: `campaign_id` (PK) + `product_name` (SK)

One campaign covers multiple products. `campaign_id` as PK retrieves all products in one Query. `product_name` as SK targets a specific product. No scan needed for either pattern.

| Attribute | Type | Notes |
|---|---|---|
| `campaign_id` | String | UUID — partition key |
| `product_name` | String | Sort key |
| `region` | String | e.g. `"brazil"` |
| `audience` | String | e.g. `"working women 24-40"` |
| `message` | String | Core campaign message |
| `language` | String | BCP-47 code e.g. `"pt-BR"` |
| `ad_copy` | List | `[{ lang, headline, body, cta }]` |
| `image_prompt` | String | Prompt used for Imagen 4 |
| `images` | Map | `{ "1x1": { url, s3_key, ratio }, ... }` |
| `compliance_pass1` | Map | GPT-4o mini text check result |
| `compliance_pass2` | Map | GPT-4o mini vision check result |
| `approval_status` | String | `pending_review` \| `approved` \| `rejected` \| `compliance_blocked` |
| `created_at` | String | ISO 8601 |
| `ttl` | Number | Unix timestamp — auto-expire after 90 days |

**Billing:** `PAY_PER_REQUEST` — zero cost at idle. No capacity planning needed at POC scale.

**GSI — status-created-index:**

```
Hash key:  approval_status
Sort key:  created_at

Purpose: Query all campaigns WHERE approval_status = "pending_review"
         ORDER BY created_at DESC — reviewer dashboard without full table scan.
Sparse:  items without approval_status (still generating) excluded automatically.
```

### 5.E API Design

All endpoints use HTTP API v2 (API Gateway). JWT authorisation via Cognito User Pool. CORS enabled.

**Why HTTP v2 over REST v1:** HTTP v2 natively supports JWT authorisers without a Lambda authoriser. Cost: $1.00/M vs $3.50/M requests. At POC scale (~100K/month) the difference is ~$0.25/month. `reviewed_by` extracted from JWT claims server-side — never trusted from client.

**POST /brief** — single campaign brief

```json
// Request
{
  "product_name": "LuxeRoll Hair Brush",
  "region": "united states",
  "audience": "working women aged 24-40, mid-to-high income",
  "message": "Define your natural beauty, on your schedule",
  "language": "en"
}

// Response 202
{
  "campaign_id": "0ca58c7d-...",
  "product_name": "LuxeRoll Hair Brush",
  "status": "queued"
}
```

**POST /brief/batch** — up to 50 briefs

```json
// Request
{ "briefs": [ {...}, {...} ] }

// Response 202
{ "batch_id": "60322e51-...", "campaign_ids": ["bfe42355-...", "21f3ced5-..."], "count": 5 }
```

**GET /campaigns/{id}** — poll for status and assets

```json
// Response 200 (complete)
{
  "campaign_id": "0ca58c7d-...",
  "status": "complete",
  "approval_status": "pending_review",
  "ad_copy": [{
    "lang": "en",
    "headline": "Elevate Your Workspace Experience",
    "body": "Designed to support your productivity...",
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

### 5.F AI Pipeline

**Models used:**

| Model | Role | Provider |
|---|---|---|
| `gpt-4o-mini` | Ad copy generation, localisation, 2-pass compliance | OpenAI |
| `imagen-4.0-fast-generate-001` | Image generation — exact 1:1, 9:16, 16:9 | Google AI |

**Why Imagen 4 Fast over alternatives:**

I evaluated every accessible image generation model before settling:

- **Nova Canvas** (Bedrock) — requires Bedrock model access console activation
- **DALL-E 3** (OpenAI) — no native 9:16 ratio; 1024×1792 approximation
- **Stability Style Guide** (Bedrock) — requires AWS Marketplace subscription
- **Imagen 4 Fast** — native 1:1, 9:16, 16:9; ~800 KB output; already accessible

Imagen 4 Fast generates images at exact aspect ratios without approximation. This matters: a 9:16 at 1024×1792 (DALL-E) is not the same as 720×1280 (TikTok native). I chose correctness over convenience.

**Why GPT-4o mini for compliance:** Compliance is a structured classification task — pass/warn/fail per criterion with JSON output. Mini handles this reliably at a fraction of the cost. The compliance prompts are tightly constrained; this is not a task that benefits from a larger model.

**Client caching (cold start optimisation):**

```python
_openai_client = None
_google_client = None

def get_openai_client():
    global _openai_client
    if _openai_client is None:
        secret = boto3.client("secretsmanager").get_secret_value(...)
        _openai_client = OpenAI(api_key=secret["SecretString"])
    return _openai_client
```

API clients are initialised once per Lambda cold start and reused across invocations. Secrets Manager is not called on every request.

### 5.G Infrastructure (AWS CDK, Python)

```
stacks/
  secrets_stack.py    — Secrets Manager: OpenAI + Google AI keys
  storage_stack.py    — S3 (outputs, versioned, CORS) + DynamoDB
  pipeline_stack.py   — SQS + GenerateCampaign Lambda + ESM
  api_stack.py        — HTTP API v2 + Cognito + 2 API Lambdas
```

Deploy order: `secrets → storage → pipeline → api`

Each stack is environment-parameterised via CDK context (`--context env=dev`). All configuration lives in `cdk.json` context maps.

---

## 6. Results

### Ergo Desk Pro — United States Campaign

Generated in **19 seconds**. Three aspect ratios, compliant copy, presigned download URLs.

**Instagram 1×1**

![Ergo Desk Pro 1×1](results/test_20260505_230810/images/1-1.png)

**TikTok/Reels 9×16**

![Ergo Desk Pro 9×16](results/test_20260505_230810/images/9-16.png)

**YouTube 16×9**

![Ergo Desk Pro 16×9](results/test_20260505_230810/images/16-9.png)

---

### LuxeRoll Hair Brush — Multi-Market Campaign

I submitted 5 briefs simultaneously as a batch — one per demographic — targeting working women aged 24–40 across different global markets. All generated concurrently. Results in under 35 seconds.

**United States — African American women**

![LuxeRoll US 1×1](results/hairbrush_1778039117/united_states/1x1.png) ![LuxeRoll US 9×16](results/hairbrush_1778039117/united_states/9x16.png) ![LuxeRoll US 16×9](results/hairbrush_1778039117/united_states/16x9.png)

**Brazil — Latina women** *(Copy in Portuguese)*

![LuxeRoll Brazil 1×1](results/hairbrush_1778039117/brazil/1x1.png) ![LuxeRoll Brazil 9×16](results/hairbrush_1778039117/brazil/9x16.png) ![LuxeRoll Brazil 16×9](results/hairbrush_1778039117/brazil/16x9.png)

**India — South Asian professional women**

![LuxeRoll India 1×1](results/hairbrush_1778039117/india/1x1.png) ![LuxeRoll India 9×16](results/hairbrush_1778039117/india/9x16.png) ![LuxeRoll India 16×9](results/hairbrush_1778039117/india/16x9.png)

**Germany — European professional women** *(Copy in German)*

![LuxeRoll Germany 1×1](results/hairbrush_1778039117/germany/1x1.png) ![LuxeRoll Germany 9×16](results/hairbrush_1778039117/germany/9x16.png) ![LuxeRoll Germany 16×9](results/hairbrush_1778039117/germany/16x9.png)

---

## 7. Key Design Decisions & Trade-offs

### Decision 1: Single Lambda vs Step Functions

| | Choice |
|---|---|
| **Chose** | Single GenerateCampaign Lambda with internal ThreadPoolExecutor |
| **Alternative** | AWS Step Functions Express Workflow (7 states) |
| **Reason** | Step Functions adds operational overhead — 7 IAM roles, EventBridge Pipe, per-state CloudWatch streams — with no material benefit at POC scale. The single Lambda with ThreadPoolExecutor achieves the same internal parallelism (CopyGen ‖ Pass1, image ratios concurrent) in ~20s. Step Functions would be the right call if individual states needed independent retry budgets or if the pipeline exceeded Lambda's 15-minute limit. |

### Decision 2: Two-Pass Compliance vs Single-Pass Parallel

| | Choice |
|---|---|
| **Chose** | Sequential two-pass: text pre-generation + vision post-generation |
| **Alternative** | Single compliance pass running parallel with image generation |
| **Reason** | Pass 1 running before image generation means a hard compliance failure aborts the pipeline before any Imagen 4 calls — saving ~$0.12 per rejected campaign. Pass 2 vision-checks the rendered composite, catching visual brand violations that text analysis cannot see. The net latency cost of this sequencing is near zero because Pass 1 runs in parallel with CopyGen (~8s). |

### Decision 3: Google Imagen 4 vs Alternatives

| | Choice |
|---|---|
| **Chose** | `imagen-4.0-fast-generate-001` via Google AI API |
| **Alternative** | DALL-E 3 (OpenAI), Nova Canvas (Bedrock), Stability (Bedrock) |
| **Reason** | I tested every accessible image generation model before committing. Nova Canvas and Titan Image Generator require Bedrock model access console activation. Stability models require an AWS Marketplace subscription. DALL-E 3 lacks a native 9:16 aspect ratio. Imagen 4 Fast supports exact 1:1, 9:16, 16:9 natively, produces ~800 KB output, costs ~$0.04/image, and was immediately accessible with the existing Google AI key. |

### Decision 4: DynamoDB vs RDS

| | Choice |
|---|---|
| **Chose** | DynamoDB PAY_PER_REQUEST |
| **Alternative** | Aurora Serverless PostgreSQL |
| **Reason** | Campaign data is accessed by primary key 99% of the time. DynamoDB delivers microsecond reads with no query language required. PAY_PER_REQUEST means zero cost at idle — Aurora's minimum is ~$50/month even with no traffic. The sparse GSI handles the reviewer dashboard query without relying on SQL aggregation. |

### Decision 5: Polling vs WebSockets

| | Choice |
|---|---|
| **Chose** | Client polls `GET /campaigns/{id}` every 3s |
| **Alternative** | API Gateway WebSocket + Lambda push |
| **Reason** | Generation takes 20–30 seconds. Polling at 3s costs ~10 API calls per campaign — negligible. WebSockets require connection management, reconnection logic, a connection table in DynamoDB, and significantly more infrastructure. The UX difference vs push notification is imperceptible for a 20–30 second operation. |

### Decision 6: HTTP API v2 vs REST API v1

| | Choice |
|---|---|
| **Chose** | HTTP API v2 with Cognito JWT native authoriser |
| **Alternative** | REST API v1 with API key authentication |
| **Reason** | HTTP v2 supports Cognito JWT authorisers natively — no Lambda authoriser needed. It also costs 3.5× less ($1.00/M vs $3.50/M). Using Cognito means `reviewed_by` identity is extracted from the JWT claims server-side; the client cannot spoof it. |

---

## 8. Known Limitations

**A. Imagen 4 has no reference photo seeding**

Nova Canvas supports `IMAGE_VARIATION` mode — using an uploaded product photo as a conditioning seed for brand-anchored generation. Imagen 4 is text-to-image only. Prompts are crafted to describe the product accurately, but there is no pixel-level brand anchoring.

*Mitigation planned:* Switch to Nova Canvas once Bedrock model access is activated, enabling `IMAGE_VARIATION` with product reference photos from S3.

**B. Japanese language support is an edge case**

The system supports any BCP-47 language code and GPT-4o mini handles Japanese copy well. However, the Pillow text overlay (`RobotoSlab-Bold.ttf`) does not include CJK (Chinese/Japanese/Korean) character sets. Japanese headlines will not render correctly as composited text overlays.

*Mitigation planned:* See Future Improvements §9.

**C. Compliance is a classification task, not legal advice**

GPT-4o mini applies a consistent rubric but is not a licensed legal reviewer. Pass/warn/fail verdicts should be treated as a first-pass filter, not a legal clearance. Human approval (the `pending_review` workflow) remains mandatory before assets are published.

**D. DynamoDB Scan for analytics**

A `Scan` on the full CampaignTable is used for aggregation. At ~500 campaigns/month this completes in under 1 second. As the table grows beyond ~10,000 items, scan performance will degrade.

*Mitigation:* Add Kinesis Firehose → S3 Parquet → Athena pipeline when volume warrants it.

---

## 9. What's Next

### Short Term

**Japanese and CJK language support**

The current `RobotoSlab-Bold.ttf` font does not contain CJK glyphs. Supporting Japanese (ja), Korean (ko), Simplified Chinese (zh-Hans), and Traditional Chinese (zh-Hant) requires:

1. Bundle a CJK-compatible font — [Noto Sans JP](https://fonts.google.com/noto/specimen/Noto+Sans+JP) covers Japanese, [Noto Sans KR](https://fonts.google.com/noto/specimen/Noto+Sans+KR) covers Korean
2. Detect the target language in `text_overlay.py` and load the appropriate font
3. Adjust font size metrics for CJK characters (typically wider glyph boxes)

```python
FONT_MAP = {
    "ja": "NotoSansJP-Bold.ttf",
    "ko": "NotoSansKR-Bold.ttf",
    "zh": "NotoSansSC-Bold.ttf",
    "default": "RobotoSlab-Bold.ttf",
}

lang = data.get("language", "en")[:2]
font_file = FONT_MAP.get(lang, FONT_MAP["default"])
```

This unblocks campaigns targeting Japan (~$3.7T consumer market) and South Korea without any API or model changes — GPT-4o mini already produces high-quality Japanese copy.

**Reference-led image generation**

Switch image generation to Nova Canvas `IMAGE_VARIATION` mode once Bedrock model access is enabled. Product reference photos uploaded to S3 serve as conditioning seeds, anchoring outputs to the actual product rather than relying on text description alone.

**Runtime asset upload**

Wire the assets-input S3 path to a pre-signed upload URL endpoint so creative teams can upload product photos directly from the browser without AWS console access.

### Long Term

**Video generation**

Expand from Imagen 4 to Google Veo or Nova Reel for short-form social video assets (15s, 30s). The pipeline architecture is unchanged — image generation becomes video generation with the same compliance gates applied to the video transcript and thumbnail.

**Ad platform integration**

One-click publish to Meta Ads Manager and TikTok Ads via their respective APIs. The `approved` campaign state is the natural trigger. Requires OAuth integration per platform.

**Fine-tuning on approved campaigns**

Once ~1,000 approved campaigns exist, fine-tune the copy generation prompt with high-performing briefs. Compliance pass rates and reviewer approval data feed back into the system — closing the learning loop specified in FR12.

**Horizontal micro-scaling**

Currently one SQS message = one Lambda execution handles all products in a campaign. Future state: fan out to one SQS message per product, enabling true horizontal scaling to hundreds of simultaneous Lambda instances with no code changes.

---

## Appendix

### A. Deployment

```bash
# One-time setup
./scripts/bootstrap.sh dev

# Install Lambda dependencies (Linux-compatible wheels)
./scripts/package_lambdas.sh

# Deploy all stacks
AWS_PROFILE=campaignforge ./scripts/deploy.sh dev

# Inject API keys
./scripts/inject_openai_key.sh dev
aws secretsmanager put-secret-value \
  --secret-id "campaignforge/dev/google-api-key" \
  --secret-string "AIza..."
```

### B. Testing

```bash
# Smoke test — 3 images, ~$0.13, ~30s
AWS_PROFILE=campaignforge ./scripts/quick_test.sh dev

# Full E2E — downloads images to results/test_{timestamp}/
AWS_PROFILE=campaignforge ./scripts/test_campaign.sh dev "Ergo Desk Pro" "united states" en

# Batch — multiple markets simultaneously
curl -X POST $API_URL/brief/batch \
  -H "Authorization: Bearer $JWT" \
  -d @campaign_briefs/hairbrush_roll_campaign.json

# Unit tests (no AWS required)
python -m pytest tests/unit/ -v
```

### C. Lambda Complexity Analysis

**SubmitBrief** — O(1). Validates JSON, writes one DynamoDB item, sends one SQS message.

**GenerateCampaign** — O(R) where R = number of active aspect ratios (1–3). Parallelised at two levels:
- Level 1: CopyGen ‖ Pass1Compliance via ThreadPoolExecutor(max_workers=2)
- Level 2: All R image ratios via ThreadPoolExecutor(max_workers=R)

Sequential wall-clock ≈ max(CopyGen, Pass1) + max(ImageGen × R) + TextOverlay + Pass2 + Persist ≈ 30s for R=3.

**GetCampaigns** — O(P) where P = number of products per campaign (typically 1–2). Queries DynamoDB by PK (no scan), refreshes presigned URLs in memory.
