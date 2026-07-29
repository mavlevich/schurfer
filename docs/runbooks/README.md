# Runbooks

Operational procedures for the production server and recurring tasks.

Production runs on a single Hetzner host with the Docker Compose prod stack behind
Caddy. Access is over Tailscale only. There is no public web exposure. Everything
below runs from the repo root on the server (`/opt/schurfer`) unless noted.

The real hostname, IP, and SSH user are not in this repo. Keep them in your local
SSH config (for example an alias `schurfer` that points at the Tailscale hostname).
Commands below use `schurfer` as that alias.

## Access

The host is only reachable over the tailnet.

```bash
# SSH (over Tailscale)
ssh schurfer

# Reach Postgres from your machine through an SSH tunnel
ssh -L 15432:127.0.0.1:5432 schurfer
# then connect a local client to localhost:15432
```

If SSH hangs, check that Tailscale is up on both your machine and the server
(`tailscale status`).

## Deploy a change

Golden rule: never hand-edit a git-tracked file on the server. Commit, push, open the
PR, merge it, then deploy from `main`. The only file edited directly on the server is
`.env.prod`, which is not tracked by git.

The standard deploy is one command:

```bash
ssh schurfer
cd /opt/schurfer
make prod-deploy
```

`make prod-deploy` runs, in order: assert the checkout is clean and on `main`,
**backup**, `git pull --ff-only origin main`, start the datastores, **run migrations**
(`alembic upgrade head`), rebuild and restart all services and wait for them to come
up (`--wait`), prune old images, and print health. Running migrations before building
the services is deliberate: a new revision may expect columns a migration adds, so the
schema must be current first. The branch and clean-tree guards stop you from deploying
a stray feature branch or uncommitted local edits.

Before treating a deploy as done, confirm the commit is actually on `main`. A PR can
be merged while a late commit is not, which silently drops it:

```bash
git log origin/main --oneline -3 -- <changed file>
```

### Migrations

You do not run migrations by hand in the normal flow: `make prod-deploy` already runs
`alembic upgrade head` every time. It is idempotent, so it is a no-op when the schema
is already current, which is exactly why it is safe to run on every deploy: you can
never forget one. To apply a schema change without a full redeploy:

```bash
make prod-migrate
```

### Faster single-service redeploy (optional)

For a code-only change to one service with NO new migration, rebuild just that service
instead of everything. Use the guarded target (same branch and clean-tree checks and
`--ff-only` pull as `prod-deploy`), not a raw `git pull` + `compose up`, which could
run on a stray branch or merge instead of fast-forward:

```bash
make prod-deploy-svc SERVICE=execution
```

Caveat: this path skips both the backup and the migration. If the change includes a
new Alembic migration, use `make prod-deploy` instead. When in doubt, use
`make prod-deploy`.

### Post-deploy verification

Do not trust "containers are up". After a change that writes new data, look at the
first real rows to confirm the pipeline produces what you expect. Example for the
decision-measurement change:

```sql
-- docker exec schurfer-postgres psql -U schurfer -d schurfer
SELECT ts, base, action, score, decision_id, strategy_version,
       liquidity->>'status' AS liquidity_status
FROM app.trade_decisions ORDER BY ts DESC LIMIT 20;
```

### Rollback

If a deploy misbehaves, redeploy a previous known-good commit. Do NOT use
`make prod-deploy` for this: it pulls `main` and would fast-forward straight back to
the broken version. Use the rollback target, which checks out the given commit with no
pull and no migration:

```bash
make prod-rollback REV=<previous-good-sha>
```

After `prod-rollback` the server is on a detached HEAD, so the next `make prod-deploy`
will refuse to run (its branch guard). Once you have a forward fix merged to `main`,
return to normal deploys with:

```bash
git switch main
git pull --ff-only origin main
```

Note: checking out old code does NOT undo a migration. A schema change is forward-only
here; to reverse one, run an explicit `alembic downgrade` or restore from the
pre-deploy backup. Prefer a forward fix over a downgrade.

## Health, logs, backup

```bash
make prod-health          # container status and health
make prod-logs            # tail all services
make prod-backup          # manual DB backup (also runs on a cron)
PROD_HOST=schurfer make prod-restore-local   # restore latest prod backup into local dev (run from your machine)
```

Raw docker for a single service:

