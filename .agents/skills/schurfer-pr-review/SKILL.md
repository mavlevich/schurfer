---
name: schurfer-pr-review
description: Review Schurfer branches and pull requests for correctness, regressions, missing tests, migration risk, trading safety, and research-integrity failures. Use for code review or merge-readiness checks; do not use for implementing fixes unless the user also asks for changes.
---

# Schurfer PR Review

Review the complete proposed change, not only the files named by the author.

## Establish the review snapshot

1. Read `AI_RULES.md`.
2. Capture the current branch, `HEAD`, status, and relationship to
   `origin/main`.
3. Inspect all three layers separately: committed branch diff
   (`origin/main...HEAD`), staged diff, and unstaged/untracked work. State
   which layer the findings refer to. Tests execute the working tree, while a
   plain commit records only the index.
4. If the branch or files change during review, stop relying on earlier output
   and re-establish the snapshot. Never silently review a stale commit.

## Trace behavior across boundaries

- Read changed functions in full and inspect callers, models, migrations,
  configuration, Compose, Make targets, API/UI consumers, and tests affected
  by their contract.
- For execution changes, apply `schurfer-execution-safety`.
- For research/report changes, apply `schurfer-research-integrity`.
- Check recurring Schurfer failure classes: symbol/venue identity confusion,
  strategy-origin mixing, long/short sign errors, gross presented as net,
  Redis/DB divergence, restart duplicates, check-then-insert races, swallowed
  fail-closed errors, stale/partial data labelled complete, relative paths in
  disposable containers, and migrations tested only against mocks.
- Treat comments and docstrings as claims to verify against control flow,
  persistence, concurrency, deployment topology, and failure handling.

## Demand regression evidence

- Reproduce or encode every fixed bug with a test that fails on the old code.
- Cover success, empty/partial input, timeout/error, restart/retry,
  concurrency/idempotency, side direction, and stale identity when relevant.
- Database constraints, transactions, window functions, and migrations need
  a real PostgreSQL integration test. A skipped integration test is not a
  passing gate; inspect skip counts and CI's `REQUIRE_INTEGRATION_DB` path.
- Start with focused tests and static checks, then run the affected package
  suite. Use `make verify` for final cross-repository confidence when the
  scope/risk justifies it and dependencies are available.

## Report the result

- Lead with the merge verdict: approve, comment, or request changes.
- List only actionable findings, ordered P0 to P2, with tight file/line
  references, failure scenario, impact, and safe fix direction.
- Separate blockers from follow-ups. Do not invent findings to fill a list.
- Report exact commands that passed, skipped tests, and anything not verified.
- Do not edit the branch during review unless the user explicitly asks.
