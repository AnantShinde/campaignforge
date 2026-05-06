#!/usr/bin/env bash
# test_campaign.sh — end-to-end campaign generation test with recorded results
#
# Usage:
#   ./scripts/test_campaign.sh [env] [product] [region] [language]
#
# Defaults:
#   env      = dev
#   product  = "Ergo Desk Pro"
#   region   = "united states"
#   language = en
#
# What it does:
#   1. Creates a Cognito test user (if not exists)
#   2. Gets a JWT token
#   3. Submits a campaign brief → POST /brief
#   4. Polls GET /campaigns/{id} every 5s until complete or timeout (12 min)
#   5. Downloads all 3 generated images locally
#   6. Records full results to results/test_{timestamp}/report.json
#   7. Prints a pass/fail summary
#
# Prerequisites:
#   - All 5 CDK stacks deployed
#   - OpenAI key injected
#   - Bedrock model access enabled (Nova Canvas, Claude Haiku, Titan Embed)
#   - KB synced (./scripts/sync_kb.sh)

set -euo pipefail

ENV="${1:-dev}"
PRODUCT="${2:-Ergo Desk Pro}"
REGION_NAME="${3:-united states}"
LANGUAGE="${4:-en}"
AWS_PROFILE="${AWS_PROFILE:-campaignforge}"
AWS_REGION="${AWS_DEFAULT_REGION:-us-east-1}"

POLL_INTERVAL=5
TIMEOUT_SECONDS=720   # 12 minutes

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
ok()    { echo -e "${GREEN}  ✓ $*${NC}"; }
fail()  { echo -e "${RED}  ✗ $*${NC}"; }
info()  { echo -e "${CYAN}  → $*${NC}"; }
warn()  { echo -e "${YELLOW}  ! $*${NC}"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "${SCRIPT_DIR}")"

# ── Resolve stack outputs ────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════════════"
echo "  CampaignForge E2E Test"
echo "  Product  : ${PRODUCT}"
echo "  Region   : ${REGION_NAME}"
echo "  Language : ${LANGUAGE}"
echo "════════════════════════════════════════════════════"
echo ""

info "Resolving stack outputs..."

API_URL=$(aws cloudformation describe-stacks \
  --stack-name "CampaignForge-${ENV}-Api" \
  --profile "${AWS_PROFILE}" \
  --region "${AWS_REGION}" \
  --query "Stacks[0].Outputs[?contains(OutputKey,'ApiUrl') || contains(OutputKey,'Endpoint')].OutputValue" \
  --output text 2>/dev/null || echo "")

USER_POOL_ID=$(aws cloudformation describe-stacks \
  --stack-name "CampaignForge-${ENV}-Api" \
  --profile "${AWS_PROFILE}" \
  --region "${AWS_REGION}" \
  --query "Stacks[0].Outputs[?contains(OutputKey,'UserPoolId')].OutputValue" \
  --output text 2>/dev/null || echo "")

CLIENT_ID=$(aws cloudformation describe-stacks \
  --stack-name "CampaignForge-${ENV}-Api" \
  --profile "${AWS_PROFILE}" \
  --region "${AWS_REGION}" \
  --query "Stacks[0].Outputs[?contains(OutputKey,'ClientId') || contains(OutputKey,'WebClientId')].OutputValue" \
  --output text 2>/dev/null || echo "")

if [[ -z "${API_URL}" || -z "${USER_POOL_ID}" || -z "${CLIENT_ID}" ]]; then
  fail "Could not resolve Api stack outputs. Is CampaignForge-${ENV}-Api deployed?"
  echo "     API_URL     : '${API_URL}'"
  echo "     USER_POOL_ID: '${USER_POOL_ID}'"
  echo "     CLIENT_ID   : '${CLIENT_ID}'"
  exit 1
fi

ok "API        : ${API_URL}"
ok "User Pool  : ${USER_POOL_ID}"
ok "Client ID  : ${CLIENT_ID}"
echo ""

# ── Create / ensure test user ────────────────────────────────────────────────
echo "── Setting up Cognito test user ──"

TEST_EMAIL="testuser-${ENV}@campaignforge.internal"
TEST_PASS="CfTest@2025!"

# Try to create the user (ignore error if already exists)
aws cognito-idp admin-create-user \
  --user-pool-id "${USER_POOL_ID}" \
  --username "${TEST_EMAIL}" \
  --temporary-password "${TEST_PASS}" \
  --message-action SUPPRESS \
  --profile "${AWS_PROFILE}" \
  --region "${AWS_REGION}" > /dev/null 2>&1 || true