```bash
docker ps --filter name=schurfer-execution --format '{{.Names}}: {{.Status}}'
docker logs schurfer-execution --since 15m -f
docker logs schurfer-api-gateway --since 1h 2>&1 | grep -i error
```

The datastores (postgres, redis, nats) and api-gateway, web, and caddy have Compose
healthchecks; the worker services (analytics, execution, collector, market-hotset,
notifier) do not yet, so `--wait` only waits for them to be running, not healthy.
`caddy` depends on `api-gateway` and `web` being healthy before it starts.

### Bybit real-time hot set

The `collector` publishes broad Bybit ticker events to NATS. `market-hotset` consumes
them, keeps a bounded in-memory prebuffer, and retains five-second bars only for
symbols found in `pumps:measurement`. It is a research data path and cannot trade.

After the first deployment, verify both event flow and the bounded storage contract:

```bash
docker logs schurfer-market-hotset --since 10m

docker exec schurfer-redis redis-cli --raw \
  HGETALL market:hotset:health

docker exec schurfer-redis redis-cli --scan \
  --pattern 'market:hot:bars:bybit:*'

docker exec schurfer-redis redis-cli XLEN \
  market:hot:bars:bybit:AKEUSDT

docker exec schurfer-redis redis-cli XRANGE \
  market:hot:bars:bybit:AKEUSDT - + COUNT 2

docker stats --no-stream \
  schurfer-collector schurfer-market-hotset schurfer-nats schurfer-redis
```

Healthy steady state means `pump_feed_status=ok`, event rate is non-zero,
`invalid_total`, `out_of_order_total`, `nats_dropped_total`,
`pending_dropped_total`, and `persist_errors_total` do not grow, and `last_lag_ms`
and `window_max_lag_ms` stay low. Empty bar streams are normal when no current
measurement pump maps to a Bybit contract. Streams are capped at 3,600 entries and
expire 24 hours after their last retained bar. Do not raise the limits before
checking Redis memory and host available RAM.

`unmapped_candidates` counts measurement pumps without an explicit Bybit market id.
The consumer deliberately does not guess `base + USDT`: symbols can be reused for
different assets across venues. Downstream ingestion must de-duplicate the
at-least-once stream by `(exchange, symbol, bucket_start_ms, pump_event_id)`.
The four-hour watch registry is stored in the `market:hotset:bybit` sorted set with
metadata in `market:hotset:bybit:metadata`, so a consumer restart does not shorten an
already registered observation window.

This first slice uses Core NATS and keeps the current five-second bucket in memory.
It is low-latency measurement, not a lossless event log: a process or host failure
can lose in-flight ticker events, the open bucket, and the in-memory prebuffer.
The drop, lag, and persistence counters make those gaps visible. Any signal promoted
to formal replay input must first move through the planned durable research layer.

Test the restore periodically. A backup that has never been restored is not a backup.

## Environment and secrets

- `.env.prod` lives on the server only and is not in git. It holds
  `POSTGRES_PASSWORD`, `JWT_SECRET`, `ADMIN_PASSWORD_HASH`, exchange API keys, the
  Tailscale hostname, and trading config.
- Templates are in the repo: `.env.prod.example` and `.env.example`. The developer
  fills in real values on the server.
- To change a value, edit `.env.prod` on the server, then recreate the affected
  service so it picks up the new env (`make prod-deploy`, or `up -d` that service).

## Database

```bash
# Open a psql shell inside the container
docker exec -it schurfer-postgres psql -U schurfer -d schurfer

# Run a one-off query
docker exec schurfer-postgres psql -U schurfer -d schurfer -c "SELECT count(*) FROM app.pump_events;"
```

Production PostgreSQL is bound to server loopback only. Verify after infrastructure
changes:

```bash
docker port schurfer-postgres 5432
sudo ss -lntp | grep 5432
# Expected: 127.0.0.1:5432, never 0.0.0.0:5432 or [::]:5432.
stat -c '%a %U:%G %n' .env.prod  # expected mode: 600
```

For local pgAdmin, keep port 5432 private and use an SSH/Tailscale tunnel:

```bash
ssh -N -L 15432:127.0.0.1:5432 schurfer
# pgAdmin: host 127.0.0.1, port 15432, database/user schurfer.
```

