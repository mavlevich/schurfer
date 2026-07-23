# CCXT-002: Adopt released XT support in Schurfer

> Status: blocked on an upstream merge and published CCXT release
> Depends on: [CCXT-001](001-xt-fetch-open-interest.md)
> Produces: a dependency upgrade and safe removal of Schurfer's XT fallback

## Goal

Move Schurfer from its private raw-endpoint adapter to CCXT's released unified
`fetch_open_interest` implementation without creating a collection gap or silently
changing stored units.

## Preconditions

- The upstream PR is merged.
- A published CCXT version contains the change.
- Release notes or the packaged adapter confirm
  `exchange.has["fetchOpenInterest"]`.
- The released parser's amount, value, and timestamp semantics match the behavior
  validated in the upstream task.

Do not remove the local fallback against an unreleased master commit.

## Current Schurfer behavior

Schurfer:

- prefers native `fetch_open_interest` when the exchange advertises support;
- otherwise routes XT through a local public raw-endpoint fallback;
- validates response shape and timestamp freshness;
- stores the USD value in `app.oi_snapshots`;
- bounds concurrent requests and request duration;
- has regression tests for native preference, fallback parsing, stale responses,
  future timestamps, exchange errors, and unsupported exchanges.

Relevant files:

- [`apps/analytics/schurfer_analytics/oi.py`](../../../apps/analytics/schurfer_analytics/oi.py)
- [`apps/analytics/tests/test_oi.py`](../../../apps/analytics/tests/test_oi.py)
- [`pyproject.toml`](../../../pyproject.toml)
- [`uv.lock`](../../../uv.lock)

## Work

1. Upgrade CCXT in an isolated Schurfer branch.
2. Regenerate `uv.lock`.
3. Assert the packaged XT adapter reports native support.
4. Run the existing pipeline against mocked unified responses.
5. Run a public XT smoke collection locally.
6. Compare old fallback and new unified results for the same symbols:
   - USD OI value;
   - amount;
   - timestamp;
   - symbol selection.
7. Remove only:
   - `_fetch_xt_open_interest`;
   - XT's entry in `OPEN_INTEREST_FALLBACKS`;
   - fallback-only parser tests.
8. Keep application-level policies:
   - timeout;
   - concurrency bound;
   - freshness/future-skew validation where still required;
   - structured error logging;
   - durable database writes.
9. Add a regression test proving XT now takes the native path.
10. Deploy analytics only and monitor at least three scan cycles.

## Production verification

Confirm:

```text
oi.fetch_failed does not increase for XT
oi.fetched continues to include XT targets
persistence.oi_snapshots_inserted remains non-zero
recent app.oi_snapshots rows exist where exchange = 'xt'
```

Compare values against the pre-upgrade range. A unit mismatch can look “healthy”
while poisoning research data, so non-zero rows alone are insufficient.

## Rollback

- Dependency downgrade plus fallback restoration must remain a single revertable
  commit/PR boundary.
- If native values differ materially or timestamps are missing, restore the local
  fallback and report the released behavior upstream with a minimal reproduction.

## Acceptance criteria

- Released CCXT native XT support is used.
- Stored `oi_usd` remains semantically and numerically consistent.
- Existing freshness and durability protections remain.
- Analytics tests pass.
- Three or more production scan cycles persist valid XT rows.
- No local XT raw-endpoint adapter remains.
- Rollback is documented in the PR.
