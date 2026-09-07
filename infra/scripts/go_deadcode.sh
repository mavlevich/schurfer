#!/usr/bin/env bash
# Runs deadcode over every module registered in go.work and fails if ANY
# module fails.
#
# Same shape, and the same defect, as the golangci-lint wrapper next to it
# (see go_lint.sh's header): this loop lived inline in the Makefile's
# `deadcode` recipe, where a shell `for` loop's exit status is the status of
# its LAST iteration, so a module that failed early was masked by any later
# module that succeeded. `make verify` runs this, so the masking was in the
# pre-push gate too.
#
# Installing the pinned deadcode binary stays in the Makefile
# (`install-deadcode`); this script only runs it.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../.."
root="$(pwd)"

bin="$(go env GOBIN)"
if [[ -z "$bin" ]]; then
    bin="$(go env GOPATH)/bin"
fi
bin="$bin/deadcode"

if [[ ! -x "$bin" ]]; then
    echo "go_deadcode.sh: deadcode not found at $bin -- run 'make install-deadcode'" >&2
    exit 1
fi

# Captured into a variable, not read from a process substitution: `set -e`
# does not see a process substitution's exit status, so a failing module
# list would loop over nothing and succeed.
modules="$(infra/scripts/go_workspace_modules.sh)"
if [[ -z "$modules" ]]; then
    echo "go_deadcode.sh: go_workspace_modules.sh returned no modules" >&2
    exit 1
fi

failed=()
while IFS= read -r dir; do
    echo "=== deadcode $dir ==="
    if ! (cd "$root/$dir" && "$bin" ./...); then
        failed+=("$dir")
    fi
done <<< "$modules"

if ((${#failed[@]} > 0)); then
    echo "go_deadcode.sh: deadcode failed in: ${failed[*]}" >&2
    exit 1
fi
