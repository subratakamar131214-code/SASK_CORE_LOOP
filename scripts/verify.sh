#!/usr/bin/env bash
# Full local verification. Fails closed on lint, types, tests, secrets, or invariants.
set -euo pipefail

cd "$(dirname "$0")/.."
python_bin="${PYTHON:-python}"

"$python_bin" -m src.core.invariants --check
"$python_bin" -m ruff check src tests
"$python_bin" -m mypy src
"$python_bin" -m pytest -q
"$python_bin" -m src.compliance.secretscan --tree .
"$python_bin" -m src.orchestrator --status --dry-run > /dev/null

echo "verification passed"
