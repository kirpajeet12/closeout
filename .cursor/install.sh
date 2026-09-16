#!/usr/bin/env bash
# Idempotent Cloud Agent bootstrap for Closeout.
# System packages: poppler-utils (pdftoppm) renders drawing sheets; Python 3.13 is the
# interpreter the project targets (see README "Run it locally").
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

sudo apt-get update -qq
sudo apt-get install -y --no-install-recommends poppler-utils software-properties-common

if ! command -v python3.13 >/dev/null 2>&1; then
  sudo add-apt-repository -y ppa:deadsnakes/ppa
  sudo apt-get update -qq
  sudo apt-get install -y --no-install-recommends python3.13 python3.13-venv python3.13-dev
fi

if [ ! -x .venv/bin/python ] || ! .venv/bin/python --version 2>/dev/null | grep -q "3.13"; then
  rm -rf .venv
  python3.13 -m venv .venv
fi

.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

# Local config: Bedrock provider by default; AI features need AWS credentials, but the
# drawing/field-review/report smoke path runs without them.
[ -f .env ] || cp .env.example .env

echo "Closeout environment ready. Start the server with:"
echo "  .venv/bin/python -m uvicorn closeout.api:app --host 0.0.0.0 --port 8765"
