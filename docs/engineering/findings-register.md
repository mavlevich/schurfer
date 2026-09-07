# Engineering findings register

Status: current engineering intake and verification register.

Last intake review: 2026-09-07, code revision `657c411`. September findings and
selected stale entries were reconciled; this is not a fresh verification of every
historical claim or any production deployment.

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

- **Status / priority:** missing-supervisor claim `rejected` as stale on 2026-09-07;
  residual operational verification `reported`, `P1` before unattended live.
- **Evidence:** `apps/execution/schurfer_execution/main.py` constructs and starts
  `WorkerSupervisor`, exposes its readiness gate, and stops/waits for it at shutdown.
  `supervisor.py` defines per-worker restart policies and intentional-stop states;
  `apps/execution/tests/test_supervisor.py` already exists.
- **Residual gate:** verify the deployed critical-worker inventory, restart
  exhaustion, stale-worker health and container shutdown/restart behavior. The
  documentation intake did not run a production drill or re-run these tests.
- **Scheduling:** validate or repair a concrete remaining failure. Do not implement
  a second supervisor because the old register described it as absent.

### ENG-002 — Reconcile unknown live exchange positions at startup

- **Status / priority:** `reported` residual gap, `P1`; the broader claim that live
  reconciliation does not exist is `rejected`.
- **Evidence:** `apps/execution/schurfer_execution/monitor.py` already fetches live
  exchange positions, reconciles vanished tracked positions from exchange-native stop
  fills, persists unresolved incidents, and retries pending journal closes. Paper also
  repairs missing Redis state in `paper.py`. September amendment:
  `reconciliation_worker.py`, `reconciliation.py` and `order_attempts.py` also exist,
  are wired into startup under a readiness blocker, and have dedicated tests.
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
  reconciliation lane, not a newly discovered replacement subsystem. Use the existing
  worker for ENG-022 residual fill/protection/restart fixes; do not rebuild it.

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
- **2026-09-06 measurement:** the real `_tick` with fake Redis/exchange and 10 ms
  artificial latency took 0.0109/0.1198/0.5950/1.1924 seconds at 1/10/50/100
  positions. This confirms sequential scaling, not production exchange latency.
  See the [performance snapshot](audits/2026-09-06/schurfer-performance-audit-2026-09-06.md).
  Momentum repository starvation is a separate selection defect, ENG-023.

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
- **2026-09-07 clarification:** several analytics repositories already use bounded
  SQLAlchemy pools. The momentum-paper repository has two connections, one held for
  its advisory lock. The claim that Python has no pooling anywhere is rejected;
  adding more idle pools is not a substitute for a service-wide connection budget.

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

### ENG-017 — Separate MEXC contract creation from trading-open time

- **Status / priority:** `confirmed`, `P1` before listing-age, cross-venue identity,
  or listing-model evidence uses MEXC instruments.
- **Evidence:** on 2026-08-25 the official MEXC `contract/detail` response for
  `CATE_USDT` reported `createTime=1726410428000` (2024-09-15) and
  `openingTime=1785132000000` (2026-07-27 06:00 UTC). The official futures listing
  announcement and the first public kline agree with `openingTime`. The broad scanner
  currently maps MEXC `createTime` to `onboarded_at`, so the new futures listing is
  represented as an established 2024 instrument.
- **Impact:** listing age, relisting/reuse detection, derivative identity versioning,
  cross-venue matching, control selection, and ML features can be wrong even though
  the ticker and prices are valid.
- **Bounded remediation:** retain native creation and trading-open timestamps as
  separate versioned lifecycle fields; prefer verified `openingTime` for MEXC trading
  availability; preserve the original raw value and do not rewrite historical
  identity rows in place. Publish a new catalog/classifier version and audit changed
  identities before consumers adopt it.
