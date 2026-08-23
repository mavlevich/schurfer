# Engineering findings register

Status: current engineering intake and verification register.

Last reviewed: 2026-08-23.

This register records cross-cutting reliability, performance, and architecture
findings before they become implementation work. It prevents an unverified review
comment from silently becoming a production fact or an item in the delivery queue.

The register is not a second roadmap. [`ROADMAP.md`](../../ROADMAP.md) owns priority,
sequence, and promotion gates. A finding becomes scheduled work only when the roadmap
or an incident assigns it a bounded pull request. Research hypotheses belong in the
[discovery ledger](../research/discovery-ledger.md), not here.

## Status and priority rules

Statuses:

- `reported`: plausible claim that has not been reproduced or measured;
- `confirmed`: current code or production evidence demonstrates the failure mode;
- `measuring`: instrumentation or a bounded benchmark is required before choosing a
  remediation;
- `planned`: confirmed or measured work has a bounded PR in `ROADMAP.md`;
- `fixed`: the remediation and regression gate have merged and been verified;
- `rejected`: the stated failure mode does not exist; preserve the explanation so it
  is not repeatedly rediscovered.

Priorities:

- `P0`: active capital, security, or irreversible-data incident;
- `P1`: required before unattended real-money trading;
- `P2`: material paper/operations/UX reliability work;
- `P3`: optimization or architecture experiment that must not displace profit and
  evidence work without measurements.

No performance multiplier, exchange-ban prediction, latency claim, or capacity claim
is accepted without a reproducible benchmark or production metric. Prefer the
smallest fix at the owning boundary and add a regression test for the exact failure.

## Active and reviewed findings

### ENG-001 — Supervise long-running execution workers

- **Status / priority:** `confirmed`, `P1`.
- **Evidence:** `apps/execution/schurfer_execution/main.py` starts several independent
  workers with `asyncio.create_task`. Holding task references prevents garbage
  collection, but it does not make an unexpectedly completed task fail the service or
  restart the worker. Several worker loops catch ordinary exceptions themselves, so
  supervision must distinguish a recovered iteration failure from termination of the
  whole worker.
- **Failure mode:** one strategy, monitor, or accounting worker can stop while the API
  process and container remain healthy.
- **Bounded remediation:** introduce one service-owned supervisor with named tasks,
  explicit expected-cancellation semantics, termination logging, and a documented
  policy per worker: fail the container, bounded restart, or intentionally stop. Do
  not wrap every task in unrelated ad-hoc `try/except` blocks. `TaskGroup` is an
  implementation option, not the acceptance criterion: naïvely adopting it can
  cancel healthy workers when one optional worker exits.
- **Regression/operational gates:** tests for unexpected return, raised exception,
  cancellation during shutdown, restart exhaustion, and health becoming non-OK;
  Docker restart behavior must be exercised in a bounded smoke test. A critical
  worker must never disappear while `/health` remains green.
- **Scheduling:** required before unattended micro-live. Reuse the generic heartbeat
  and worker-health primitives introduced by the early-momentum input-quality work;
  do not create a second health framework.

### ENG-002 — Reconcile unknown live exchange positions at startup

- **Status / priority:** `reported` residual gap, `P1`; the broader claim that live
  reconciliation does not exist is `rejected`.
- **Evidence:** `apps/execution/schurfer_execution/monitor.py` already fetches live
  exchange positions, reconciles vanished tracked positions from exchange-native stop
  fills, persists unresolved incidents, and retries pending journal closes. Paper also
  repairs missing Redis state in `paper.py`.
- **Residual question:** verify what happens when an exchange position exists but
  neither the journal nor Redis contains a corresponding tracked position—for
  example, after an entry fill followed by a persistence failure or after a manual
  order.
- **Bounded remediation:** startup and periodic three-way comparison of exchange,
  journal, and Redis state. Classify unknown positions and quarantine/alert them;
  never silently adopt, close, or invent an entry price. Repair only when identity and
  durable evidence are sufficient.
- **Regression/operational gates:** integration scenarios for exchange-only,
  DB-only, Redis-only, partial-fill, manual-position, and transient exchange failure;
  idempotent incident creation and recovery notification; micro-live remains blocked
  until the result is fail-closed.
- **Scheduling:** part of the existing real-money execution checklist and live-risk
  reconciliation lane, not a newly discovered replacement subsystem.

