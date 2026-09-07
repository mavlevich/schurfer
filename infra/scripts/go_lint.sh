#!/usr/bin/env bash
# Runs golangci-lint over every module registered in go.work and fails if
# ANY module fails.
#
# This used to be an inline one-liner in .pre-commit-config.yaml:
#
#   grep "^use " go.work | awk "{print \$2}" | while read -r dir; do
#       (cd "$ROOT/$dir" && "$LINT" run --config "$ROOT/.golangci.yml" ./...)
#   done
#
# which had two independent ways of reporting a clean tree that was not
# clean (September audit, H-1/H-2):
#
# 1. A `while` loop's exit status is the status of its LAST iteration, so a
#    failing module was masked by any later module that passed. That was not
#    hypothetical: at the time this script was written apps/collector had 9
#    findings and apps/notifier 3, and the hook still exited 0 because
#    apps/market-hotset, the last module in go.work, is clean.
# 2. `grep '^use '` only recognizes the single-line `use ./apps/foo` form.
#    Against the equally valid `use (\n ./apps/foo\n)` block it yields an
#    EMPTY list, so the loop lints nothing and succeeds. That is the exact
#    parser drift infra/scripts/go_workspace_modules.sh was created to end;
#    this hook was simply never migrated to it.
#
# Every module is linted even after one fails, so a single run reports the
# whole tree rather than one module at a time.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../.."
root="$(pwd)"
config="$root/.golangci.yml"

lint="$(go env GOBIN)"
if [[ -z "$lint" ]]; then
    lint="$(go env GOPATH)/bin"
fi
lint="$lint/golangci-lint"

if [[ ! -x "$lint" ]]; then
    echo "go_lint.sh: golangci-lint not found at $lint -- run 'make install-golangci-lint'" >&2
    exit 1
fi

# The config is verified before any module runs. `golangci-lint run` ignores
# unknown keys silently, so a setting written at a v1 location under a v2
# config simply never applies while still reading as configured (audit M-10).
# `config verify` is what turns that into a failure.
"$lint" config verify --config "$config"

# Captured into a variable rather than piped or read from a process
# substitution: a failing go_workspace_modules.sh must fail this script.
# `set -e` does not see the exit status of a process substitution, so
# `done < <(go_workspace_modules.sh)` would loop over nothing and succeed --
# the same silent-empty-list failure this script exists to prevent.
modules="$(infra/scripts/go_workspace_modules.sh)"
if [[ -z "$modules" ]]; then
    echo "go_lint.sh: go_workspace_modules.sh returned no modules" >&2
    exit 1
fi

failed=()
while IFS= read -r dir; do
    echo "=== golangci-lint $dir ==="
    if ! (cd "$root/$dir" && "$lint" run --config "$config" ./...); then
        failed+=("$dir")
    fi
done <<< "$modules"

if ((${#failed[@]} > 0)); then
    echo "go_lint.sh: golangci-lint failed in: ${failed[*]}" >&2
    exit 1
fi