Before `AUTO_TRADE=true`, complete the database/host security gate: replace the shared
PostgreSQL superuser with separate migration-owner, application, and read-only roles;
set backup directory/file permissions to 700/600 and encrypt offsite copies; test a
restore; verify firewall rules; apply OS updates and reboot; keep exchange keys
withdrawal-disabled and IP-restricted. Store credentials in a password manager, never
in the repository. Measurement-only operation may continue before this gate because
the database is private and currently contains no exchange credentials or customer PII.

## Trading kill-switch

Trading is gated by the `trading:enabled` Redis flag and by the `AUTO_TRADE` and
`DRY_RUN` env values.

```bash
# Stop all trading immediately (the Telegram /stop command does the same)
docker exec schurfer-redis redis-cli set trading:enabled false

# Resume
docker exec schurfer-redis redis-cli set trading:enabled true
```

To take the system fully out of live trading, set `AUTO_TRADE=false` (or
`DRY_RUN=true`) in `.env.prod` and recreate the execution service.

### Market-quality gate

Execution records an order-book snapshot for every candidate and, for score-eligible
entries, fails closed unless both the short-entry bid side and buy-to-close ask side
are executable. Defaults are `REQUIRE_MARKET_QUALITY=true`, `MAX_SPREAD_BPS=50`,
`MAX_LIQUIDITY_IMPACT_BPS=50`, and `LIQUIDITY_DEPTH_MULTIPLIER=2`. With the current
`SIGNAL_POSITION_USD=50`, the book must fill at least $100 on each side. A transient
market-quality skip uses a five-minute seen TTL and is re-evaluated later. If an
exchange minimum would round the actual order above that checked $100 notional, order
placement rejects it instead of relying on an unmeasured part of the book.

The full verdict lives at `trade_decisions.liquidity.quality`; inspect calibration and
skip reasons rather than silently relaxing thresholds:

```sql
SELECT liquidity #>> '{quality,reason}' AS reason, count(*)
FROM app.trade_decisions
WHERE strategy_version = 'pump_short_v1_market_quality'
GROUP BY 1 ORDER BY 2 DESC;
```

`AUTO_TRADE=true` refuses to start when `REQUIRE_MARKET_QUALITY=false`. Changing any
threshold changes strategy eligibility, so also change `STRATEGY_VERSION` and evaluate
the new cohort separately.

## Common issues

- Service stuck unhealthy: check its logs, then confirm its dependencies (Postgres,
  Redis, NATS) are healthy first.
- Caddy unhealthy: its healthcheck hits the local admin API at
  `http://127.0.0.1:2019/config/`. Confirm the Caddyfile mounted correctly and the
  cert files exist.
- Chart or OHLCV missing for a token: check api-gateway logs for `pumps.ohlcv.fetch`
  warnings. Some tokens are only tradeable on one exchange, and exchange APIs
  occasionally change field formats.
- Scanner produced no pumps: check analytics logs and that outbound network to the
  exchanges works from the host.
- Decision outbox (durable Redis Stream): execution XADDs each decision to
  `execution:decisions`; a writer task drains it into Postgres and XACKs + XDELs only
  after the commit, so decisions survive an execution restart and a Postgres outage.
  A message that fails to insert `_MAX_ATTEMPTS` (5) times — for example a value that
  violates a column constraint (a `STRATEGY_VERSION` longer than `varchar(32)`) — is
  moved to `execution:decisions:dlq` instead of blocking the stream forever. Inspect:

  ```
  redis-cli XLEN execution:decisions                          # backlog depth
  redis-cli XPENDING execution:decisions decision-db-writers  # unacked / stuck
  redis-cli XRANGE execution:decisions:dlq - + COUNT 20       # poison messages
  redis-cli CONFIG GET appendonly                             # AOF on
  redis-cli CONFIG GET appendfsync                            # everysec
  redis-cli INFO persistence
  ```

  A growing `XLEN` while the writer is connected means Postgres is unreachable (the
  stream buffers safely until it returns). A growing DLQ means recurring poison rows —
  read them from the DLQ stream and fix the cause.

  Durability guarantee: after a successful XADD a decision survives an execution
  restart and a Postgres outage; redelivery does not duplicate (idempotent on
  `decision_id`). Not yet covered: `opened`/`opened_dry_run` have a window between the
  order/paper side effect and the XADD (the trade itself is still in the trade
  journal), and a Redis-host crash can lose up to ~1s with `appendfsync everysec`.
  Two-phase `intent -> resolution` closes the opened-window and is planned for a
  follow-up PR (required before `AUTO_TRADE=true`).

