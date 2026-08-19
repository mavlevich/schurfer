# Binance momentum WATCH v1

Status: implemented and unit-tested. Compose profile disabled by default,
same activation gates as `feat/binance-momentum-capture-v1`. PR 7 of the
30-PR roadmap, following that PR.

## What this PR is and is not

**Is**: a second WATCH worker instance running the exact same frozen
`momentum_flow_watch_v1` logic and thresholds as the live Bybit worker,
scoped to Binance's own captured bars via its own contract identity
(`BINANCE_WATCH_CONTRACT`, `watch_version="momentum_flow_watch_v1_binance"`).
Wired into a disabled-by-default `momentum-watch-binance` Compose profile,
own start/stop/health Makefile targets.

**Is not** a redesign, and not activated. Every numeric threshold on
`BINANCE_WATCH_CONTRACT` is reused byte-identical from the live
`FROZEN_WATCH_CONTRACT` -- enforced by a test that diffs every field
except the two identity fields (`watch_version`, `source_exchange`). No
Compose profile is enabled by this PR; Binance WATCH has nothing to watch
until `feat/binance-momentum-capture-v1`'s own activation gates
(corrected Bybit checkpoint + host capacity) pass and Binance bars start
landing in the shared hypertable.

## Why this was buildable without live Binance data

`momentum_flow_watch_evaluator`, `WatchStore` (the repository protocol),
and `MomentumFlowWatchRepository` were already contract-parameterized
from the start -- `evaluate_bucket` has always taken a `WatchContract` as
a parameter, and `momentum_flow_watch_states`/`_evaluations` already
carry `exchange` in their own primary key. The only place a second
venue's contract was NOT reachable was the outer orchestration layer:
`run_watch_worker` hardcoded `FROZEN_WATCH_CONTRACT` at every call site,
and `HEALTH_KEY` was a bare, unparameterized Redis key constant.

## What changed

**`run_watch_worker` gained `contract`/`contract_sha256` parameters,
defaulting to `FROZEN_WATCH_CONTRACT`/`WATCH_CONTRACT_SHA256`.** Every
existing caller (the live Bybit worker's own `main()`, any code already
calling `run_watch_worker` without these arguments) sees byte-identical
behavior -- a regression test asserts the default path threads
`FROZEN_WATCH_CONTRACT` through `acquire_worker_lock`/`register_run`/
`due_buckets`/`load_states` exactly as before this PR existed. A second
test asserts `run_watch_worker` rejects a `(contract, contract_sha256)`
pair whose hash doesn't actually match, before ever reaching the store --
`register_run`'s own equality check alone can't catch that, since on a
fresh run it only compares the caller-supplied hash against the DB row it
just wrote from that same value.

**`HEALTH_KEY` became `health_key(watch_version)`, scoped per contract.**
Before this, a second contract's worker would have published its own
health snapshot to the exact same Redis key as Bybit's, silently
overwriting it -- the same "no shared/masking counters" failure class
[momentum-canary-multivenue-v1.md](momentum-canary-multivenue-v1.md)
already fixed on the Go capture side. `watch_version` is already the
row-identity key `acquire_worker_lock`/`register_run` use, so no new
parameter was needed to scope it -- see below for why that specific
field has to differ. The `momentum_watch.bucket_completed` log event also
now carries `watch_version`, matching the adjacent `starting`/`qualified`
events in the same function -- with two workers logging to one pipeline,
an event with no venue identity is not attributable to either.

**`momentum_flow_watch_binance_worker.py` is a new, thin entrypoint
module** (`main()` + a `momentum-flow-watch-binance` script in
`pyproject.toml`). It owns nothing but selecting `BINANCE_WATCH_CONTRACT`
and calling the exact same `run_watch_worker` the Bybit entrypoint calls.
Unlike the Go capture binaries, this needed no forked duplicate at all --
the shared orchestration function already existed in a form both
entrypoints could call directly.

## Why `watch_version` is the field that has to differ

`MomentumFlowWatchRepository.acquire_worker_lock` takes a Postgres
advisory lock keyed by `hashtext(watch_version)`, and
`momentum_flow_watch_runs`'s own primary key is `watch_version` alone
(see the `0026` migration). Reusing `"momentum_flow_watch_v1"` for a
Binance contract with a different `source_exchange` would hit one of two
failure modes:

- If Binance's worker starts while Bybit's own is already running, its
  `acquire_worker_lock` call blocks on the SAME advisory lock Bybit
  already holds -- it never starts.
- If Binance's worker somehow registered its run first, Bybit's own
  `register_run` would find an existing `momentum_flow_watch_v1` row
  whose `contract_sha256`/`contract_json` don't match (a different
  `source_exchange` changes the hash) and raise a RuntimeError saying the
  stored contract does not match this binary -- Bybit's own live worker
  would refuse to start.

A distinct `watch_version` string (`"momentum_flow_watch_v1_binance"`)
gives Binance's instance its own advisory lock, its own `_runs` row, and
(via `health_key`) its own Redis key -- completely isolated, no schema
change needed anywhere: the states/evaluations tables already key by
watch_version, exchange, market_type, and symbol together.

## What this deliberately did not retune

`min_cross_section_size` (100) stays exactly as Bybit's own frozen
contract sets it, even though Binance's real strict-USDT-perpetual
universe is smaller than Bybit's (~516 crypto perpetuals). This is a
deliberate reading of "frozen v1 logic": if Binance's real quality-ready
cross-section turns out too small to ever clear 100, WATCH will simply
never fire for Binance, and real data should be the thing that reveals
that, before any threshold gets loosened to route around it. If this
does turn out to be the limiting factor, the worker will still run,
register its own `_runs` row, and publish health -- it will just never
produce a "watch" decision, which needs to be read as "cross-section too
small," not "working correctly, market's just quiet."

## What this does not touch downstream

`momentum_flow_watch_linkage_repository.py` and
`momentum_flow_episode_study_report.py` both still filter on the bare
`WATCH_VERSION` constant (Bybit's) rather than accepting a contract or
`watch_version` parameter. Once Binance's worker is actually activated
and starts writing rows under its own `watch_version`, these two research
consumers will silently exclude every Binance row from cohort/evaluation
lookups -- no error, just missing data. This PR's own scope is making
Binance WATCH itself runnable and isolated; making the research tooling
that reads WATCH output venue-aware is separate, not-yet-started
follow-up work, called out here rather than left to be discovered later.

## Operational note

Nothing in this PR touches the live Bybit WATCH worker's own runtime
behavior when called with default arguments (see the regression tests
above). Deploying it restarts that worker the same way any code change
to its own file would, subject to the same standing deploy discipline as
every other PR in this roadmap -- a separate, explicitly confirmed step,
not implied by merging to `main`.
