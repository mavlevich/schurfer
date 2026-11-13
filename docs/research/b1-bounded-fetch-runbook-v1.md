# B1 bounded supplemental premium transport

Status, 2026-11-13: implemented locally; **no supplemental market fetch run**.
This implements the transport portion of the
[scope amendment](b1-minute-scope-repair-proposal-v1.md), not an authorization to
fetch and not a release of B2. The registered fixed pilot is first. A separate
owner go is still needed before invoking a market-fetch action.

## Bound inputs and scope

`bybit_presettlement_bounded_fetch` verifies the frozen scope and pilot SHA-256,
the discovery index, and the original minute snapshot. There are exactly 11,660
additional requests, all 4h, including the 64 fixed pilot IDs. The remaining
8,185 paths in the 19,845-member S1 union remain in the original snapshot. No
hourly premium, new control, replacement event, outcome, test year or holdout is
used by this tool. The real classifier and all B1 report thresholds are unchanged.

`prepare` publishes `run.json` and `ledger.sqlite` together via a private staging
directory. It refuses any existing run directory, including an empty one. Resume
requires the same clean Git revision, source fingerprints, inputs, endpoint,
request IDs and limits. A missing ledger is an error, not a reason to reset it.
Only one process may hold the run's writer lock. A killed initialization can leave
its own hidden staging directory; that directory is never treated as a run.

## Durable transport rules

- One worker; at least 1/3 second between attempt starts, including retries and
  resumes. Only the fixed native Bybit premium endpoint is permitted; redirects
  are not followed. Each 4h path fits in one request, without pagination.
- At most two attempts per interval, 23,320 total and 128 for the pilot. An attempt
  commits to SQLite **before** opening HTTP. A kill consumes the reserved attempt.
- Before each body-iterator read, commit a 1 KiB reservation: 512 MiB globally and
  10 MiB for the pilot, with pilot charges included globally. Failed requests,
  retries, interrupted reservations and the final EOF probe consume budget; no
  refund occurs. These conservative reservations bound response body bytes exposed
  to the application before JSON parsing, not HTTP headers or socket buffers.
  HTTP content encoding must be identity, preventing hidden decompression growth.
- Budget exhaustion interrupts the current response. No successful snapshot is
  published from a budget-stopped run. Restarting cannot replenish either cap.
- Every finished attempt has an immutable receipt: run identity, exact request,
  HTTP status, fixed error reason, byte-exact native response encoded as base64,
  body checksum and validation result. Raw values are not printed. Transport
  exception URLs are not retained. A complete receipt saved before a crash in the
  ledger commit is adopted after its identity and checksum are revalidated.
- Wrong symbol/category/bounds, non-finite OHLC, malformed native data and
  conflicting duplicate OHLC fail closed. Identical duplicate rows fold. Gaps
  remain `incomplete`, with at most one retry; no zero filling or replacement.
- The fixed pilot passes only with all 64 IDs terminal, at least 61 complete and
  no integrity failure. Its immutable result cannot be resampled after a failure.
  Full fetching requires that pass, reuses successful pilot paths, and preserves
  terminal missing pilot paths in the denominator.
- `seal` streams deterministic gzip (`mtime=0`) in fixed ID order and publishes a
  SHA-256 manifest. It includes explicit missing-path records after exhausted
  retries. It refuses invalid or nonterminal paths. The original files are never
  modified. Subsequent B1 loading/merging still rejects conflicting native OHLC.

SQLite uses full synchronous commits. On resume, counters are reconciled against
all charged attempts, receipt digests are verified, and orphan/missing receipts
are refused. Back up the **whole run directory**; copying only `run.json` or a
snapshot is not sufficient for a resumable run.

## Commands after review and fetch authorization

Run from the committed, clean canonical worktree. The same arguments and revision
are required for every action; keep the output under ignored `runtime/`.

```sh
uv run --package schurfer-analytics python -m schurfer_analytics.bybit_presettlement_bounded_fetch prepare \
  --run runtime/research/b1/prior-only-4h \
  --scope docs/research/evidence/b1-minute-scope-repair-v1/four-hour-scope.json.gz \
  --pilot docs/research/evidence/b1-minute-scope-repair-v1/proposed-pilot.json.gz \
  --index /absolute/path/to/index-d87b3a2.jsonl.gz \
  --base-minutes /absolute/path/to/minutes-7260e55.jsonl.gz
```

`prepare` is offline. With the identical arguments, replace the action with:

1. `pilot`: fetch only the fixed 64 IDs and persist `pilot.json`.
2. `status`: recover the local ledger and print counters, without network calls.
3. `full`: only after the pilot pass is reviewed; resume uses the same action.
4. `seal`: publish `supplement.jsonl.gz` and `manifest.json`, without network calls.

If stopped, keep all receipts and the ledger. On SIGTERM/SIGKILL the current
attempt is consumed; a saved atomic receipt is reused, an unsaved attempt is
recorded as interrupted. Exhausted per-ID/global budgets are never reset by moving
the same task into another run directory.

Pin the eventual supplemental SHA in a separate reviewed commit before any B1
report. At present `MINUTES_EXTRA_SHA256` remains `None`, and the old unbudgeted
minute-fetch CLI remains disabled. This runbook contains no result of a new fetch.

## Local verification, 2026-11-13

- 22 transport tests pass; 101 tests pass across the three targeted B1 modules.
  The transport tests include real localhost HTTP truncation/reconnect, persisted
  attempt/byte caps, a crash between receipt publication and ledger commit, pilot
  reuse, corruption refusal, deterministic sealing and the fixed 61/64 versus
  60/64 completeness boundary.
- Ruff and strict mypy pass for the new module and tests.
- Full `make verify` passes with the repository's disposable local PostgreSQL:
  all-files lint, lock check, Python types/tests, Go test/vet/dead-code checks,
  web tests/build and Compose validation. The first attempt stopped at the pinned
  old Ruff's stdlib handler-name rule; the compatibility annotation was fixed
  before the successful complete run.
- The classifier SHA remains
  `9b31f96127fd8223ee1f6c5199fa95a0dc948e8eea1eed78029bb3eb38e23a77`, matching
  the existing exhaustive containment proof. That unchanged 12-minute audit was
  not repeated for a transport-only addition.

No external market API, production service, blind window or strategy outcome was
used to verify this transport. The actual pilot remains unrun.