- Forward outcome resolver: the separate `outcome-resolver` service reads due rows
  from `app.trade_decisions`, fetches 5-minute OHLCV, and idempotently writes
  strategy-agnostic forward metrics to `app.trade_decision_outcomes`. It resolves
  15m, 30m, 1h, 4h, 8h, 24h, 72h, and 7d horizons. The candle already in progress at
  decision time is excluded so pre-decision prices cannot leak into MAE/MFE; a
  `complete` result requires every expected closed bar. Inspect:

  ```sql
  SELECT horizon_minutes, status, count(*)
  FROM app.trade_decision_outcomes
  GROUP BY horizon_minutes, status
  ORDER BY horizon_minutes, status;

  SELECT d.ts, d.base, o.horizon_minutes, o.status, o.coverage_ratio,
         o.mfe_pct, o.mae_pct, o.short_return_pct, o.attempt_count, o.error
  FROM app.trade_decision_outcomes o
  JOIN app.trade_decisions d ON d.decision_id = o.decision_id
  ORDER BY o.updated_at DESC
  LIMIT 50;
  ```

  `partial`, `missing_ohlcv`, `fetch_failed`, `unsupported_exchange`, and
  `complete_fallback` rows retry after `OUTCOME_RETRY_AFTER`, up to
  `OUTCOME_MAX_ATTEMPTS`; this lets late OHLCV or a newly enabled venue repair a
  lower-quality row. `missing_price` is terminal. A `complete_fallback` row used another
  measured candidate venue because the decision's anchor venue was unavailable or had
  poorer coverage; do not silently mix it with exact-venue results. This worker does
  not simulate exits or costs — those belong to the versioned virtual-strategy
  analysis.

  LBank perpetual OHLCV is a known permanent source gap rather than a transient fetch
  failure. The resolver does not call its spot-only historical path for swap symbols.
  It tries other recorded candidate venues and stores a terminal
  `complete_fallback_unsupported` result when one has complete coverage. A
  LBank-only decision becomes terminal `market_path_unavailable`. Both preserve LBank
  as the decision's anchor; only the explicitly fallback-enabled research views accept
  the cross-venue result.

- Durable derivatives context: migration `0017` adds
  `app.pump_derivatives_context_runs` and
  `app.pump_derivatives_context_samples`. The existing `outcome-resolver` process
  starts recovery only after the eight-hour post-anchor window is complete, so this
  change needs a full deploy rather than an analytics-only restart:

  ```bash
  make prod-deploy

  docker exec schurfer-postgres psql -U schurfer -d schurfer -c \
    "SELECT version_num FROM app.alembic_version"

  docker logs schurfer-outcome-resolver --since 15m 2>&1 \
    | grep -E 'derivatives_context.starting|derivatives_context.resolved|outcomes.tick_failed'
  ```

  The forward cohort starts at `2026-07-27T00:00:00Z`. The initial allowlist persists
  funding and OI only where the v2 probe returned valid timestamped rows, plus Binance
  long/short ratios and HTX liquidations. Mark/index/premium candles remain
  reconstructable report inputs and are not duplicated into Postgres. HTX funding and
  liquidation requests are capped at 100 rows per page even when the generic fetch
  limit is 200. Resolver version `derivatives_context_v2` also sends an explicit end
  bound for Binance long/short-ratio and Bybit open-interest history. Version `v1`
  exposed that both endpoints otherwise returned a moving 200-row latest tail and
  omitted the beginning of older requested windows.

  Inspect work coverage and stored rows:

  ```sql
  SELECT exchange, method, status, count(*) AS runs,
         sum(in_window_rows) AS rows, max(attempt_count) AS max_attempts
  FROM app.pump_derivatives_context_runs
  GROUP BY exchange, method, status
  ORDER BY exchange, method, status;

  SELECT r.event_id, r.exchange, r.method, r.status, r.coverage_ratio,
         r.request_limit, r.request_count, r.in_window_rows,
         r.attempt_count, r.error, r.updated_at
  FROM app.pump_derivatives_context_runs r
  ORDER BY r.updated_at DESC
  LIMIT 50;

  SELECT r.exchange, r.method, count(*) AS samples,
         min(s.source_at) AS first_source_at, max(s.source_at) AS last_source_at
  FROM app.pump_derivatives_context_samples s
  JOIN app.pump_derivatives_context_runs r ON r.id = s.run_id
  GROUP BY r.exchange, r.method
  ORDER BY r.exchange, r.method;
  ```

  `sampled` is terminal. Transient failures, no data, partial/incomplete coverage,
  window mismatch, and missing current symbols retry after
  `DERIVATIVES_CONTEXT_RETRY_AFTER`, up to
  `DERIVATIVES_CONTEXT_MAX_ATTEMPTS`. Every attempt updates the run row and increments
  `attempt_count`; samples use a deterministic key, so retries do not create duplicate
  points. `identity_mismatch` is terminal and intentionally fetches no history: inspect
  the recorded and current exchange market metadata before changing that policy.
  These public historical measurements never replace the live liquidity snapshot
  attached to a trade decision.

