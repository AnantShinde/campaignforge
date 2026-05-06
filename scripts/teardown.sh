#!/usr/bin/env bash
# teardown.sh — destroy all CampaignForge stacks (reverse order)
#
# Usage:
#   ./scripts/teardown.sh [env]
#
# WARNING: This deletes all deployed resources except S3 buckets and DynamoDB
# tables (which have RemovalPolicy.RETAIN). Empty and delete those manually
# in the AWS Console if you want a full clean slate.

set -euo pipefail

ENV="${1:-dev}"

RED='\033[0;31m'; YELLOW='\033[1;33m'; GREEN='\033[0;32m'; NC='\033[0m'
ok()   { echo -e "${GREEN}  ✓ $*${NC}"; }
warn() { echo -e "${YELLOW}  ! $*${NC}"; }
fail() { echo -e "${RED}  ✗ $*${NC}"; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "${SCRIPT_DIR}")"
cd "${PROJECT_ROOT}"

source .venv/bin/activate 2>/dev/null || fail ".venv not found"

echo ""
echo "════════════════════════════════════════════════════"
echo "  CampaignForge Teardown  [env=${ENV}]"
echo ""
warn "This will DESTROY all stacks for env '${ENV}'."
warn "S3 buckets and DynamoDB tables are RETAINED (RemovalPolicy.RETAIN)."
warn "Delete them manually in the console for a full clean slate."
echo ""
read -rp "  Type '${ENV}' to confirm: " CONFIRM
[[ "${CONFIRM}" == "${ENV}" ]] || { warn "Cancelled"; exit 0; }
echo ""

# Destroy in reverse dependency order
STACKS=(
  "CampaignForge-${ENV}-Api"
  "CampaignForge-${ENV}-Pipeline"
  "CampaignForge-${ENV}-Ai"
  "CampaignForge-${ENV}-Storage"
  "CampaignForge-${ENV}-Secrets"
)

for STACK in "${STACKS[@]}"; do
  echo "── Destroying ${STACK} ──"
  npx aws-cdk@latest destroy "${STACK}" \
    --context env="${ENV}" \
    --force 2>&1 && ok "${STACK} destroyed" || warn "${STACK} failed (may not exist)"
  echo ""
done

echo "════════════════════════════════════════════════════"
echo "  Teardown complete."
echo "  Retained resources (delete manually if needed):"
echo "    S3 buckets: campaignforge-${ENV}-ragdocs"
echo "                campaignforge-${ENV}-assetsinput"
echo "                campaignforge-${ENV}-outputs"
echo "    DynamoDB:   campaignforge-${ENV}-campaigns"
echo "    Secret:     campaignforge/${ENV}/openai-api-key"
echo "════════════════════════════════════════════════════"
echo ""
