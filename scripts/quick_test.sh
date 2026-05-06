#!/usr/bin/env bash
# quick_test.sh — minimal smoke test: 1 brief → 1 image (1×1 only)
#
# Cost: ~$0.09  (1 Nova Canvas call + GPT-4o mini copy + 2 compliance passes)
# Use this first after deploying to confirm the pipeline works end-to-end
# before running the full test_campaign.sh ($0.49 for all 3 ratios).
#
# Usage:
#   ./scripts/quick_test.sh [env]
#
# How it works:
#   Temporarily sets RATIOS=1x1 env var which image_gen/handler.py reads
#   to skip 9x16 and 16x9 generation. Submits one hardcoded brief.

set -euo pipefail

ENV="${1:-dev}"
AWS_PROFILE="${AWS_PROFILE:-campaignforge}"
AWS_REGION="${AWS_DEFAULT_REGION:-us-east-1}"
POLL_INTERVAL=5
TIMEOUT_SECONDS=300

RED='\033[0;31m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'; YELLOW='\033[1;33m'; NC='\033[0m'
ok()   { echo -e "${GREEN}  ✓ $*${NC}"; }
info() { echo -e "${CYAN}  → $*${NC}"; }
fail() { echo -e "${RED}  ✗ $*${NC}"; exit 1; }
warn() { echo -e "${YELLOW}  ! $*${NC}"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "${SCRIPT_DIR}")"

echo ""
echo "════════════════════════════════════════════"
echo "  CampaignForge Quick Smoke Test"
echo "  1 brief · 1 image (1×1) · ~\$0.09"
echo "════════════════════════════════════════════"
echo ""

# ── Resolve outputs ──────────────────────────────────────────────────────────
API_URL=$(aws cloudformation describe-stacks \
  --stack-name "CampaignForge-${ENV}-Api" \
  --profile "${AWS_PROFILE}" --region "${AWS_REGION}" \
  --query "Stacks[0].Outputs[?contains(OutputKey,'ApiUrl')].OutputValue" \
  --output text 2>/dev/null)

USER_POOL_ID=$(aws cloudformation describe-stacks \
  --stack-name "CampaignForge-${ENV}-Api" \
  --profile "${AWS_PROFILE}" --region "${AWS_REGION}" \
  --query "Stacks[0].Outputs[?contains(OutputKey,'UserPoolId')].OutputValue" \
  --output text 2>/dev/null)

CLIENT_ID=$(aws cloudformation describe-stacks \
  --stack-name "CampaignForge-${ENV}-Api" \
  --profile "${AWS_PROFILE}" --region "${AWS_REGION}" \
  --query "Stacks[0].Outputs[?contains(OutputKey,'WebClientId')].OutputValue" \
  --output text 2>/dev/null)

[[ -z "${API_URL}" ]] && fail "Api stack not deployed. Run: ./scripts/deploy.sh ${ENV}"

ok "API: ${API_URL}"

# ── Cognito test user ────────────────────────────────────────────────────────
TEST_EMAIL="testuser-${ENV}@campaignforge.internal"
TEST_PASS="CfTest@2025!"

aws cognito-idp admin-create-user \
  --user-pool-id "${USER_POOL_ID}" --username "${TEST_EMAIL}" \
  --temporary-password "${TEST_PASS}" --message-action SUPPRESS \
  --profile "${AWS_PROFILE}" --region "${AWS_REGION}" > /dev/null 2>&1 || true

aws cognito-idp admin-set-user-password \
  --user-pool-id "${USER_POOL_ID}" --username "${TEST_EMAIL}" \
  --password "${TEST_PASS}" --permanent \
  --profile "${AWS_PROFILE}" --region "${AWS_REGION}" > /dev/null 2>&1

JWT=$(aws cognito-idp initiate-auth \
  --auth-flow USER_PASSWORD_AUTH \
  --client-id "${CLIENT_ID}" \
  --auth-parameters USERNAME="${TEST_EMAIL}",PASSWORD="${TEST_PASS}" \
  --profile "${AWS_PROFILE}" --region "${AWS_REGION}" \
  --query 'AuthenticationResult.IdToken' --output text 2>/dev/null)