- Decision measurement report: after rebuilding the analytics image, run a read-only
  Markdown report against production:

  ```bash
  make prod-measurement-report
  make prod-measurement-report ARGS="--since 2026-07-22 --exchange-horizon 240"
  make prod-measurement-report ARGS="--format json --strategy-version pump_short_v1_market_quality"
  ```

  It reports decisions/hour, field completeness, signal lag, market-quality reasons,
  due/unresolved outcomes, and raw short return/MAE/MFE by strategy, horizon, segment,
  and exchange. Treat these as descriptive diagnostics: the report shows distinct pump
  episodes beside decision N, but only the versioned virtual replay will enforce one
  chronological decision path per episode, model costs/exits, and produce confidence
  intervals suitable for champion selection.

- Decision-quality report: use the completed exact-anchor episode cohort to test
  whether the recorded score and its five point components separate better and worse
  virtual trades after fees, funding, and decision-time liquidity impact:

  ```bash
  make prod-deploy-svc SERVICE=analytics
  make prod-decision-quality-report
  make prod-decision-quality-report \
    ARGS="--until 2026-08-03T00:00:00Z --format json" \
    > backups/reports/decision-quality-2026-08-03.json
  ```

  The default scope starts at `2026-07-26T00:00:00Z` and uses
  `pump_short_v1_market_quality`. `score_any` is the market-quality-only control;
  score 6 is the current baseline; score 4, 5, 7, 8, and 9 are descriptive threshold
  views. The `score_6_without_*` rows subtract the persisted point contribution of one
  component while keeping the cutoff fixed. They diagnose whether a component admits
  useful or harmful episodes, but are not causal estimates.

  Report v2 adds a matched-economics section for `score_any`, `score_4`, and
  `score_6`. All three rows use the same completely resolved episode set. A policy
  that does not trigger contributes zero return and zero costs, so different trade
  rates do not silently change the denominator. Gross return is separated from
  decision-time entry impact, modeled exit impact, taker fees, conservative funding,
  and net return. Bid and ask impact are VWAP distances from mid and already include
  the crossed half-spread. The spread buckets are descriptive and must not be
  subtracted again.

  The same run also reports discovery-only exit-mechanics ablations on identical
  decisions and exact candle paths: full v1, pump-band max-hold only, initial SL plus
  max-hold, and a stop-free fixed 240-minute reference. Read each paired delta against
  its named reference. The effects interact and cannot be added together. The
  initial-stop follow-through table counts baseline initial-stop exits that would
  later show positive modeled net return at 240 minutes and reports the MAE incurred
  by holding. It does not claim that disabling or widening the stop is safe. Any such
  candidate requires fixed-dollar-risk sizing, drawdown, and liquidation-distance
  analysis on a separate registered cohort.

  OI and funding component tables keep `missing` separate from an observed zero-point
  value. A missing source currently defaults to zero points in the live score, but it
  is not evidence of the market condition represented by a genuine zero-point
  observation.

  Review input exclusions, unresolved policy evaluations, exact path coverage,
  matched episode N, completed trades, cash episodes, cluster count and
  largest-cluster share, gross-to-net decomposition, paired exit effects, net
  expectancy and its 95% cluster-bootstrap interval, recorded-size P&L, profit factor,
  sequential episode drawdown, initial-stop rate, MFE/MAE, and the score/component
  calibration tables. Also inspect pump-size, venue, action, spread, impact, and
  liquidity-quality segments for obvious concentration or data-quality artifacts.
  Fewer than 50 resolved episodes is `collecting`; 50 or more is only a directional
  read; 100 episodes and 30 clusters is `formal_size`, not a confirmatory verdict.

  A stricter policy that rejects a recorded `opened` or `opened_dry_run` decision is
  marked `right_censored_after_recorded_open`, not cash. The real execution path stops
  producing later decisions after opening, so the report cannot know whether that
  stricter policy would have triggered later in the episode.

  The report is discovery-only and never changes production. Total P&L and drawdown
  order independent episode results chronologically; they do not model shared capital,
  overlapping positions, `MAX_POSITIONS`, margin, or the daily loss breaker. Any
  promising score rule must be pre-registered in the next score-threshold PR and
  confirmed on a new untouched cohort. Use `--allow-fallback` only as a separately
  labelled sensitivity run.

