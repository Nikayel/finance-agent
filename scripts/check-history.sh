#!/usr/bin/env bash
#
# Check that the history says what it claims.
#
# This repo is built test-first, and the commit log is supposed to prove it:
# a commit whose subject begins "Add failing" introduces tests with no
# implementation behind them and MUST be red; every other commit MUST be green.
# A project about verifying claims should verify its own.
#
# Each commit is checked out into a throwaway git worktree and its tests run
# with PYTHONPATH pointed at that worktree's `src`, which shadows the editable
# install — so one venv checks the whole history instead of one venv per commit.
#
# Usage:  scripts/check-history.sh [<rev-range>]     (default: all of HEAD)
#         PYTHON=/path/to/python3.11 scripts/check-history.sh

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RANGE="${1:-HEAD}"
PYTHON="${PYTHON:-$REPO_ROOT/.venv/bin/python}"

if ! "$PYTHON" -c 'import pytest' >/dev/null 2>&1; then
    echo "check-history: $PYTHON cannot import pytest" >&2
    echo "  locally: python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'" >&2
    echo "  or point PYTHON= at an interpreter that has it" >&2
    exit 1
fi

WORKDIR="$(mktemp -d)"
WORKTREE="$WORKDIR/tree"
cleanup() {
    git -C "$REPO_ROOT" worktree remove --force "$WORKTREE" >/dev/null 2>&1
    rm -rf "$WORKDIR"
}
trap cleanup EXIT

# Oldest first, so a failure reads as "the history broke here".
COMMITS="$(git -C "$REPO_ROOT" rev-list --reverse "$RANGE")"
git -C "$REPO_ROOT" worktree add --detach --quiet "$WORKTREE" HEAD

failures=0
checked=0

for commit in $COMMITS; do
    subject="$(git -C "$REPO_ROOT" log -1 --format=%s "$commit")"
    case "$subject" in
        "Add failing"*) expected="red" ;;
        *)              expected="green" ;;
    esac

    git -C "$WORKTREE" checkout --detach --quiet --force "$commit"
    git -C "$WORKTREE" clean -qxdf

    if [ ! -d "$WORKTREE/tests" ]; then
        continue  # before there was a suite to run
    fi

    PYTHONPATH="$WORKTREE/src" "$PYTHON" -m pytest "$WORKTREE/tests" \
        -q -p no:cacheprovider >"$WORKDIR/out" 2>&1
    status=$?

    # pytest exits 5 when it collected nothing; that is not a broken commit.
    if [ "$status" -eq 0 ] || [ "$status" -eq 5 ]; then
        actual="green"
    else
        actual="red"
    fi

    checked=$((checked + 1))
    if [ "$actual" = "$expected" ]; then
        printf '  %s  %-5s  %s\n' "${commit:0:8}" "$actual" "$subject"
    else
        failures=$((failures + 1))
        printf '! %s  %-5s  %s  (expected %s)\n' \
            "${commit:0:8}" "$actual" "$subject" "$expected"
        sed -n '$p' "$WORKDIR/out" | sed 's/^/      /'
    fi
done

echo
if [ "$failures" -eq 0 ]; then
    echo "history is honest: $checked commits, each as red or green as it claims"
    exit 0
fi
echo "history is not honest: $failures of $checked commits disagree with their subject" >&2
exit 1
