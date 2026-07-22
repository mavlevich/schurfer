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
healthchecks; the worker services (analytics, execution, collector, notifier) do not
yet, so `--wait` only waits for them to be running, not healthy. `caddy` depends on
`api-gateway` and `web` being healthy before it starts.

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

## Incidents

Record notable incidents and their fixes here as they happen, so the next person (or
the same person in six months) has the context.