- **Regression gates:** a static CATE contract-detail fixture, timestamp-unit tests,
  create-versus-open divergence, absent/invalid `openingTime`, symbol reuse/relisting,
  immutable prior snapshots, and a real PostgreSQL catalog-version integration test.

### ENG-018 — Apply asset class and listing-baseline semantics to the broad scanner

- **Status / priority:** `confirmed`, `P1` before LBank-first alerts become a crypto
  strategy or ML training universe.
- **Evidence:** the Bybit and Binance momentum collectors already classify and
  fail-closed on tokenized equities, commodities, indices, dated futures, and unknown
  classes. The broad multi-exchange scanner admits any active ticker ending in
  `/USDT:USDT`; its durable instrument metadata records `market_type=swap` but no
  normalized asset class. LBank 24H stock futures such as DJT, LYTE, and PURR therefore
  appeared as extreme crypto pumps when their fresh 24-hour baseline initialized.
- **Impact:** heterogeneous instruments and listing-baseline resets contaminate pump
  cohorts, Telegram interpretation, thresholds, cross-venue matching, and prospective
  model labels.
- **Bounded remediation:** reuse one versioned classification contract across real
  consumers, with `crypto`, `tokenized_equity`, `commodity`, `index`,
  `leveraged_product`, and `unknown`; persist raw evidence and classifier version.
  Unknown/non-crypto instruments remain observable in separate research universes but
  fail closed for crypto strategies. Model listing/open age separately from price
  change instead of discarding all new instruments.
- **Regression gates:** LBank stock-future and crypto fixtures, unknown classification,
  a listing-baseline reset, same ticker across asset classes, notifier categorization,
  and repository integration coverage proving class/version survive persistence.

### ENG-019 — Restore exact market paths for LBank-first evidence

- **Status / priority:** `confirmed`, `P1` for an LBank-first trading thesis; not a
  blocker for existing Bybit/Binance strategies.
- **Evidence:** the 2026-08-25 production audit found 1,251 sole-LBank pump events
  across 83 assets. Of these, 1,224/850/114 were mature for 1d/7d/28d respectively,
  while exact complete outcome counts at all three horizons were zero. LBank current
  perpetual tickers are available, but no supported native historical perpetual OHLCV
  path exists in the current resolver; event snapshots end when the pump episode stops
  and are missing-not-at-random.
- **Impact:** the system can alert on LBank-first events but cannot determine their
  unbiased continuation/reversal economics, train a trustworthy model, or promote a
  strategy from native execution evidence.
- **Bounded remediation:** define one canonical market-path envelope and an immutable
  coverage artifact; classify every path as exact native, same-asset proxy,
  third-party, or unrecoverable. Backfill official venue paths where supported and
  begin forward LBank perpetual capture now. Keep proxy cohorts separate from exact
  native evidence.
- **Decision gate:** one frozen all-event report must compare long and short outcomes
  at registered entry delays and 15m/1h/4h/1d/7d/28d horizons with MFE/MAE, gaps,
  costs, liquidity, asset/time concentration, listing status, and unresolved rows.
  Only a pre-registered surviving segment may start prospective shadow capture.

## September audit intake

Source IDs below refer to the [archived reconciliation](audits/2026-09-06/schurfer-audit-reconciliation-2026-09-06.md).
`planned` means selected in the roadmap, not implemented, tested or deployed.
Before each fix, pin the current revision and reproduce its concrete failure.
Production claims not independently verified remain reported; an audit severity
label alone never establishes a P0 incident.

### ENG-020 — Enforce entry mode, TESTNET and stop admission consistently

- **Status / priority:** first two bounded steps `fixed in code`, deployment
  verification pending; durable stop-state `declared, not implemented`; `P1`; source
  C-1/C-6/H-4/H-5, package B01. Merged as #343 at `356bbb7` with regression tests for
  both reproduced defects. Executed there: full `apps/execution` pytest suite, ruff,
  mypy, and the all-files CI gate (lint, Go, Python, TypeScript, dead code, security).
  Not executed: any production action, so nothing here is verified in production. A
  deployment must confirm the deployed revision, restart count, health, `GET /risk`
  reporting its new reason field, and `POST /order` returning 409 under the current
  ceiling. Note that `TESTNET=true` with MEXC or KUCOIN credentials now fails startup
  by design; verify `.env.prod` before deploying.