# Set permanent password (bypasses force-change-password state)
aws cognito-idp admin-set-user-password \
  --user-pool-id "${USER_POOL_ID}" \
  --username "${TEST_EMAIL}" \
  --password "${TEST_PASS}" \
  --permanent \
  --profile "${AWS_PROFILE}" \
  --region "${AWS_REGION}" > /dev/null 2>&1

ok "Test user: ${TEST_EMAIL}"
echo ""

# ── Get JWT token ────────────────────────────────────────────────────────────
echo "── Authenticating ──"

AUTH_RESULT=$(aws cognito-idp initiate-auth \
  --auth-flow USER_PASSWORD_AUTH \
  --client-id "${CLIENT_ID}" \
  --auth-parameters USERNAME="${TEST_EMAIL}",PASSWORD="${TEST_PASS}" \
  --profile "${AWS_PROFILE}" \
  --region "${AWS_REGION}" \
  --query 'AuthenticationResult.IdToken' \
  --output text 2>&1)

if [[ "${AUTH_RESULT}" == "None" || -z "${AUTH_RESULT}" ]]; then
  fail "Authentication failed. The Cognito app client may not support USER_PASSWORD_AUTH."
  echo "     Check that the app client has USER_PASSWORD_AUTH enabled."
  exit 1
fi

JWT="${AUTH_RESULT}"
ok "JWT token obtained (${#JWT} chars)"
echo ""

# ── Submit brief ─────────────────────────────────────────────────────────────
echo "── Submitting campaign brief ──"

BRIEF=$(cat <<EOF
{
  "product_name": "${PRODUCT}",
  "region": "${REGION_NAME}",
  "audience": "professionals aged 25-45",
  "message": "Work smarter, perform better",
  "language": "${LANGUAGE}"
}
EOF
)

SUBMIT_RESPONSE=$(curl -s -X POST "${API_URL}/brief" \
  -H "Authorization: Bearer ${JWT}" \
  -H "Content-Type: application/json" \
  -d "${BRIEF}")

CAMPAIGN_ID=$(echo "${SUBMIT_RESPONSE}" | python3 -c "import sys,json; print(json.load(sys.stdin).get('campaign_id',''))" 2>/dev/null || echo "")

if [[ -z "${CAMPAIGN_ID}" ]]; then
  fail "Brief submission failed:"
  echo "     ${SUBMIT_RESPONSE}"
  exit 1
fi

ok "Campaign submitted: ${CAMPAIGN_ID}"
info "Brief: ${BRIEF}"
echo ""

# ── Poll for completion ──────────────────────────────────────────────────────
echo "── Polling for completion (timeout: ${TIMEOUT_SECONDS}s) ──"

START_TIME=$(date +%s)
STATUS="queued"
FINAL_RESPONSE=""