### ENG-003 — Replace per-position paper quote polling with a quote snapshot boundary

- **Status / priority:** `confirmed`, `P2` now and `P1` before materially increasing
  concurrent positions.
- **Evidence:** `apps/execution/schurfer_execution/paper.py` scans open Redis positions
  and awaits an individual `fetch_ticker(symbol)` for each position. Latency and REST
  request count therefore grow with the number of open positions.
- **Failure mode:** slow paper exits, rate-limit pressure, and one slow symbol delaying
  every later position. Calling this a guaranteed exchange DDoS or ban is not
  supported by current evidence.
- **Bounded remediation:** one reusable, exchange-scoped quote provider. Use
  `fetch_tickers(symbols)` only when the CCXT adapter advertises correct support and
  cost; otherwise use a bounded-concurrency, rate-limited fallback. Preserve partial
  failures per symbol and a quote timestamp/staleness contract. Do not issue an
  unrestricted all-markets request on an exchange where it has a high rate-limit
  weight.
- **Regression/operational gates:** adapter capability tests; bounded request count;
  one-symbol failure does not block other exits; stale/missing quotes fail closed;
  deterministic ordering and timeout coverage; metrics for batch size, latency,
  fallbacks, 429s, and quote age.

### ENG-004 — Harden Telegram HTTP delivery and finish gateway migration

- **Status / priority:** `confirmed`, `P2`; durable consolidation is already
  `planned` in `ROADMAP.md`.
- **Evidence:** `apps/notifier/internal/notifier/telegram.go` constructs a client in
  `postMessage`, closes but does not consume the response body, reports only the HTTP
  status, and does not honor Telegram `429 Retry-After`.
- **Failure mode:** impaired keep-alive reuse, lost diagnostic/rate-limit information,
  and delivery failures without a bounded retry. The current code closes the body, so
  the stronger claim of an immediate file-descriptor leak is not established.
- **Bounded remediation:** one long-lived bounded HTTP client in the notifier, bounded
  body consumption, Telegram response decoding, Retry-After-aware retry with jitter,
  and delivery state through the existing notification outbox/audit contract. Do not
  independently improve every legacy direct sender instead of completing the gateway
  migration.
- **Regression/operational gates:** `httptest` coverage for success, non-JSON error,
  429 with valid/invalid Retry-After, retry exhaustion, timeout, body-size bound,
  delivery deduplication, and DLQ/health counters.
- **Related authority:**
  [`docs/contracts/notification-delivery-v1.md`](../contracts/notification-delivery-v1.md).

### ENG-005 — Centralize expired-session handling in the web client

- **Status / priority:** `confirmed`, `P2`.
- **Evidence:** `apps/web/src/hooks/useTradesData.ts` turns non-2xx responses into
  generic query errors, while `apps/web/src/contexts/AuthContext.tsx` changes the
  authenticated state only during its own health/login/logout flows. A later API 401
  does not transition the application back to the login state.
- **Failure mode:** an expired session leaves protected pages mounted while queries
  repeatedly fail.
- **Bounded remediation:** a shared API request boundary emits one session-expired
  transition, clears protected query data, and redirects to login. Avoid calling the
  logout endpoint once per concurrently failing query or introducing recursive 401
  handling.
- **Regression gates:** one 401 changes auth state once; concurrent 401 responses do
  not produce a logout storm; ordinary 403/429/500 responses retain their distinct
  UI behavior; cached account/trade data is not visible after logout.
- **Related authority:**
  [`docs/architecture/web-ui-evolution-v1.md`](../architecture/web-ui-evolution-v1.md).

### ENG-006 — Measure PostgreSQL connection lifecycle before pooling

- **Status / priority:** `measuring`, `P2`.
- **Evidence:** long-running Python services and analytics commands contain direct
  `psycopg.AsyncConnection.connect()` call sites. The review did not establish their
  connection rate, peak concurrency, handshake cost, `max_connections` headroom, or
  a production `too many clients` incident.
- **Decision rule:** do not mechanically replace every call with a global pool.
  Long-lived services may benefit from a service-owned bounded pool; short-lived CLI
  reports may be clearer and safer with one direct connection. Pools multiply across
  containers and can increase idle connection count if sized without a budget.
- **Measurement gate:** inventory call sites by lifetime; record connection opens,
  checkout wait, query latency, peak `pg_stat_activity`, failure count, server
  `max_connections`, and deploy/restart behavior under representative load.