- **Evidence:** `routers/orders.py:35` selects authenticated trading clients without
  checking the global dry-run ceiling; `exchanges.py:148` logs sandbox failure and
  retains the client. `orders.py:218` reads stop before slow preflight, while
  `routers/control.py` only changes the Redis flag. The audit reproduced these paths
  with fake exchange/DB/Redis effects. `risk.check_trading_enabled(None)` allows,
  but the current orders caller substitutes `0`: deletion alone does not enable it.
- **Bounded sequence:** first reject prohibited manual entries and fail trading
  startup on unsupported TESTNET; then reconcile stop with final admission and make
  the helper fail closed. Declare authoritative stop-state and recovery semantics
  before adding a persistent store; preserve allowed protective close operations.
- **Acceptance:** endpoint-to-exchange tests for paper/disabled mode, failed sandbox,
  stop during preflight, missing state and protective reduce-only exit; unknown
  already-submitted requests reconcile rather than being assumed cancelled.
- **Declared stop-state semantics** (required by the bounded sequence before any
  persistent store is added; declaration only, not implemented):
  - _Authority._ The latest durable stop/resume event in PostgreSQL is authoritative.
    Redis `trading:enabled` is a hot-path cache of that decision, never the record of
    it. This follows the accounting rule already stated in ARCHITECTURE.md.
  - _Write order._ `POST /stop` persists the event (state, actor, reason, event time)
    and only then writes Redis and acknowledges. A failed durable write is not an
    acknowledged stop and must be reported as such.
  - _Startup and recovery._ Execution reads the last durable event and reconciles
    Redis to it before admitting any entry; admission stays closed until that
    reconciliation succeeds. PostgreSQL is already mandatory when `AUTO_TRADE=true`,
    so this adds no new runtime dependency to the live path.
  - _Absence._ A missing Redis key means unknown, which admits nothing (implemented)
    and triggers a re-read of the durable state rather than leaving trading silently
    off until an operator notices.
  - _Resume._ Only the resume handler may write the enabled value, and only after its
    own durable write. No startup path, migration or repair script may infer resume.
  - _Idempotency._ Events carry a monotonic id; re-applying the last event is a no-op,
    so retries, double writes and replayed reconciliation are harmless.
  - _Crash window._ Production Redis runs `noeviction` with AOF `everysec` and a named
    volume, so eviction is not a failure mode, but up to about one second of
    acknowledged writes can be lost on a host crash and a restored volume can carry an
    older value. Losing the enabled value fails closed and costs only availability;
    the one dangerous direction is a stale enabled value silently un-pressing the kill
    switch, which the durable-first write removes.
  - _Deliberately excluded._ Do not raise `appendfsync` for the whole instance to
    protect one key, and do not introduce a separate coordination store.
    `position:sl_order_id` and `position:opened_at` share this shape and stay in
    ENG-022.
  - _Sequencing._ A stale enabled value only matters once `orders.place_order` can
    actually run, so implement this with the live-order-lifecycle work rather than
    ahead of ENG-021/ENG-022.

### ENG-021 — Make Go verification wrappers and configuration fail reliably

- **Status / priority:** `fixed in code`, `P1` verification blocker; H-1/H-2/M-10,
  B02. Merged as #345. The masking was real, not hypothetical: at `356bbb7` the hook
  exited 0 while apps/collector had 9 findings and apps/notifier 3, hidden behind
  apps/market-hotset being last in go.work and clean. Wrappers, config schema
  locations and all twelve findings are fixed; the linter settings the schema fix
  exposed are carried to ENG-029 rather than adopted by accident. Executed: `make
verify`, `make deadcode`, `pre-commit run --all-files`, and 13 black-box tests that
  run the real scripts, six of which fail against the previous one-liner. This gate
  runs in CI and locally, so there is no separate deployment step for it.
