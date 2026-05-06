#!/usr/bin/env bash
# package_lambdas.sh — install Python dependencies into Lambda directories
#
# Packages are built for Linux x86_64 (Lambda runtime) using --platform.
# This avoids the macOS vs Linux C-extension mismatch (pydantic_core, etc.)
#
# Usage:
#   ./scripts/package_lambdas.sh

set -euo pipefail

GREEN='\033[0;32m'; CYAN='\033[0;36m'; YELLOW='\033[1;33m'; NC='\033[0m'
ok()   { echo -e "${GREEN}  ✓ $*${NC}"; }
info() { echo -e "${CYAN}  → $*${NC}"; }
warn() { echo -e "${YELLOW}  ! $*${NC}"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "${SCRIPT_DIR}")"
cd "${PROJECT_ROOT}"

echo ""
echo "════════════════════════════════════════════════════"
echo "  CampaignForge Lambda Packager"
echo "  Target: Linux x86_64 (Lambda runtime)"
echo "════════════════════════════════════════════════════"
echo ""

# GenerateCampaign — needs openai (for copy gen + compliance) and Pillow (for text overlay)
TARGET="lambdas/generate_campaign"

info "Installing openai + google-genai + Pillow into ${TARGET}..."

# Install for Linux x86_64 — --only-binary=:all: ensures we get
# pre-built wheels (no C compilation needed on macOS)
pip install openai "google-genai>=1.0.0" Pillow \
  --target "${TARGET}" \
  --platform manylinux2014_x86_64 \
  --python-version 3.12 \
  --implementation cp \
  --only-binary=:all: \
  --upgrade \
  --quiet \
  --no-cache-dir

ok "openai + google-genai + Pillow installed for Linux x86_64"

# Verify key modules are present
for MOD in openai google pydantic_core PIL; do
  if ls "${TARGET}/${MOD}" > /dev/null 2>&1; then
    ok "  ${MOD} present"
  else
    warn "  ${MOD} NOT found — check pip output above"
  fi
done

echo ""
echo "════════════════════════════════════════════════════"
echo "  Done. Run: AWS_PROFILE=campaignforge ./scripts/deploy.sh dev"
echo "════════════════════════════════════════════════════"
echo ""