[[ -z "${JWT}" || "${JWT}" == "None" ]] && fail "Auth failed — check USER_PASSWORD_AUTH is enabled on app client"
ok "Authenticated"

# ── Submit brief ─────────────────────────────────────────────────────────────
info "Submitting brief (1×1 only)..."

# RATIOS env var limits image_gen to 1×1 only when set
BRIEF='{"product_name":"Ergo Desk Pro","region":"united states","audience":"professionals","message":"Work smarter","language":"en","_ratios":"1x1"}'

RESP=$(curl -s -X POST "${API_URL}/brief" \
  -H "Authorization: Bearer ${JWT}" \
  -H "Content-Type: application/json" \
  -d "${BRIEF}")

CAMPAIGN_ID=$(echo "${RESP}" | python3 -c "import sys,json; print(json.load(sys.stdin).get('campaign_id',''))" 2>/dev/null)
[[ -z "${CAMPAIGN_ID}" ]] && { fail "Submission failed: ${RESP}"; }
ok "Campaign: ${CAMPAIGN_ID}"

# ── Poll ─────────────────────────────────────────────────────────────────────
echo ""
info "Polling every ${POLL_INTERVAL}s (timeout ${TIMEOUT_SECONDS}s)..."
START=$(date +%s)

while true; do
  ELAPSED=$(( $(date +%s) - START ))
  [[ ${ELAPSED} -ge ${TIMEOUT_SECONDS} ]] && fail "Timed out after ${ELAPSED}s"

  POLL=$(curl -s "${API_URL}/campaigns/${CAMPAIGN_ID}" -H "Authorization: Bearer ${JWT}")
  STATUS=$(echo "${POLL}" | python3 -c "
import sys,json; d=json.load(sys.stdin)
b=d.get('blueprints',[d]); print(b[0].get('status','unknown') if b else 'unknown')
" 2>/dev/null)

  APPROVAL=$(echo "${POLL}" | python3 -c "
import sys,json; d=json.load(sys.stdin)
b=d.get('blueprints',[d]); print(b[0].get('approval_status','') if b else '')
" 2>/dev/null)

  printf "  [%3ds]  status=%-10s  approval=%s\r" "${ELAPSED}" "${STATUS}" "${APPROVAL}"

  if [[ "${STATUS}" == "complete" ]]; then
    echo ""; ok "Done in ${ELAPSED}s"
    break
  elif [[ "${STATUS}" == "failed" ]]; then
    echo ""
    REASON=$(echo "${POLL}" | python3 -c "
import sys,json; d=json.load(sys.stdin)
b=d.get('blueprints',[d]); print(b[0].get('failure_reason','unknown') if b else 'unknown')
" 2>/dev/null)
    fail "Failed: ${REASON}"
  fi
  sleep "${POLL_INTERVAL}"
done

# ── Print result ─────────────────────────────────────────────────────────────
echo ""
python3 - <<PYEOF
import json, sys

resp = """${POLL}"""
data = json.loads(resp)
bp = data.get("blueprints", [data])[0] if data.get("blueprints") else data

print("════════════════════════════════════════════")
print("  Result")
print(f"  Campaign     : {bp.get('campaign_id','')}")
print(f"  Approval     : {bp.get('approval_status','')}")

c1 = bp.get("compliance_pass1", {}).get("overall", "—")
c2 = bp.get("compliance_pass2", {}).get("overall", "—")
print(f"  Compliance P1: {c1}")
print(f"  Compliance P2: {c2}")

imgs = bp.get("images", {})
for ratio, meta in imgs.items():
    url = meta.get("url","") if isinstance(meta,dict) else ""
    print(f"  Image {ratio:5s}   : {'✓ ' + url[:60] + '...' if url else '—'}")

copy = (bp.get("ad_copy") or [{}])[0]
print(f"  Headline     : {copy.get('headline','—')}")
print("════════════════════════════════════════════")
PYEOF