- **Evidence:** `.pre-commit-config.yaml:86` uses a per-module pipeline/while loop
  whose final success masks an earlier failure. Its parser differs from
  `infra/scripts/go_workspace_modules.sh`. `.golangci.yml` declares v2 while using
  root `linters-settings` and `issues.exclude-dirs`; bundled v2 schema and Go types
  reject those locations. The analogous `deadcode` loop in Makefile needs the same
  exit-status review. Full network-dependent config verification was unavailable
  during the audit; the colleague's three notifier issues need a fresh lint run.
- **Bounded remediation:** one workspace parser, empty-list failure, propagate any
  failing module, validate the pinned schema, then fix current lint results.
- **Acceptance:** injected failure in first/middle/last module fails hook/verify;
  both go.work forms work; empty/invalid workspace and bad config keys fail. Do not
  claim all Go tests/vet were ineffective merely because this lint wrapper was wrong.

### ENG-022 — Preserve fills, residual exposure and close accounting across recovery

- **Status / priority:** `planned`, `P1`; C-3/C-4/C-5/H-3/H-6/J-1/J-3, B04;
  reported EP-2 consumer behavior remains a verification subtask.
- **Evidence:** `fill_price.py` accepts positive price with zero filled volume;
  `orders.py` journals requested notional on partial entry, reports a partial exit
  as closed, and removes stop protection/tracking before successful close. The
  per-instrument lock does not reserve the global portfolio slot. These failures
  were reproduced synthetically. `journal.py:901` timestamps a first close at write
  time; delayed paper accounting can acquire extra modeled funding and a new UTC
  day. Missing opened_at defaults differ between legacy live and legacy paper.
- **Small implementation steps:** (1) fill evidence and actual notional;
  (2) partial-close/protection/remaining lifecycle; (3) portfolio reservation using
  existing durable attempts; (4) execution timestamp carried through pending-close
  retries and recovery of missing position age; (5) strategy identity compatibility
  and explicit reconciliation-error summaries after consumer verification.
- **Acceptance:** regression scenarios for zero/partial/full/unknown fills,
  contractSize, failed close after stop cancellation, restart between each external
  boundary, delayed commit across UTC midnight, and concurrent distinct instruments
  at the portfolio limit. Use real PostgreSQL for durable/concurrency guarantees.
  Never skip all protective servicing forever because opened_at is missing.
- **Reuse:** extend ENG-002's implemented reconciliation worker and ENG-013 risk
  limits. Stop-key loss does not make an open exchange position invisible to every
  monitor; keep the narrower protection/tracking claim and test it.

### ENG-023 — Guarantee fair servicing beyond the momentum-paper batch limit

- **Status / priority:** `planned`, `P2` now / `P1` before scaling beyond the limit;
  source E-01, B05. No production threshold breach has been established.
- **Evidence:** `momentum_flow_paper_repository.py:591` orders eligible open probes
  by unchanged entry_at and applies limit=100 by default. A successful quote does
  not remove an open position from that ordering. Later positions can wait while
  the oldest batch remains open. Sequential quotes also delay scheduled outcomes.
- **Bounded remediation:** fair queue/cursor or explicit admission bound, maximum
  quote age and deadline budget; coordinate with ENG-003's venue-aware concurrency.
  Account for the advisory-lock connection before parallelizing DB work.
- **Acceptance:** real repository test above the batch limit services every probe
  over successive ticks without requiring older positions to close; slow/failing
  quotes cannot silently violate the declared observation contract. Preserve frozen
  cohort semantics or introduce a declared version/cutover.

### ENG-024 — Verify coverage artifacts and trace partial-outcome consumers

