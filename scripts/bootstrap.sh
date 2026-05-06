#!/usr/bin/env bash
# bootstrap.sh — one-time AWS + CDK setup for CampaignForge
# Run this ONCE before your first deploy.
#
# Usage:
#   chmod +x scripts/bootstrap.sh
#   ./scripts/bootstrap.sh [env]          # env defaults to "dev"
#
# What it does:
#   1. Verifies AWS credentials are configured
#   2. Prints account/region being used
#   3. Checks Bedrock model access for the 3 required models
#   4. Runs CDK bootstrap in the target account + region
#   5. Activates the Python venv and installs dependencies

set -euo pipefail

ENV="${1:-dev}"
REGION="${AWS_DEFAULT_REGION:-us-east-1}"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
ok()   { echo -e "${GREEN}  ✓ $*${NC}"; }
warn() { echo -e "${YELLOW}  ! $*${NC}"; }
fail() { echo -e "${RED}  ✗ $*${NC}"; exit 1; }

echo ""
echo "════════════════════════════════════════════════════"
echo "  CampaignForge Bootstrap  [env=${ENV}]  [region=${REGION}]"
echo "════════════════════════════════════════════════════"
echo ""

# ── 1. AWS credentials ──────────────────────────────────────────────────────
echo "── Checking AWS credentials ──"
ACCOUNT=$(aws sts get-caller-identity --query Account --output text 2>/dev/null) \
  || fail "AWS credentials not found. Run 'aws configure' first."
CALLER=$(aws sts get-caller-identity --query Arn --output text)
ok "Account : ${ACCOUNT}"
ok "Identity: ${CALLER}"
ok "Region  : ${REGION}"
echo ""

# ── 2. Bedrock model access ─────────────────────────────────────────────────
echo "── Checking Bedrock model access ──"
REQUIRED_MODELS=(
  "amazon.nova-canvas-image-generator-v2:0"
  "anthropic.claude-haiku-4-5"
  "amazon.titan-embed-text-v2:0"
)
ALL_OK=true
for MODEL in "${REQUIRED_MODELS[@]}"; do
  STATUS=$(aws bedrock get-foundation-model \
    --model-identifier "${MODEL}" \
    --region "${REGION}" \
    --query 'modelDetails.modelLifecycle.status' \
    --output text 2>/dev/null || echo "NOT_FOUND")

  ACCESS=$(aws bedrock list-foundation-models \
    --region "${REGION}" \
    --query "modelSummaries[?modelId=='${MODEL}'].modelAccessStatus" \
    --output text 2>/dev/null || echo "UNKNOWN")

  if [[ "${ACCESS}" == "ENABLED" ]]; then
    ok "${MODEL}"
  else
    warn "${MODEL} — access status: ${ACCESS:-UNKNOWN}"
    warn "   → Go to Bedrock console → Model access → Enable this model"
    ALL_OK=false
  fi
done

if [[ "${ALL_OK}" == "false" ]]; then
  echo ""
  warn "One or more Bedrock models need access approval."
  warn "Open: https://console.aws.amazon.com/bedrock/home#/modelaccess"
  warn "Enable the models listed above, then re-run this script."
  echo ""
  read -rp "Continue bootstrap anyway? (y/N): " CONT
  [[ "${CONT}" =~ ^[Yy]$ ]] || exit 1
fi
echo ""

# ── 3. Python venv + dependencies ───────────────────────────────────────────
echo "── Setting up Python environment ──"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "${SCRIPT_DIR}")"

cd "${PROJECT_ROOT}"

if [[ ! -d ".venv" ]]; then
  python3 -m venv .venv
  ok "Created .venv"
fi

# shellcheck source=/dev/null
source .venv/bin/activate
pip install -q -r requirements.txt
ok "Python dependencies installed"
echo ""

# ── 4. CDK bootstrap ────────────────────────────────────────────────────────
echo "── CDK bootstrap ──"
echo "   Target: aws://${ACCOUNT}/${REGION}"
npx aws-cdk@latest bootstrap "aws://${ACCOUNT}/${REGION}" \
  --context env="${ENV}" \
  --cloudformation-execution-policies arn:aws:iam::aws:policy/AdministratorAccess
ok "CDK bootstrap complete"
echo ""

echo "════════════════════════════════════════════════════"
echo "  Bootstrap done. Next step:"
echo ""
echo "    ./scripts/deploy.sh ${ENV}"
echo "════════════════════════════════════════════════════"
echo ""
