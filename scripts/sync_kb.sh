#!/usr/bin/env bash
# sync_kb.sh — upload RAG docs + product images, then trigger Bedrock KB ingestion
#
# Usage:
#   ./scripts/sync_kb.sh [env]
#
# What it does:
#   1. Uploads everything in rag-resource/ to the rag-docs S3 bucket
#      (brand-guidelines/ prefix for brand docs, regional-trends/ for market docs)
#   2. Uploads everything in scripts/images/ to the assets-input S3 bucket
#      under products/{product-name}/
#   3. Triggers StartIngestionJob on both Bedrock KB data sources
#   4. Polls until ingestion is COMPLETE

set -euo pipefail

ENV="${1:-dev}"
REGION="${AWS_DEFAULT_REGION:-us-east-1}"

GREEN='\033[0;32m'; CYAN='\033[0;36m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
ok()   { echo -e "${GREEN}  ✓ $*${NC}"; }
info() { echo -e "${CYAN}  → $*${NC}"; }
warn() { echo -e "${YELLOW}  ! $*${NC}"; }
fail() { echo -e "${RED}  ✗ $*${NC}"; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "${SCRIPT_DIR}")"
cd "${PROJECT_ROOT}"

RAG_BUCKET="campaignforge-${ENV}-ragdocs"
ASSETS_BUCKET="campaignforge-${ENV}-assetsinput"

echo ""
echo "════════════════════════════════════════════════════"
echo "  CampaignForge KB Sync  [env=${ENV}]"
echo "════════════════════════════════════════════════════"
echo ""

# ── 1. Upload RAG documents ──────────────────────────────────────────────────
echo "── Uploading RAG documents ──"

RAG_DIR="${PROJECT_ROOT}/rag-resource"
if [[ ! -d "${RAG_DIR}" ]]; then
  warn "rag-resource/ not found — creating sample documents..."
  mkdir -p "${RAG_DIR}"
  _create_samples=true
fi

# Create sample brand/regional docs if directory is empty
SAMPLE_BRAND="${RAG_DIR}/brand-guidelines/brand-voice.md"
SAMPLE_LATAM="${RAG_DIR}/regional-trends/latam.md"
SAMPLE_US="${RAG_DIR}/regional-trends/us.md"

if [[ ! -f "${SAMPLE_BRAND}" ]]; then
  mkdir -p "${RAG_DIR}/brand-guidelines" "${RAG_DIR}/regional-trends"
  cat > "${SAMPLE_BRAND}" <<'EOF'
# CampaignForge Brand Voice Guidelines

## Tone & Voice
- Premium, aspirational, and authentic
- Confident but never arrogant
- Inclusive and globally resonant
- Avoid: superlatives, unsubstantiated claims, competitor mentions