- **Status / priority:** fingerprint fix `fixed in code`; partial-outcome impact still
  `reported`; E-04/M-8, B06. The audit now hashes the bytes it actually read, refuses a
  file whose fingerprint is not the one it was built against unless that is stated
  explicitly, validates the episode shape, and reports the verified identity and path
  instead of a constant. The September audit's own synthetic `[]` is rejected by name in
  the regression tests. The partial-outcome consumer tracing is untouched and remains
  open.
- **Evidence:** `cex_activity_path_coverage_audit.py:230` reads an arbitrary JSON and
  `render_markdown` prints `_AUDITED_ARTIFACT_FINGERPRINT` without validating that
  input. The audit accepted a synthetic unrelated `[]`. This is now merged code.
  A last bar after a historical window does not exclude suspension/relisting within
  it. `outcomes.py` already labels incomplete windows partial, so downstream
  contamination must be traced rather than asserted for every formal verdict.
- **Bounded remediation:** reuse artifact/schema/checksum verification; report the
  actual verified identity; correct causal claims without changing frozen HYP-016
  outcomes. Verify consumers and exact grid/duplicate/boundary cases separately.
- **Acceptance:** corrupt/unrelated/wrong-schema inputs fail closed; partial extrema
  cannot be treated as exact by formal consumers; independently recalculate a small
  pinned accounting/outcome sample. Preserve original and corrected artifact versions.

### ENG-025 — Establish recovery evidence and capture continuity

- **Status / priority:** `confirmed` script defaults, `reported` production coverage;
  `P1` before unattended live; C-2/M-6/M-11/P-7, B03. Inventory/isolated preparation
  is queued; production actions require their own authorization.
- **Evidence:** `infra/scripts/backup.sh:12` defaults to one retained local dump;
  offsite upload is commented out. This does not prove no separate backup service
  exists. Momentum capture has `restart: 'no'`; current deployment mode and external
  monitoring must be verified. Shared-host analytics pressure is recorded history.
- **Bounded remediation:** inventory copies and disk headroom, choose local/offsite
  generations and RPO/RTO, verify checksum/restore in isolation, and document capture
  restart/alert behavior. Do not blindly retain 14 large dumps on the same full disk.
- **Acceptance:** a dated restore record with schema revision, row/time-range checks,
  achieved RPO/RTO and duration; failed upload does not discard the last verified
  copy; a stopped critical collector is detected. Keep backup-before-migration and
  explicitly handle deployment of the backup script itself.

### ENG-026 — Update vulnerable dependencies and verify runtime exposure

- **Status / priority:** `confirmed` lock finding, `planned`, `P1`; H-7/E-06, B10.
- **Evidence:** September pip-audit found aiohttp 3.14.1 in uv.lock with upstream fix
  3.14.3 for GHSA-cq5v-8q36-5273. Production image versions/exploitation were not
  verified. Other cryptography/pip findings need reachability/tooling separation.
- **Bounded remediation:** update the compatible lock, exercise CCXT adapters,
  rebuild and scan affected images; record deployed versions only after authorized
  rollout. UID/capabilities, DB roles, key permissions and ingress claims from the
  colleague report remain reported hardening checks, not proven compromises.
- **Acceptance:** affected dependency finding cleared, adapter tests pass, image
  inventory matches intended versions; production verification explicitly tracked.

### ENG-027 — Bound web retries/cancellation and verify PWA artifacts

- **Status / priority:** `confirmed`, `planned`, `P2`; M-9/E-02, B10.
- **Evidence:** `apps/web/src/hooks/useOHLCV.ts:34` ignores attempt count for 5xx and
  network failures; the installed Query client retried 12 times until explicit
  cancellation in the synthetic audit. Fetch does not receive AbortSignal. Build
  exit=0 produced an index referencing missing manifest/registerSW resources.