- Episode replay input readiness: after rebuilding the analytics image, validate the
  pre-registered confirmatory cohort without running a strategy simulation:

  ```bash
  make prod-episode-replay
  make prod-episode-replay ARGS="--format json"
  make prod-episode-replay ARGS="--since 2026-07-22 --horizon 240 --horizon 480"
  ```

  The default run starts at the locked confirmation cutoff, requires exact anchor-venue
  8-hour outcomes, groups decisions by direct `pump_event_id`, and excludes an entire
  episode if any decision path input is incomplete. The manifest records the deployed
  Git revision, working-tree dirty state, exclusive data cutoff, query/resolver
  versions, accepted outcome statuses, and a deterministic input fingerprint.
  `--allow-fallback` is a sensitivity run and must not be presented as exact-venue
  confirmatory evidence.

- Entry-confirmation challenger replay: after deploying the analytics image, compare
  the registered HYP-002 family without changing production trading configuration:

  ```bash
  make prod-virtual-entry-challenger-report
  make prod-virtual-entry-challenger-report \
    ARGS="--until 2026-08-03T00:00:00Z --format markdown"
  ```

  The default cohort begins at `2026-07-29T00:00:00Z`. The report compares red-candle,
  1.5% retrace, and combined confirmation against the same baseline episodes. It uses
  only fully closed exact-venue 5-minute candles, waits at most 60 minutes, and treats
  no confirmation as a zero-return cash episode. Missing candles or costs remain
  unresolved and are never replaced by later episodes inside the locked first-100
  sample. Before 100 fully resolved episodes and 30 clusters, formal intervals and
  verdicts are withheld. Once ready, inspect the 95% expectancy intervals,
  Holm-adjusted paired tests, conservative familywise paired bounds, and top-five
  cluster sensitivity. A passing result is only a live-shadow candidate; do not change
  production entry rules from this command alone. Baseline eligibility is held
  constant during the wait, so future score and market-quality gates still require
  live shadow validation. The exact inference parameters, data sources, archive
  command, and inspection checklist live in `ROADMAP.md` and the research protocol.

- Entry-floor challenger replay: after the +20% measurement cohort has accumulated
  closed episodes with exact-anchor 8-hour outcomes, compare the registered HYP-003
  family without changing the production +30% hard gate:

  ```bash
  make prod-virtual-threshold-challenger-report
  make prod-virtual-threshold-challenger-report \
    ARGS="--until 2026-08-10T00:00:00Z --format markdown"
  ```

  The default cohort begins at `2026-07-27T07:00:00Z` and combines
  `pump_short_measurement_v1` with `pump_short_v1_market_quality` inside the same
  parent episodes. Baseline +30% is paired with +20%, +25%, +35%, +40%, and +50%.
  Each floor selects the first recorded crossing that passes the stored score and
  market-quality gates; a floor never reached contributes zero-return cash. Missing
  exact-venue paths or cost inputs remain unresolved. Formal inference is withheld
  before 100 fully paired episodes and 30 asset clusters. Archive a JSON run with a
  cutoff selected before looking at threshold output; the full checklist is in
  `ROADMAP.md`.

- Exit-policy challenger replay: after the registered OBS-001 cohort has accumulated
  closed episodes with exact-anchor 8-hour outcomes, compare the bounded exit family
  without changing the production exit:

  ```bash
  make prod-virtual-exit-policy-report
  make prod-virtual-exit-policy-report \
    ARGS="--until 2026-08-10T00:00:00Z --format markdown"
  ```

  The default cohort begins at `2026-07-29T00:00:00Z`. Every policy uses the same
  point-in-time decision, next complete 5-minute entry, exact venue, and cost inputs.
  The report requires every candle through the longest registered policy window, so
  a truncated path leaves the whole paired family unresolved. It reports net
  expectancy, recorded-size P&L, profit factor, sequential episode drawdown, exit
  reasons, duration, MFE/MAE, captured move, and paired deltas. Formal inference is
  withheld before the first 100 episodes are completely paired across at least 30
  asset clusters. A passing policy advances only to live shadow.

