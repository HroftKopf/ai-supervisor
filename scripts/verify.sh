#!/usr/bin/env bash
# Run local static checks and tests without network or API keys.
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ ! -x .venv/bin/python ]]; then
  echo "error: .venv is missing; see docs/INSTALL.md" >&2
  exit 1
fi
.venv/bin/ruff check .
.venv/bin/pytest -q
echo "OK: ruff and pytest passed"