- **Bounded sequence:** shared HTTP boundary with finite retries, cancellation and
  ENG-005 session handling; separate compatible PWA integration/build smoke change.
  Preserve endpoint-specific 404-as-null behavior and distinct error statuses.
- **Acceptance:** finite retries and cancelled network work, no concurrent-401
  logout storm, existing success paths preserved, every referenced generated PWA
  resource exists and registration is exercised by an appropriate smoke check.
- **Residual reported work:** WS deadlines/origin, login/body limits and outbox
  capacity have separate verification scopes in the archive; do not silently drop
  undelivered outbox data to obtain a bounded list.

### ENG-028 — Reuse report serialization only with equivalent measured output

- **Status / priority:** `measuring`, `P3`; E-03/E-05, B11. Synthetic stage-level
  benefit is confirmed; production report impact remains to be measured.
- **Evidence:** existing `reporting.render_dataclass_json` avoids recursive asdict
  copying. On a synthetic 20,000-row report, median serialization was 594 vs 148 ms,
  peak tracked Python allocations 16.45 vs 7.39 MiB, with byte-identical JSON.
  This is not a fourfold whole-report speedup or RSS measurement.
- **Bounded next step:** profile one actual report still using json_ready(asdict),
  then reuse the existing renderer if material. Check nested types, NaN handling,
  ordering, datetime serialization and final newline. No new parser dependency is
  required by this finding.
- **Acceptance:** identical JSON/hash/verdict on pinned real inputs and measured
  useful improvement. Other duplicated loaders require matching contracts and two
  real consumers; do not generalize all research into a speculative framework.

### ENG-029 — Decide the Go lint policy the repaired gate would enforce

- **Status / priority:** `planned`, `P3`; follow-up to ENG-021's schema fix, not an
  audit finding of its own.
- **Evidence:** `.golangci.yml` declared `version: '2'` while writing `gocyclo`,
  `gocritic` and `gosec` settings at the v1 root location, where this version's
  schema rejects them and `run` ignores them silently. The tree was therefore never
  held to `gocyclo.min-complexity: 15` or `gocritic.enabled-tags: [diagnostic,
performance, style]`. Measured with those settings applied at `356bbb7`: 161
  findings, api-gateway 48, collector 90, notifier 23, market-hotset 0, of which
  about 100 are gocritic style suggestions and 49 are gocyclo, including eight
  production functions (`OHLCV`, `computeSignals`, `readCheckpointOrchestrator`,
  trades `List`, `validateCapability`, `Observe`, `Activate`, notifier `tick`).
- **Bounded next step:** decide per setting whether the repo adopts it, then land the
  cleanup separately from the gate repair. A test-only exclusion for gocyclo is a
  legitimate option; silently relaxing a threshold to whatever the tree already
  passes is not. `gosec` severity/confidence is already restored, since it changes
  nothing today.
- **Acceptance:** whatever is adopted is written at a location `golangci-lint config
verify` accepts, and the tree passes it with no baseline file or blanket nolint.

### ENG-031 — Refresh instrument catalogs while the service runs

- **Status / priority:** `confirmed`, `P1`; found on 2026-09-07 by investigating why
  two specific tokens produced no evaluation, not by an audit.
- **Evidence:** `main._preload_markets` loads each client's catalog once at startup and
  nothing refreshes it afterwards, while `symbols.resolve_execution_instrument` reads
  that in-memory `client.markets` dict. An instrument listed after the process started
  is therefore unresolvable for the whole lifetime of that process. In production
  AMEMECOIN was onboarded on bingx on 2026-09-04 while the execution container had run
  since roughly 2026-08-28; its episode `12200` reached +480.15% over sixteen hours and
  the trader wrote 29 consecutive `skipped` / `execution_instrument_unresolved`
  decisions across thirteen hours of it, from +31% to +290%. The first evaluation after
  a deploy restarted the service opened a paper trade on the same instrument at +209%.
  Resolving `AMEMECOIN` against a freshly loaded bingx catalog succeeds, confirming the
  symbol itself was never the problem.