- **Possible remediation after evidence:** bounded `psycopg_pool` lifecycle owned by
  each long-running service, small explicit min/max size, checkout timeout, health
  check, statement/transaction timeouts, rollback hygiene, and graceful close.
- **Regression gates:** concurrency/load test, connection-loss recovery, pool
  exhaustion, leaked transaction prevention, and clean application shutdown.

### ENG-007 — Benchmark alternative JSON parsers; do not adopt by assertion

- **Status / priority:** `measuring`, `P3`.
- **Evidence:** the Go collector and market-hotset use `encoding/json`; Python uses its
  standard JSON path in places. Existing production observations have not shown JSON
  parsing to be the limiting resource, and the claimed 2–4x/3x end-to-end gains were
  not measured on Schurfer payloads.
- **Decision rule:** third-party parser or generated-code adoption requires CPU and
  allocation profiles proving JSON is a material hot path. Parser speed alone is not
  an application throughput result.
- **Benchmark gate:** representative recorded payload corpus for every venue, Go
  benchmarks with `-benchmem`, Python microbenchmarks only for measured hot paths,
  whole-worker CPU/RSS/GC comparison, malformed/fuzz payloads, numeric precision,
  duplicate-key/case behavior, and wire-compatibility tests.
- **Scheduling:** optimization backlog only. Reject changes justified solely by
  library marketing benchmarks.

### ENG-008 — Benchmark `uvloop`/`orjson` instead of enabling them globally

- **Status / priority:** `measuring`, `P3`.
- **Evidence:** no repository benchmark currently demonstrates that event-loop or JSON
  overhead, rather than database/exchange/network latency, constrains execution or
  analytics. `schurfer-execution` already depends on `uvicorn[standard]`, starts
  Uvicorn with its default `loop="auto"`, and therefore selects the installed
  `uvloop` on supported Unix/CPython deployments. Adding two explicit setup lines to
  that executable would not introduce the proposed optimization. Analytics CLIs use
  `asyncio.run`; they are often CPU/SQL bound and do not automatically benefit from
  another event loop. `orjson` is already present transitively through CCXT, but that
  does not mean Schurfer's Redis/persistence/wire contracts use it or can change
  serializer semantics safely.
- **Decision rule:** enable per executable only after representative end-to-end
  evidence, platform compatibility, shutdown/cancellation tests, and a fallback path.
  Never change serialized persistence hashes or numeric semantics accidentally.
- **Scheduling:** no roadmap implementation PR until a benchmark crosses a declared
  materiality threshold.

### ENG-009 — `waitOutPause` is not a busy-wait spinlock

- **Status / priority:** `rejected`, no implementation priority.
- **Evidence:** `apps/collector/internal/binance/openinterest.go` computes the remaining
  pause and waits in a `select` on `time.After(remaining)` or `ctx.Done()`. The
  goroutine sleeps and yields the processor. Its atomic compare-and-swap loop only
  resolves concurrent pause-extension updates.
- **Optional follow-up:** a reusable timer could reduce allocations if a profile ever
  shows timer churn, but that is a micro-optimization, not a CPU-burning production
  defect.
- **Regression rule:** preserve the rejection so the same false finding does not
  trigger a channels/`sync.Cond` rewrite later.

### ENG-010 — Measure venue latency before changing production region

- **Status / priority:** `reported`, `P3`.
- **Evidence:** no measurement supports a Tokyo/Vultr/AWS `<2 ms` round-trip claim.
  Production currently runs on Hetzner; historical hosting rationale is preserved in
  superseded [ADR-0008](../adr/0008-aws-frankfurt-hosting.md). Strategy holds, data
  locality, database/notification traffic, exchange access policy, cost, and
  operational blast radius matter in addition to ping.
- **Decision gate:** benchmark DNS, WebSocket event lag, authenticated REST/order
  round-trip, reconnect behavior, jitter, packet loss, and exchange-specific endpoints
  from bounded candidate regions using no real orders. Decide which hot path, if any,
  benefits economically.
- **Remediation rule:** a region move or split hot-path node requires a new accepted
  ADR that supersedes the current deployment fact. Never migrate because a cloud
  region is assumed to colocate with an exchange matching engine.

### ENG-011 — Select execution algorithms by urgency and measured capacity

- **Status / priority:** `reported` design improvement, `P2` before scaling position
  sizes; not a blocker for tiny bounded micro-live probes with strict liquidity
  controls.
