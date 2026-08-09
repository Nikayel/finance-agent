#!/usr/bin/env bash
#
# The demo. One command, on a fresh clone, no manual steps.
#
#     git clone https://github.com/Nikayel/finance-agent.git
#     cd finance-agent && demo/demo.sh
#
# It installs into a throwaway venv and points HOME at a throwaway directory,
# so it neither needs nor touches anything of yours — including your real
# ~/.sbx. Everything it makes is deleted when it exits.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-python3}"

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT
export HOME="$WORKDIR/home"
mkdir -p "$HOME"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
note() { printf '%s\n' "$*"; }

say "0. install into a throwaway venv"
if ! "$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
    echo "demo: sbx needs Python 3.11+; re-run with PYTHON=/path/to/python3.11" >&2
    exit 1
fi
"$PYTHON" -m venv "$WORKDIR/venv"
"$WORKDIR/venv/bin/python" -m pip install --quiet --upgrade pip
"$WORKDIR/venv/bin/python" -m pip install --quiet --editable "$REPO_ROOT"
SBX="$WORKDIR/venv/bin/sbx"
"$SBX" --version

say "1. make a journal and seal it"
note "The dataset becomes immutable and is named by the hash of its own bytes."
"$WORKDIR/venv/bin/python" "$REPO_ROOT/demo/make_journal.py" "$WORKDIR/events.jsonl"
"$SBX" seal "$WORKDIR/events.jsonl"
DATA="$(ls "$HOME/.sbx/datasets")"

say "2. a strategy goes looking for data it has not been shown"
note "This is the attack the whole architecture is built against."
"$SBX" run "$REPO_ROOT/demo/peeker.py" --data "${DATA:0:8}" --seed 1

say "3. a strategy with a great-looking number that came from nowhere"
note "Nothing malicious. It sizes its orders from the wall clock, which is the"
note "ordinary way an agent-written backtest turns an accident into a claim."
"$SBX" run "$REPO_ROOT/demo/cheater.py" --data "${DATA:0:8}" --seed 42

say "4. ask sbx to reproduce that result"
note "This is the one command the whole project exists for."
if "$SBX" verify run-0002; then
    echo "demo: the cheating run reproduced, which should be impossible" >&2
    exit 1
fi
note ""
note "-- caught. The number was never a measurement."

say "5. the honest version of the same idea"
"$SBX" run "$REPO_ROOT/demo/honest.py" --data "${DATA:0:8}" --seed 42

say "6. reproduce it"
"$SBX" verify run-0003

say "7. the ledger"
"$SBX" ls

say "demo complete"
note "Everything above ran in a throwaway HOME and has already been deleted."