- **Scale:** in the thirty days to 2026-09-07 the same reason accounts for 816 skipped
  evaluations: lbank 301 across 11 bases (max pump 844%), mexc 209 across 15, bingx 130
  across 9, bybit 93, gate 74, bitget 5 (max 907%), binance 4.
- **Why it matters:** the strategy family is new-listing pumps, so the blind spot lines
  up exactly with the instruments the system exists to evaluate. Production runs paper,
  so the cost is not missed trades but missed evidence: observations that candidate
  promotion depends on were never generated at all.
- **Bounded remediation:** one refresher owning the same client objects every component
  already holds, with a periodic sweep bounding staleness and a reload on a resolution
  miss for the minutes-long case, both through one per-exchange cooldown so a base that
  genuinely does not exist on a venue cannot turn every tick into an API call.
- **Acceptance:** an instrument absent from the cached catalog resolves after a refresh;
  repeated misses on a base that does not exist reload at most once per cooldown; a
  failed reload still holds off the next attempt; one venue's failure does not stop the
  others; concurrent misses on one venue reload once; and a miss retries the resolve
  even when the refresh call itself did nothing. That last one is not cosmetic: a
  refresh returns False for a cooldown as well as for a failure, so a trader queued
  behind the periodic sweep's own lock gets False for a catalog that now contains the
  listing, and two new listings on one venue inside the cooldown window hit it with no
  concurrency at all (colleague review). Do not claim the 816 skipped
  evaluations can be recovered: those events are gone, only future ones are protected.
- **First production sweep, 2026-09-07:** the worker ran on schedule (two sweeps,
  seventeen venues each) and the skipped-evaluation count went to zero and stayed there
  for five hours while total decisions per hour held at 400-650, so the fix works. But
  19 of those first 34 reloads failed. Reloading all seventeen venues concurrently
  pushed several past their timeout (`load_markets` is dozens of rate-limited requests
  for venues that paginate per category), and `asyncio.wait_for` cancelling a ccxt request
  mid-flight left its aiohttp connector unusable, observed as
  `File descriptor 21 is used by transport <TCPTransport closed=False reading=True>` on
  bitget. Those clients are shared with everything else in the process, so a broken
  connector is not confined to this worker. Fixed by sweeping one venue at a time,
  removing the asyncio-level deadline in favour of the client's own per-request timeout,
  and logging the exception type, since ccxt renders many errors as a bare
  `<id> <METHOD> <url>` that does not say whether it timed out or was refused.
- **Not covered:** CZ is a different case and not a defect. It resolved on lbank and was
  skipped on score (`score 3 < threshold 5` at pumps up to 602%). Whether that threshold
  is right is a strategy question needing a registered hypothesis, not a fix. Neither CZ
  nor AMEMECOIN appears in the momentum universe at all (zero watch states, zero paper
  probes): those strategies cover bybit and binance, these tokens live on bingx, lbank
  and mexc.

## Promotion summary

The [current roadmap sequence](../../ROADMAP.md#current-delivery-sequence--2026-09-07)
owns selection and the feature/fix balance. September packages map to ENG-020–028
and existing entries rather than creating duplicate implementation queues. The
[archive index](audits/2026-09-06/README.md) maps every B-package, and its reconciliation
retains all 61 source IDs, including rejected, reported and research-only claims.

ENG-001/002 require validation of existing mechanisms, not reconstruction.
ENG-003/023 become critical when paper concurrency threatens observation timing;
ENG-013/020/022 remain live capital-safety gates. ENG-017–019 remain scoped to the
LBank-first thesis, not blockers for unrelated Bybit/Binance evidence. Measurement
items stay measurements until their thresholds justify implementation. A positive
hindsight bound or a proposed TP/score change belongs in research, not the fix queue.

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