- **Evidence:** Schurfer already records/uses executable VWAP evidence in paper paths.
  A blanket ban on market orders has not been justified. TWAP/VWAP can reduce impact
  for large patient orders but can add adverse selection, signaling, missed fills,
  and strategy delay for urgent entries.
- **Decision gate:** replay or shadow comparison of market/IOC, limit-with-timeout,
  and sliced execution using the same decision, observed book, latency, fill ratio,
  implementation shortfall, opportunity loss, and size buckets.
- **Bounded remediation:** an execution-policy interface chooses an algorithm by
  venue capability, notional/depth ratio, urgency, spread, and impact budget. Every
  order retains idempotency, a deadline, cancellation/reconciliation, and exact fill
  accounting.

### ENG-012 — Extend BBO/order-flow capture without claiming spoof detection

- **Status / priority:** `planned` in the research/data lane, `P2` only when the
  non-recoverable fields serve a registered experiment.
- **Evidence:** Binance `bookTicker` capture and order-flow research already exist;
  see [`docs/research/binance-bookticker-capture-v1.md`](../research/binance-bookticker-capture-v1.md).
  BBO alone provides top-of-book prices and, if retained, sizes. It does not expose
  the order lifecycle or trader intent required to label spoofing.
- **Bounded next data:** retain bid/ask size and sequence/freshness diagnostics where
  supported; add L2 snapshots/deltas only under a bounded symbol/venue pilot with gap
  detection, sequence recovery, storage budget, and compression gates.
- **Research rule:** call derived outputs order-flow imbalance or liquidity/book
  anomalies. A spoofing study requires L2 add/cancel/replace behavior and still must
  avoid asserting intent as ground truth.
- **Regression/operational gates:** sequence-gap and reconnect fixtures, venue schema
  conformance, point-in-time timestamps, queue/drop metrics, storage/day limit, and a
  frozen prospective hypothesis before strategy promotion.

### ENG-013 — Layer portfolio and per-strategy circuit breakers

- **Status / priority:** `planned`, `P1` before unattended micro-live.
- **Evidence:** the roadmap already requires durable daily loss/trade limits, exchange
  native stops, idempotent orders, startup reconciliation, and heartbeat alerts before
  live money. A single global hourly drawdown switch does not cover strategy-local
  failures and can unnecessarily stop healthy, operationally independent strategies.
- **Bounded remediation:** immutable per-trade risk, per-strategy limits, portfolio
  exposure/drawdown limits, data/health breakers, and a global kill switch. Define
  fail-closed behavior, persistence across restart, manual reset authorization, and
  recovery criteria. Correlated strategies share an exposure budget instead of each
  receiving a full independent allocation.
- **Regression/operational gates:** concurrent-order races, restart persistence,
  stale market data, accounting uncertainty, exchange outage, breach-before-order,
  breach-after-fill, recovery/reset audit, and a production drill with no real order.

### ENG-014 — Choose analytics engines per workload, not through a blanket rewrite

- **Status / priority:** `measuring`, `P3`.
- **Evidence:** `apps/analytics` already has DuckDB as a direct dependency and uses it
  to create/read verified Parquet datasets and in token-behavior reporting. No Pandas
  or Polars import was found in the active analytics code. The proposal to "replace
  Pandas with Polars and DuckDB" is therefore outdated as a repository-wide task.
  Slow reports observed so far can include remote PostgreSQL scanning, SQL window
  functions, transfer through the SSH tunnel, Python inference/bootstrap work, or
  output generation; changing a dataframe library cannot fix every component.
- **Decision rule:** retain SQL/Timescale for selective server-side work, DuckDB for
  local Parquet/columnar queries, and ordinary typed Python structures for small
  bounded results. Consider Polars only for a measured, material in-memory dataframe
  stage that DuckDB/SQL cannot execute clearly or efficiently.
- **Measurement gate:** profile one representative slow report phase by phase: DB
  `EXPLAIN (ANALYZE, BUFFERS)`, rows and bytes transferred, query time, local compute,
  peak RSS, output time, and repeated-run variance. Compare the current implementation
  with a DuckDB pushdown and a Polars implementation only for the identified hot
  stage, on the same immutable input artifact.