- Score-threshold challenger replay: after the HYP-006 cohort has accumulated closed
  episodes with exact-anchor 8-hour outcomes, compare score 6 against the registered
  score 4 and 5 family:

  ```bash
  make prod-virtual-score-challenger-report
  make prod-virtual-score-challenger-report \
    ARGS="--until 2026-08-10T00:00:00Z --format json" \
    > backups/reports/score-thresholds-2026-08-10.json
  ```

  The default cohort begins at `2026-07-31T00:00:00Z`. Running the command before
  that time exits with a concise usage error and does not query the database. Each
  policy selects its first recorded score crossing that passes the recorded
  market-quality gate. A threshold never reached is zero-return cash. Inspect exact
  selected-decision path coverage, unresolved policies, cluster concentration, trade
  rate, net expectancy, profit factor, drawdown, initial stops, captured MFE, and
  paired deltas. Formal inference is withheld before 100 fully paired episodes and 30
  asset clusters. A passing result is only a live-shadow candidate. Score 7 and 8
  belong to the live multi-variant shadow evaluator because baseline opens
  right-censor their later recorded decisions.

- Candle anomaly research: after the registered cohort has accumulated closed episodes
  with exact-anchor 8-hour outcomes, derive HYP-005 features and join them to the
  baseline virtual replay:

  ```bash
  make prod-candle-anomaly-report
  make prod-candle-anomaly-report \
    ARGS="--until 2026-08-05T00:00:00Z --format json" \
    > backups/reports/candle-anomalies-2026-08-05.json
  ```

  The cohort starts at `2026-07-29T00:00:00Z`; running the command before that time
  intentionally exits without querying an invalid or future interval.

  The command uses only fully closed exact-venue 5-minute candles available by each
  decision. Inspect feature and volume coverage, unresolved paths, cluster
  concentration, all four pre-registered blow-off/reversal buckets, net return,
  MFE/MAE, captured move, and initial-stop rate. This report is descriptive and cannot
  authorize a production gate. Any candidate feature needs a separately registered
  out-of-sample live-shadow cohort.

- Derivatives-context coverage probe: after deploying the analytics image, test which
  recoverable CCXT history is actually usable around recent pump episodes whose
  post-anchor windows have completed:

  ```bash
  make prod-derivatives-context-report
  make prod-derivatives-context-report \
    ARGS="--exchange binance --exchange bybit --format json" \
    > backups/reports/derivatives-context-$(date -u +%Y-%m-%d).json
  ```

  The default selection looks back 14 days and chooses at most one identity-safe,
  exact-symbol target per exchange whose eight-hour post-anchor window has completed.
  Each request is bounded to four hours before and eight hours after the anchor, with
  a 200-row page limit, at most 10 pages, and a 15-second timeout per request. Review
  `sampled`, `incomplete`, `window_mismatch`, `partial`, `no_data`, `unsupported`, and
  failure rows method by method. For regular series, `sampled` means the requested
  boundaries, expected row count, and cadence were all covered; inspect coverage,
  bounds, duplicates, and maximum gap. For sparse event series, `sampled` only means
  that at least one valid event was recoverable inside the window. A capability flag
  alone is not evidence of historical coverage, and `emulated` remains distinct from
  native support. Effective timeframes and explicit venue overrides are printed in
  every result. Either side of the requested window is capped at seven days. The
  command is read-only, does not persist rows, does not replace live liquidity
  snapshots, and cannot change production trading.

- Pump/signal readiness after deploying migration 0012: verify newly published pumps
  carry an episode id and recent decisions are attributed. `signal_missing` or
  `signal_episode_mismatch` may occur for one execution tick while the minute-based
  signal cache catches up, but either must retry after 60 seconds rather than being
  suppressed for 30 minutes:

  ```bash
  docker exec schurfer-redis redis-cli --raw GET pumps:latest
  docker exec schurfer-postgres psql -U schurfer -d schurfer -c \
    "SELECT ts, base, reason, score, pump_event_id FROM app.trade_decisions ORDER BY ts DESC LIMIT 20"
  ```

