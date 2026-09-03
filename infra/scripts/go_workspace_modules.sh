#!/usr/bin/env bash
# Prints the DiskPath of every module registered in go.work, one per line.
#
# The single source of truth for "what Go modules does this workspace
# contain" -- shared by `make verify`/`make deadcode` (via the Makefile's
# own GO_MODULE_DIRS, which shells out to this script) and every CI job
# that needs to loop over Go modules (test-go, security, deadcode in
# .github/workflows/ci.yml), so the two can never independently drift on
# how they parse go.work again.
#
# Colleague review, 2026-09-03, three rounds on the same underlying bug:
# 1. A hardcoded module-path list in the Makefile had already drifted out
#    of sync with go.work once (apps/market-hotset was missing).
# 2. The fix that replaced it with `grep '^use ' go.work | awk '{print $2}'`
#    only recognizes the single-line `use ./apps/foo` form -- a valid
#    `use (\n ./apps/foo\n ./apps/bar\n)` block would silently produce an
#    EMPTY list, repeating the exact same "CI/make stops checking a real
#    Go module" failure via a different go.work syntax. The Makefile was
#    fixed to use `go work edit -json` (go.work's own canonical parser,
#    handles both forms and any future one) instead of grep, but three CI
#    jobs (test-go, security, deadcode in ci.yml) were still independently
#    running the OLD `grep '^use '` one-liner -- a fix applied to only one
#    of the two places that needed it.
# 3. This script is the fix for THAT: one script, one parser, called from
#    every place that needs the module list, so a future go.work syntax
#    change (or a future fifth caller) can never drift from the others
#    again by construction.
#
# Piped through python3 (already a hard dependency everywhere else in this
# repo) rather than `jq`, which is not otherwise assumed present.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../.."

modules="$(go work edit -json | python3 -c \
    "import json,sys; print('\n'.join(m['DiskPath'] for m in json.load(sys.stdin)['Use']))")"

if [[ -z "$modules" ]]; then
    echo "go_workspace_modules.sh: go.work declares zero modules -- refusing to" >&2
    echo "silently return an empty list; check go.work itself" >&2
    exit 1
fi

printf '%s\n' "$modules"