- **Regression gates:** identical cohort membership and ordering, null/decimal/timezone
  semantics, deterministic seed/results, Parquet schema and content-hash stability,
  bounded memory, and golden report output. A speedup that changes the research
  denominator or numerical contract is invalid.
- **Promotion threshold:** at least 30% end-to-end wall-time reduction or at least 40%
  peak-RSS reduction for a report that currently blocks iteration, without weakening
  reproducibility. A library-only microbenchmark is insufficient.

### ENG-015 — Benchmark and version any binary NATS market-data contract

- **Status / priority:** `reported`, `P3`; promote only if bus serialization or
  bandwidth becomes a measured capture constraint.
- **Evidence:** the active collector publishes JSON NATS payloads and hotset/momentum
  consumers decode JSON. `msgpack` is constrained in the Python workspace, but that
  is not a cross-language wire contract. Full order-book deltas are not currently
  routed from Go to Python through NATS, so the claim that JSON will saturate the
  network is prospective. No Schurfer measurement supports the proposed 3x size or 5x
  parse improvement.
- **Architecture question:** first decide whether Python needs every L2 delta at all.
  A Go venue adapter can validate sequence, maintain the book, and publish smaller
  versioned features/snapshots while raw bounded capture is persisted separately.
  This can be safer and cheaper than optimizing a firehose that should not cross the
  service boundary.
- **Measurement gate:** record messages/s, payload bytes/s, NATS pending bytes,
  slow-consumer/dropped events, publisher/consumer CPU, allocations, end-to-end lag,
  and compression effects during a representative high-volume replay. Compare current
  JSON with Protobuf and MessagePack on the same versioned corpus. Include schema
  evolution, unknown fields, malformed messages, decimals/timestamps, cross-language
  Go/Python conformance, and generated-code/tooling cost.
- **Bounded remediation if promoted:** define a versioned envelope with content type,
  schema version, event/receive timestamps, venue identity, and compatibility policy;
  dual-publish to a new subject, shadow-compare decoded events and hashes, migrate
  consumers independently, then retire the old subject. Never reinterpret an existing
  JSON subject in place.
- **Promotion threshold:** serialization or NATS transport must be a measured top-three
  hot-path cost or violate a declared queue/lag/drop/capacity gate, and the candidate
  must deliver at least 25% whole-pipeline CPU or bandwidth headroom with identical
  semantics and no additional loss.

### ENG-016 — Add Timescale continuous aggregates only for proven repeated queries

- **Status / priority:** `reported`, `P3`.
- **Evidence:** `timeseries.bybit_momentum_bars_1m` is already a one-minute aggregate
  hypertable, not a raw-tick table. Its migration configures one-day chunks,
  compression, and retention. No continuous aggregate migration was found. The
  roadmap intentionally derives larger views from canonical 1m bars at query time or
  allows a derived continuous aggregate later. No measurement supports a universal
  2ms response claim.
- **Trade-off:** a continuous aggregate can accelerate repeated 5m/15m/1h chart or
  research queries, but adds refresh/invalidation jobs, write amplification, storage,
  late-event semantics, operational health, and migration/backfill complexity. It
  cannot safely aggregate all columns with generic `AVG`/`SUM`: price needs first/open,
  max/high, min/low, last/close; volumes and counts sum; open interest/BBO generally
  use point-in-time last values; completeness/gap flags require explicit conservative
  rules.
- **Measurement gate:** use `pg_stat_statements` and representative API/report traces
  to identify repeated slow bucket queries; capture p50/p95/p99 latency, rows scanned,
  buffers/temp IO, CPU, concurrent load, and query frequency. Compare the canonical
  query with a correctly indexed query, a regular/materialized derived table, and a
  continuous aggregate. Include compressed chunks, the 35-day retention boundary,
  late/out-of-order corrections, and refresh lag.
- **Bounded remediation if promoted:** begin with one view and one consumer, partitioned
  by exchange/market type/symbol/capture version with an explicit bucket timezone and
  quality contract. Expose aggregate freshness/refresh failures, keep 1m bars as the
  authority, and rebuild rather than hand-edit derived values.
- **Regression/operational gates:** golden rollups across bucket boundaries, missing
  minutes, late revisions, compression and retention, refresh-policy failure/recovery,
  migration downgrade, storage/day, ingest overhead, and query-plan assertions.
- **Promotion threshold:** the target query is frequent and violates its product or
  report SLA, and the aggregate provides at least 5x p95 query improvement while
  keeping ingest CPU/latency and storage growth inside declared canary gates. Network
  and API overhead remain part of the user-visible SLA.

