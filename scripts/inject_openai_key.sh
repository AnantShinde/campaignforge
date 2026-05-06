#!/usr/bin/env bash
# inject_openai_key.sh — write the real OpenAI API key into Secrets Manager
#
# Usage:
#   ./scripts/inject_openai_key.sh [env] [api_key]
#
# If api_key is omitted you will be prompted (key not echoed to terminal).
#
# The Secrets stack creates the secret with a placeholder value "REPLACE_ME".
# The compliance Lambdas fetch the key once per cold start via Secrets Manager
# — never via environment variables.

set -euo pipefail

ENV="${1:-dev}"
API_KEY="${2:-}"
AWS_PROFILE="${AWS_PROFILE:-campaignforge}"
REGION="${AWS_DEFAULT_REGION:-us-east-1}"

GREEN='\033[0;32m'; RED='\033[0;31m'; NC='\033[0m'
ok()   { echo -e "${GREEN}  ✓ $*${NC}"; }
fail() { echo -e "${RED}  ✗ $*${NC}"; exit 1; }

SECRET_NAME="campaignforge/${ENV}/openai-api-key"

if [[ -z "${API_KEY}" ]]; then
  echo ""
  echo "  Enter your OpenAI API key (input hidden):"
  read -rsp "  sk-..." API_KEY
  echo ""
fi

[[ -z "${API_KEY}" ]] && fail "API key cannot be empty"
[[ "${API_KEY}" == "REPLACE_ME" ]] && fail "That is still the placeholder — enter your real key"

echo ""
echo "── Injecting OpenAI key into Secrets Manager ──"
echo "   Secret: ${SECRET_NAME}"

aws secretsmanager put-secret-value \
  --secret-id "${SECRET_NAME}" \
  --secret-string "${API_KEY}" \
  --profile "${AWS_PROFILE}" \
  --region "${REGION}" \
  --output json > /dev/null

ok "Key injected. The compliance Lambdas will pick it up on next cold start."
echo ""
echo "  Verifying (first 5 chars only):"
STORED=$(aws secretsmanager get-secret-value \
  --secret-id "${SECRET_NAME}" \
  --profile "${AWS_PROFILE}" \
  --region "${REGION}" \
  --query SecretString --output text 2>/dev/null | cut -c1-5)
echo "    Stored key starts with: ${STORED}..."
echo ""