while true; do
  ELAPSED=$(( $(date +%s) - START_TIME ))
  if [[ ${ELAPSED} -ge ${TIMEOUT_SECONDS} ]]; then
    fail "Timed out after ${ELAPSED}s waiting for campaign ${CAMPAIGN_ID}"
    exit 1
  fi

  POLL_RESPONSE=$(curl -s "${API_URL}/campaigns/${CAMPAIGN_ID}" \
    -H "Authorization: Bearer ${JWT}")

  STATUS=$(echo "${POLL_RESPONSE}" | python3 -c "
import sys, json
d = json.load(sys.stdin)
blueprints = d.get('blueprints', [d])
print(blueprints[0].get('status', 'unknown') if blueprints else 'unknown')
" 2>/dev/null || echo "error")

  APPROVAL=$(echo "${POLL_RESPONSE}" | python3 -c "
import sys, json
d = json.load(sys.stdin)
blueprints = d.get('blueprints', [d])
print(blueprints[0].get('approval_status', '') if blueprints else '')
" 2>/dev/null || echo "")

  printf "  [%3ds] status=%-12s approval=%-20s\r" "${ELAPSED}" "${STATUS}" "${APPROVAL}"

  if [[ "${STATUS}" == "complete" ]]; then
    FINAL_RESPONSE="${POLL_RESPONSE}"
    echo ""
    ok "Complete in ${ELAPSED}s"
    break
  elif [[ "${STATUS}" == "failed" ]]; then
    echo ""
    REASON=$(echo "${POLL_RESPONSE}" | python3 -c "
import sys,json; d=json.load(sys.stdin)
b=d.get('blueprints',[d]); print(b[0].get('failure_reason','unknown') if b else 'unknown')
" 2>/dev/null || echo "unknown")
    fail "Campaign failed: ${REASON}"
    exit 1
  fi

  sleep ${POLL_INTERVAL}
done
echo ""

# ── Record results ───────────────────────────────────────────────────────────
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
RESULTS_DIR="${PROJECT_ROOT}/results/test_${TIMESTAMP}"
mkdir -p "${RESULTS_DIR}"

echo "── Recording results to ${RESULTS_DIR} ──"

# Save full API response
echo "${FINAL_RESPONSE}" | python3 -m json.tool > "${RESULTS_DIR}/report.json"
ok "report.json saved"

# Extract and save individual fields
python3 - "${RESULTS_DIR}" "${CAMPAIGN_ID}" <<'PYEOF'
import json, sys, os

results_dir = sys.argv[1]
campaign_id = sys.argv[2]

with open(f"{results_dir}/report.json") as f:
    data = json.load(f)

blueprints = data.get("blueprints", [data])
bp = blueprints[0] if blueprints else {}

summary = {
    "campaign_id": campaign_id,
    "product_name": bp.get("product_name"),
    "status": bp.get("status"),
    "approval_status": bp.get("approval_status"),
    "generation_time_note": "see elapsed time in test output",
    "compliance_pass1": bp.get("compliance_pass1", {}),
    "compliance_pass2": bp.get("compliance_pass2", {}),
    "image_urls": {
        ratio: meta.get("url", "") if isinstance(meta, dict) else ""
        for ratio, meta in bp.get("images", {}).items()
    },
    "ad_copy": bp.get("ad_copy", []),
    "image_prompt": bp.get("image_prompt"),
}

with open(f"{results_dir}/summary.json", "w") as f:
    json.dump(summary, f, indent=2)

print(json.dumps(summary, indent=2))
PYEOF

echo ""

# Download generated images
echo "── Downloading generated images ──"
IMAGES_DIR="${RESULTS_DIR}/images"
mkdir -p "${IMAGES_DIR}"

python3 - "${RESULTS_DIR}/report.json" "${IMAGES_DIR}" <<'PYEOF'
import json, sys, urllib.request, os

report_path = sys.argv[1]
images_dir  = sys.argv[2]

with open(report_path) as f:
    data = json.load(f)

blueprints = data.get("blueprints", [data])
bp = blueprints[0] if blueprints else {}
images = bp.get("images", {})

for ratio, meta in images.items():
    url = meta.get("url", "") if isinstance(meta, dict) else ""
    if not url:
        print(f"  ! No URL for {ratio}")
        continue
    filename = f"{ratio.replace('x','-')}.png"
    dest = os.path.join(images_dir, filename)
    try:
        urllib.request.urlretrieve(url, dest)
        size_kb = os.path.getsize(dest) // 1024
        print(f"  ✓ {filename} ({size_kb} KB)")
    except Exception as e:
        print(f"  ✗ {ratio}: {e}")
PYEOF

echo ""

# ── Final summary ─────────────────────────────────────────────────────────────
echo "════════════════════════════════════════════════════"
echo "  Test Results"
echo ""

APPROVAL_STATUS=$(python3 -c "
import json
with open('${RESULTS_DIR}/summary.json') as f:
    d = json.load(f)
print(d.get('approval_status','unknown'))
" 2>/dev/null || echo "unknown")

PASS1=$(python3 -c "
import json
with open('${RESULTS_DIR}/summary.json') as f:
    d = json.load(f)
print(d.get('compliance_pass1',{}).get('overall','unknown'))
" 2>/dev/null || echo "unknown")

PASS2=$(python3 -c "
import json
with open('${RESULTS_DIR}/summary.json') as f:
    d = json.load(f)
print(d.get('compliance_pass2',{}).get('overall','unknown'))
" 2>/dev/null || echo "unknown")

echo "  Campaign ID    : ${CAMPAIGN_ID}"
echo "  Product        : ${PRODUCT}"
echo "  Approval status: ${APPROVAL_STATUS}"
echo "  Compliance P1  : ${PASS1}"
echo "  Compliance P2  : ${PASS2}"
echo ""
echo "  Saved to: ${RESULTS_DIR}/"
echo "    ├── report.json    (full API response)"
echo "    ├── summary.json   (key fields extracted)"
echo "    └── images/"
echo "        ├── 1-1.png    (Instagram 1024×1024)"
echo "        ├── 9-16.png   (TikTok  720×1280)"
echo "        └── 16-9.png   (YouTube 1280×720)"
echo ""

# Exit with failure if compliance hard-blocked
if [[ "${APPROVAL_STATUS}" == "compliance_blocked" ]]; then
  warn "Campaign is compliance_blocked — Pass 2 failed. Review ${RESULTS_DIR}/summary.json"
  exit 2
fi

ok "Test complete"
echo "════════════════════════════════════════════════════"
echo ""