## Promotion summary

The reviewed findings currently imply this bounded support sequence. It does not
replace the profit/evidence lane or expand the active early-momentum PR:

1. execution worker supervision and health (`ENG-001`);
2. unknown live-position startup reconciliation (`ENG-002`);
3. quote snapshot/batching boundary (`ENG-003`);
4. complete notifier delivery hardening through the existing gateway plan
   (`ENG-004`);
5. centralized expired-session behavior (`ENG-005`);
6. measure connection lifecycle before deciding pool scope (`ENG-006`).

`ENG-001`, `ENG-002`, and `ENG-013` are capital-safety gates. `ENG-003` becomes a
capital-safety gate when concurrency grows beyond the bounded current paper cohort.
The parser/event-loop/region ideas remain experiments unless measurements promote
them. The rejected `waitOutPause` claim must not consume an implementation PR.

## Deferred performance verification queue

These measurements are deliberately retained even though they are not scheduled
implementation work. "Later" means when the stated trigger occurs, not an arbitrary
calendar date. A result is recorded here whether it supports or rejects the proposed
optimization.

| Measurement                                         | Related finding | Revisit trigger                                                                                                                                                            | Required comparison                                                                                                                                                                                 | Promotion threshold                                                                                                                                                |
| --------------------------------------------------- | --------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| PostgreSQL connection lifecycle and bounded pooling | `ENG-006`       | Before adding another connection-heavy long-running worker, or if connection errors/checkout latency/peak active sessions consume 50% of the configured connection budget  | Current direct lifecycle versus a small service-owned pool under representative concurrency, including restart and connection-loss recovery                                                         | Pooling materially lowers connection churn or p95 operation latency without leaks, excessive idle sessions, or shutdown failures                                   |
| Paper quote acquisition scaling                     | `ENG-003`       | Before raising the concurrent-position limit, adding another active paper strategy to the same worker, or after any observed 429/quote-age breach                          | Sequential per-symbol calls versus capability-aware batch and bounded-concurrency fallback on every enabled execution venue                                                                         | The selected provider remains within exchange weight limits and the declared quote-age/exit-tick SLA at the target position count                                  |
| Go JSON parser CPU/allocations                      | `ENG-007`       | Sustained collector CPU p95 above 60%, GC/allocations becoming a measured top-three cost, or host capacity blocking non-recoverable capture                                | `encoding/json` versus candidate parser on a versioned corpus of real venue payload shapes, plus whole-worker CPU/RSS/GC and fuzz/conformance tests                                                 | At least 20% whole-worker CPU or capacity improvement with identical accepted/rejected payload semantics and no material memory regression                         |
| Python event loop and JSON parser                   | `ENG-008`       | A profile attributes at least 20% of a long-running executable's wall time or CPU to event-loop scheduling/JSON encoding rather than network, SQL, or strategy computation | Standard loop/JSON versus `uvloop`/`orjson` per executable, including cancellation, shutdown, serialization hashes, decimals, datetimes, and error cases                                            | At least 15% end-to-end throughput or p95 latency improvement on the owning executable with contract-compatible output and a safe fallback                         |
| Binance OI pause timer allocation                   | `ENG-009`       | Only if a Go allocation/CPU profile identifies timer churn in `waitOutPause` as material                                                                                   | Current `time.After` behavior versus a correctly stopped/drained reusable timer under concurrent pause extensions and cancellation                                                                  | Measurable whole-worker benefit; otherwise retain the simpler correct implementation                                                                               |
| Execution/collector regional latency                | `ENG-010`       | A registered strategy demonstrates that current decision-to-order latency materially reduces fill probability or net EV                                                    | At least two candidate regions versus current production for WebSocket lag, authenticated REST/order-test latency, jitter, reconnects, endpoint reachability, cost, and operational failure domains | Prospective economic benefit exceeds migration and split-system cost; an accepted replacement ADR is mandatory before infrastructure change                        |
| Market/IOC versus limit/sliced execution            | `ENG-011`       | Before increasing live notional beyond the size that clears the existing impact/capacity gate, or when implementation shortfall breaches its budget                        | Same prospective decisions evaluated with market/IOC, limit-with-timeout, and bounded slicing; compare fill rate, delay, impact, opportunity loss, fees, and net EV                                 | A policy wins after costs on an untouched sample and remains safe under partial fill, cancel, timeout, and restart reconciliation                                  |
| BBO size/L2 processing and storage capacity         | `ENG-012`       | A frozen OFI/liquidity-anomaly hypothesis requires fields not already retained, before widening beyond a bounded symbol pilot                                              | Current BBO path versus size-retaining BBO and, only if needed, sequenced L2 deltas; measure gaps, drops, CPU, RSS, raw/compressed bytes/day, and replay fidelity                                   | Capture is gap-detectable and replayable, stays within declared host/storage gates, and supplies a registered experiment rather than speculative data accumulation |
| Analytics SQL/DuckDB/Polars engine selection        | `ENG-014`       | A phase profile shows a local transformation or memory stage materially blocks a canonical report                                                                          | Current implementation versus SQL/DuckDB pushdown and Polars only for the measured hot stage, on one immutable input                                                                                | At least 30% whole-report wall-time or 40% peak-RSS improvement with identical cohort and numerical contract                                                       |
| JSON versus binary NATS contract                    | `ENG-015`       | Serialization/transport is a top-three profile cost or a replay violates pending-byte, slow-consumer, drop, or lag gates                                                   | Current JSON versus versioned Protobuf and MessagePack on identical cross-language payloads and a whole-pipeline replay                                                                             | At least 25% pipeline CPU or bandwidth headroom with semantic parity, safe schema evolution, and no additional loss                                                |
| Timescale continuous aggregate                      | `ENG-016`       | A frequent 5m/15m/1h query violates its declared API/report SLA after query/index review                                                                                   | Canonical query/index versus derived table/materialized view/continuous aggregate, including refresh and ingest cost                                                                                | At least 5x p95 query improvement with correct late-data/quality semantics and storage/ingest inside canary gates                                                  |