- Measurement/entry floor split: migration 0016 adds the immutable
  `entry_qualified_at` strategy anchor, so use a full deploy rather than individual
  service deployment. The private feed must contain the public feed as a thresholded
  subset, while below-entry decisions use the measurement strategy version and never
  reach the order path:

  ```bash
  make prod-deploy
  docker exec schurfer-postgres psql -U schurfer -d schurfer -c \
    "SELECT version_num FROM app.alembic_version"
  docker exec schurfer-redis redis-cli --raw GET pumps:measurement
  docker exec schurfer-redis redis-cli --raw GET pumps:latest
  docker exec schurfer-analytics env \
    | grep -E 'PUMP_MEASUREMENT_MIN_PCT|PUMP_ENTRY_MIN_PCT'
  docker exec schurfer-execution env \
    | grep -E 'PUMP_ENTRY_MIN_PCT|MEASUREMENT_STRATEGY_VERSION|AUTO_TRADE|DRY_RUN'
  docker exec schurfer-postgres psql -U schurfer -d schurfer -c \
    "SELECT ts, base, pump_pct, action, reason, strategy_version
     FROM app.trade_decisions
     WHERE strategy_version = 'pump_short_measurement_v1'
     ORDER BY ts DESC LIMIT 20"
  docker exec schurfer-postgres psql -U schurfer -d schurfer -c \
    "SELECT date_trunc('hour', ts) AS hour, reason, count(*)
     FROM app.trade_decisions
     WHERE strategy_version = 'pump_short_measurement_v1'
       AND ts >= NOW() - INTERVAL '24 hours'
     GROUP BY 1, 2 ORDER BY 1 DESC, 3 DESC"
  ```

  The migration revision must be `0016`. Expected defaults are a private +20%
  measurement floor, a +30% hard entry/public floor, and
  `pump_short_measurement_v1` for lower-floor decisions. `first_seen_at` preserves the
  measurement start; `entry_qualified_at` is set once when +30% is first observed.
  Signal age and OI baseline use the latter after qualification, preserving the v1
  entry clock. The parent event remains live while it stays at or above +20%, so
  repeated +30% crossings remain one correlated research episode. Keep
  `AUTO_TRADE=false` and `DRY_RUN=true` during measurement. A measurement-only row
  must have `action=skipped` and `reason=pump_below_entry_floor`. Monitor hourly row
  growth and exchange rate-limit warnings after rollout. `entry_floor_invalid` must
  remain zero; any occurrence means the private feed contract is malformed and
  execution has correctly failed closed.

- Versioned paper performance accounting: migration 0018 adds explicit gross/net
  fields and accounting provenance to `app.trades`. This touches the migration,
  execution, API, web, and analytics images, so deploy the full stack:

  ```bash
  make prod-deploy
  make prod-health
  docker exec schurfer-postgres psql -U schurfer -d schurfer -c \
    "SELECT version_num FROM app.alembic_version"
  docker exec schurfer-postgres psql -U schurfer -d schurfer -c \
    "SELECT accounting_version, accounting_status, count(*)
     FROM app.trades
     GROUP BY 1, 2
     ORDER BY 1, 2"
  ```

  The migration revision must be `0018`. Existing closed trades must be
  `legacy_price_only_v1` with populated `gross_pnl_*` and null `net_pnl_*`. New
  paper trades must open with `paper_conservative_costs_v1` and `pending`, then close
  as `complete` only when both decision-time bid and ask impact are available. An
  incomplete row keeps gross P&L but leaves net P&L null. The Trade Journal must label
  legacy results as gross-only and calculate net statistics only from complete rows.
  Verify the first newly closed paper trade:

  ```bash
  docker exec schurfer-postgres psql -U schurfer -d schurfer -c \
    "SELECT id, symbol, accounting_version, accounting_status,
            gross_pnl_usd, fees_usd, funding_usd, slippage_usd, net_pnl_usd,
            accounting_error
     FROM app.trades
     WHERE status = 'closed'
     ORDER BY exit_at DESC
     LIMIT 10"
  ```

  `paper_conservative_costs_v1` uses the shared replay contract: 10 bps taker per
  side, 5 bps funding per eight hours, and entry-time bid/ask impact held constant.
  It is a conservative paper estimate. It is not a substitute for importing actual
  venue fills, fees, and funding when real trading is enabled.

## Incidents

Record notable incidents and their fixes here as they happen, so the next person (or
the same person in six months) has the context.