## Visual Identity
- Primary palette: deep navy (#0D1B2A), clean white (#FFFFFF), warm gold (#C9A84C)
- Typography: clean sans-serif for headlines, serif for body
- Imagery: high-contrast, cinematic lighting, real-world context

## Prohibited Terms
- "guaranteed", "#1", "best ever", "proven to", "clinically tested" (without substantiation)
- Any competitor brand names
- Political or religious content
EOF

  cat > "${SAMPLE_LATAM}" <<'EOF'
# Latin America Regional Marketing Trends

## Key Markets: Brazil, Mexico, Colombia, Argentina

## Consumer Insights
- Family and community values are central purchasing motivators
- Mobile-first audience — 85%+ social media via mobile
- Price-value perception is critical; aspirational but accessible messaging resonates
- Brazilian Portuguese and Spanish require cultural adaptation, not just translation

## Seasonal Context
- Major shopping peaks: Día de Muertos (MX), Carnaval prep (BR), Back-to-school January
- Football/soccer culture is a strong engagement driver in Brazil and Argentina

## Platform Preferences
- Instagram and TikTok dominate 18–35 demographic
- WhatsApp is primary sharing platform — assets should be shareable
EOF

  cat > "${SAMPLE_US}" <<'EOF'
# United States Regional Marketing Trends

## Consumer Insights
- Authenticity and brand purpose matter to 25–40 demographic
- Sustainability messaging resonates with Millennial and Gen Z segments
- Direct, benefit-focused copy outperforms abstract lifestyle messaging
- Diversity and representation in visuals is expected, not optional

## Platform Preferences
- Instagram for 25–34, TikTok for 18–24, YouTube for 35+
- Short-form video (< 15s) has highest completion rates

## Legal Considerations
- FTC guidelines require substantiation for any performance claims
- Clear disclosure for sponsored content
EOF
  ok "Created sample RAG documents in rag-resource/"
fi

# Upload brand-guidelines
if [[ -d "${RAG_DIR}/brand-guidelines" ]]; then
  aws s3 sync "${RAG_DIR}/brand-guidelines/" \
    "s3://${RAG_BUCKET}/brand-guidelines/" \
    --region "${REGION}" \
    --delete
  ok "brand-guidelines/ → s3://${RAG_BUCKET}/brand-guidelines/"
fi

# Upload regional-trends
if [[ -d "${RAG_DIR}/regional-trends" ]]; then
  aws s3 sync "${RAG_DIR}/regional-trends/" \
    "s3://${RAG_BUCKET}/regional-trends/" \
    --region "${REGION}" \
    --delete
  ok "regional-trends/ → s3://${RAG_BUCKET}/regional-trends/"
fi
echo ""

# ── 2. Upload product reference images ──────────────────────────────────────
echo "── Uploading product reference images ──"

IMAGES_DIR="${SCRIPT_DIR}/images"
if [[ -d "${IMAGES_DIR}" && "$(ls -A "${IMAGES_DIR}" 2>/dev/null)" ]]; then
  for IMAGE_FILE in "${IMAGES_DIR}"/*.{jpg,jpeg,png,JPG,JPEG,PNG} 2>/dev/null; do
    [[ -f "${IMAGE_FILE}" ]] || continue
    FILENAME=$(basename "${IMAGE_FILE}")
    # Derive product slug from filename: "ErgoDesk-Pro1.jpg" → "ergodesk-pro"
    PRODUCT_SLUG=$(echo "${FILENAME}" | sed 's/[0-9]*\.[a-zA-Z]*$//' | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9]/-/g' | sed 's/-*$//')
    S3_KEY="products/${PRODUCT_SLUG}/${FILENAME}"
    aws s3 cp "${IMAGE_FILE}" \
      "s3://${ASSETS_BUCKET}/${S3_KEY}" \
      --region "${REGION}" \
      --quiet
    ok "${FILENAME} → products/${PRODUCT_SLUG}/"
  done
else
  warn "No images found in scripts/images/ — add product reference photos"
  warn "Expected format: ProductName1.jpg, ProductName2.jpg"
  warn "These are used for Nova Canvas IMAGE_VARIATION conditioning"
fi
echo ""

# ── 3. Trigger Bedrock KB ingestion ─────────────────────────────────────────
echo "── Triggering Bedrock Knowledge Base ingestion ──"

KB_NAME="campaignforge-${ENV}-kb"
KB_ID=$(aws bedrock-agent list-knowledge-bases \
  --region "${REGION}" \
  --query "knowledgeBaseSummaries[?name=='${KB_NAME}'].knowledgeBaseId" \
  --output text 2>/dev/null || echo "")

if [[ -z "${KB_ID}" ]]; then
  warn "Knowledge Base '${KB_NAME}' not found."
  warn "Has the Ai stack been deployed? Run: ./scripts/deploy.sh ${ENV}"
  exit 1
fi
ok "Found KB: ${KB_ID}"

# List all data sources and trigger ingestion on each
DATA_SOURCE_IDS=$(aws bedrock-agent list-data-sources \
  --knowledge-base-id "${KB_ID}" \
  --region "${REGION}" \
  --query "dataSourceSummaries[].dataSourceId" \
  --output text)

if [[ -z "${DATA_SOURCE_IDS}" ]]; then
  fail "No data sources found for KB ${KB_ID}"
fi

JOB_IDS=()
for DS_ID in ${DATA_SOURCE_IDS}; do
  DS_NAME=$(aws bedrock-agent get-data-source \
    --knowledge-base-id "${KB_ID}" \
    --data-source-id "${DS_ID}" \
    --region "${REGION}" \
    --query "dataSource.name" \
    --output text 2>/dev/null || echo "${DS_ID}")

  JOB_ID=$(aws bedrock-agent start-ingestion-job \
    --knowledge-base-id "${KB_ID}" \
    --data-source-id "${DS_ID}" \
    --region "${REGION}" \
    --query "ingestionJob.ingestionJobId" \
    --output text)

  ok "Started ingestion job for '${DS_NAME}': ${JOB_ID}"
  JOB_IDS+=("${DS_ID}:${JOB_ID}")
done
echo ""

# ── 4. Poll until all jobs complete ─────────────────────────────────────────
echo "── Waiting for ingestion to complete ──"
TIMEOUT=300
ELAPSED=0
INTERVAL=10

while [[ ${ELAPSED} -lt ${TIMEOUT} ]]; do
  ALL_DONE=true
  for ENTRY in "${JOB_IDS[@]}"; do
    DS_ID="${ENTRY%%:*}"
    JOB_ID="${ENTRY##*:}"
    STATUS=$(aws bedrock-agent get-ingestion-job \
      --knowledge-base-id "${KB_ID}" \
      --data-source-id "${DS_ID}" \
      --ingestion-job-id "${JOB_ID}" \
      --region "${REGION}" \
      --query "ingestionJob.status" \
      --output text 2>/dev/null || echo "UNKNOWN")

    if [[ "${STATUS}" == "COMPLETE" ]]; then
      ok "Job ${JOB_ID}: COMPLETE"
    elif [[ "${STATUS}" == "FAILED" ]]; then
      fail "Job ${JOB_ID}: FAILED — check Bedrock console for details"
    else
      info "Job ${JOB_ID}: ${STATUS} (${ELAPSED}s elapsed)"
      ALL_DONE=false
    fi
  done

  [[ "${ALL_DONE}" == "true" ]] && break
  sleep ${INTERVAL}
  ELAPSED=$((ELAPSED + INTERVAL))
done

[[ ${ELAPSED} -ge ${TIMEOUT} ]] && warn "Timed out waiting for ingestion — check Bedrock console"

echo ""
echo "════════════════════════════════════════════════════"
echo "  KB sync complete. The Bedrock Knowledge Base"
echo "  is now populated with brand guidelines and"
echo "  regional trends."
echo "════════════════════════════════════════════════════"
echo ""
