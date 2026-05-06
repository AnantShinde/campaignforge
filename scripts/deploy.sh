#!/usr/bin/env bash
# deploy.sh — deploy all CampaignForge CDK stacks in dependency order
#
# Usage:
#   ./scripts/deploy.sh [env]             # env defaults to "dev"
#   ./scripts/deploy.sh dev --dry-run     # synth only, no deploy
#
# Stack deploy order (enforced by CDK add_dependency, but explicit here for clarity):
#   1. Secrets   — Secrets Manager (OpenAI key placeholder)
#   2. Storage   — S3 × 3 + DynamoDB
#   3. Ai        — OpenSearch Serverless + Bedrock KB + Guardrail
#   4. Pipeline  — SQS + EventBridge Pipe + Step Functions
#   5. Api       — HTTP API v2 + Cognito + Lambda functions

set -euo pipefail

ENV="${1:-dev}"
DRY_RUN="${2:-}"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
ok()    { echo -e "${GREEN}  ✓ $*${NC}"; }
info()  { echo -e "${CYAN}  → $*${NC}"; }
warn()  { echo -e "${YELLOW}  ! $*${NC}"; }
fail()  { echo -e "${RED}  ✗ $*${NC}"; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "${SCRIPT_DIR}")"
cd "${PROJECT_ROOT}"

source .venv/bin/activate 2>/dev/null || fail ".venv not found — run scripts/bootstrap.sh first"

STACKS=(
  "CampaignForge-${ENV}-Secrets"
  "CampaignForge-${ENV}-Storage"
  "CampaignForge-${ENV}-Ai"
  "CampaignForge-${ENV}-Pipeline"
  "CampaignForge-${ENV}-Api"
)

echo ""
echo "════════════════════════════════════════════════════"
echo "  CampaignForge Deploy  [env=${ENV}]"
[[ -n "${DRY_RUN}" ]] && echo "  MODE: DRY RUN (synth only)"
echo "════════════════════════════════════════════════════"
echo ""

# ── Synth all stacks first ───────────────────────────────────────────────────
info "Synthesising all stacks..."
npx aws-cdk@latest synth --context env="${ENV}" --quiet
ok "Synth passed"
echo ""

[[ -n "${DRY_RUN}" ]] && { warn "Dry run — skipping deploy"; exit 0; }

# ── Deploy each stack ────────────────────────────────────────────────────────
DEPLOYED=()
FAILED=()

for STACK in "${STACKS[@]}"; do
  echo "── Deploying ${STACK} ──"
  if npx aws-cdk@latest deploy "${STACK}" \
      --context env="${ENV}" \
      --require-approval never \
      --outputs-file "cdk.out/${STACK}-outputs.json" 2>&1; then
    ok "${STACK} deployed"
    DEPLOYED+=("${STACK}")
  else
    fail "${STACK} failed — stopping"
    FAILED+=("${STACK}")
    break
  fi
  echo ""
done

# ── Summary ──────────────────────────────────────────────────────────────────
echo "════════════════════════════════════════════════════"
echo "  Deploy Summary"
echo ""
for S in "${DEPLOYED[@]}"; do ok "${S}"; done
for S in "${FAILED[@]}";   do echo -e "${RED}  ✗ ${S}${NC}"; done
echo ""

if [[ ${#FAILED[@]} -eq 0 ]]; then
  SECRETS_OUT="cdk.out/CampaignForge-${ENV}-Secrets-outputs.json"
  AI_OUT="cdk.out/CampaignForge-${ENV}-Ai-outputs.json"

  echo "  Next steps:"
  echo ""
  echo "  1. Inject your OpenAI key:"
  echo "       ./scripts/inject_openai_key.sh ${ENV} sk-YOUR_KEY_HERE"
  echo ""
  echo "  2. Upload RAG documents and seed product images:"
  echo "       ./scripts/sync_kb.sh ${ENV}"
  echo ""
  echo "  3. (optional) Check the API URL:"
  if [[ -f "cdk.out/CampaignForge-${ENV}-Api-outputs.json" ]]; then
    API_URL=$(python3 -c "
import json, sys
data = json.load(open('cdk.out/CampaignForge-${ENV}-Api-outputs.json'))
key = next((k for k in data if 'Api' in k), None)
if key:
    vals = data[key]
    url = next((v for k,v in vals.items() if 'Url' in k or 'Endpoint' in k), None)
    print(url or '(see cdk.out)')
" 2>/dev/null || echo "(see cdk.out/CampaignForge-${ENV}-Api-outputs.json)")
    echo "       ${API_URL}"
  fi
fi

echo "════════════════════════════════════════════════════"
echo ""