Performance results must include the command, code revision, input/corpus identity,
duration, host/container limits, warm-up policy, repetitions, and raw artifact path.
Report p50/p95/p99 and error/drop counts where applicable; never promote from a
single best run. Synthetic microbenchmarks may locate a bottleneck but cannot alone
justify a production dependency or infrastructure migration.

## Source review snapshot — 2026-08-23

The following externally proposed items are all preserved in this register so the
original review can be retired without losing a claim:

- replace direct Python PostgreSQL connections with bounded pooling (`ENG-006`);
- supervise execution background workers (`ENG-001`);
- batch or bound paper-position quote acquisition (`ENG-003`);
- harden Telegram response handling and 429 retry (`ENG-004`);
- centralize frontend 401/session-expiry handling (`ENG-005`);
- benchmark Go JSON alternatives (`ENG-007`);
- benchmark Python `uvloop`/`orjson` (`ENG-008`);
- preserve the rejection of the alleged OI busy-wait (`ENG-009`);
- verify the remaining unknown-live-position reconciliation case while preserving
  existing live reconciliation (`ENG-002`);
- benchmark region placement rather than assuming Tokyo proximity (`ENG-010`);
- study market/IOC, limit, TWAP/VWAP, and sliced execution as policies rather than
  banning market orders globally (`ENG-011`);
- extend existing BBO/order-flow work only for a frozen experiment and do not infer
  spoofing intent from BBO (`ENG-012`);
- implement layered per-trade, per-strategy, portfolio, data-health, and global
  circuit breakers before unattended micro-live (`ENG-013`).
- preserve DuckDB's existing role and evaluate Polars only for a profiled analytics
  bottleneck rather than a nonexistent Pandas migration (`ENG-014`);
- benchmark a versioned Protobuf/MessagePack NATS contract only if JSON transport is a
  measured constraint, and first question whether full L2 belongs on that boundary
  (`ENG-015`);
- evaluate a correctly defined Timescale continuous aggregate only for a repeated
  query that misses its SLA; canonical one-minute bars remain authoritative
  (`ENG-016`).

## Review procedure

For every new external or internal code-review claim:

1. record the claim as `reported` with the exact code path and alleged failure mode;
2. reproduce it or identify the current behavior and existing safeguards;
3. classify overlap with roadmap, ADRs, contracts, incidents, and research;
4. define the smallest metric or test that can falsify the claim;
5. promote only confirmed or measured work into a bounded roadmap PR;
6. after merge, link the PR and tests, verify production when applicable, and mark it
   `fixed`; never erase rejected findings or their rationale.
