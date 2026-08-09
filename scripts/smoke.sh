#!/usr/bin/env bash
#
# Fresh-venv install smoke test.
#
# Proves the claim the README makes: a clean checkout installs from nothing,
# puts a working `sbx` on PATH, exposes exactly four verbs, and passes its own
# test suite. CI runs this on every push; run it yourself before asking anyone
# to review the repo.
#
# Usage:  scripts/smoke.sh
#         PYTHON=/path/to/python3.11 scripts/smoke.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-python3}"

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT
VENV="$WORKDIR/venv"

step() { printf '\n==> %s\n' "$1"; }

step "checking $PYTHON is 3.11+"
if ! "$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
    echo "smoke: sbx needs Python 3.11+; re-run with PYTHON=/path/to/python3.11" >&2
    exit 1
fi
"$PYTHON" --version

step "creating a throwaway venv"
"$PYTHON" -m venv "$VENV"
"$VENV/bin/python" -m pip install --quiet --upgrade pip

step "installing the package from a clean tree"
"$VENV/bin/python" -m pip install --quiet --editable "$REPO_ROOT[dev]"

step "the console script exists and reports its version"
"$VENV/bin/sbx" --version

step "exactly four verbs, no more"
# Asserted against the command registry rather than scraped from --help, so
# this checks the same source of truth the CLI itself is built from.
"$VENV/bin/python" - <<'PY'
from sbx.cli import VERBS

expected = ["ls", "run", "seal", "verify"]
assert sorted(VERBS) == expected, f"verb set drifted: {sorted(VERBS)} != {expected}"
print("verbs:", " ".join(VERBS))
PY

step "running the test suite"
cd "$REPO_ROOT"
"$VENV/bin/python" -m pytest -q

step "smoke passed"
