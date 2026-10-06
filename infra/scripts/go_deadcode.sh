#!/usr/bin/env bash
# Runs deadcode over every module registered in go.work and fails if ANY
# module fails, or if deadcode reports a function that is not listed in
# go_deadcode_allow.txt. deadcode itself exits 0 when it finds dead code, so
# without that check its findings were printed and never blocked anything.
# An allowlist entry that deadcode no longer reports fails too, so the list
# cannot go stale.
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

allow_file="$root/infra/scripts/go_deadcode_allow.txt"
allowed=""
if [[ -f "$allow_file" ]]; then
    allowed="$(grep -v '^[[:space:]]*\(#\|$\)' "$allow_file" | sort -u || true)"
fi

failed=()
found=""
while IFS= read -r dir; do
    echo "=== deadcode $dir ==="
    if ! out="$(cd "$root/$dir" && "$bin" ./...)"; then
        failed+=("$dir")
    fi
    if [[ -n "$out" ]]; then
        echo "$out"
        # "internal/x.go:17:6: unreachable func: Name" -> "apps/m/internal/x.go Name"
        found+="$(sed -E "s|^([^:]+):[0-9]+:[0-9]+: unreachable func: (.+)$|${dir#./}/\1 \2|" <<< "$out")"$'\n'
    fi
done <<< "$modules"

if ((${#failed[@]} > 0)); then
    echo "go_deadcode.sh: deadcode failed in: ${failed[*]}" >&2
    exit 1
fi

found="$(grep -v '^$' <<< "$found" | sort -u || true)"
new="$(comm -23 <(echo "$found") <(echo "$allowed") | grep -v '^$' || true)"
stale="$(comm -13 <(echo "$found") <(echo "$allowed") | grep -v '^$' || true)"
if [[ -n "$new" ]]; then
    echo "go_deadcode.sh: unreachable functions (delete them, or list them with a reason in infra/scripts/go_deadcode_allow.txt):" >&2
    echo "$new" | sed 's/^/  /' >&2
fi
if [[ -n "$stale" ]]; then
    echo "go_deadcode.sh: allowlist entries deadcode no longer reports (remove them):" >&2
    echo "$stale" | sed 's/^/  /' >&2
fi
if [[ -n "$new" || -n "$stale" ]]; then
    exit 1
fi
echo "go_deadcode.sh: no unlisted dead code"
