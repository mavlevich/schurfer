# Runbooks

Operational procedures for the production server and recurring tasks.

Production runs on a single Hetzner host with the Docker Compose prod stack behind
Caddy. Access is over Tailscale only. There is no public web exposure.

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

Golden rule: never hand-edit a git-tracked file directly on the server. Always commit,
push, then pull on the server. The only file edited directly on the server is
`.env.prod`, which is not tracked by git.

```bash
# 1. Merge the PR into main (locally: commit, push, open PR, merge on GitHub).

# 2. On the server, pull and rebuild only the services that changed.
ssh schurfer
cd /opt/schurfer
git pull origin main
docker compose --env-file .env.prod -f infra/docker/docker-compose.prod.yml up -d --build <service>
```

`<service>` is one of `api-gateway`, `web`, `execution`, `analytics`, `collector`,
`notifier`, `caddy`. Omit it to rebuild everything. The `--env-file .env.prod` flag is
required, otherwise Compose cannot read `POSTGRES_PASSWORD` and other prod values.

After a merge, confirm the commit actually landed before treating it as done:

```bash
git log origin/main --oneline -3 -- <changed file>
```

## Check health

```bash
# All containers and their health state
docker ps --format '{{.Names}}: {{.Status}}'

# One service
docker ps --filter name=schurfer-api-gateway --format '{{.Names}}: {{.Status}}'
```

Every long-running service has a healthcheck. `caddy` depends on `api-gateway` and
`web` being healthy before it starts.

## Logs

```bash
# Tail a service
docker logs schurfer-execution --since 15m -f

# Errors only
docker logs schurfer-api-gateway --since 1h 2>&1 | grep -i error
```

## Restart or rebuild a service

```bash
# Restart without rebuilding
docker restart schurfer-analytics

# Rebuild from latest code (after git pull)
docker compose --env-file .env.prod -f infra/docker/docker-compose.prod.yml up -d --build analytics
```

## Environment and secrets

- `.env.prod` lives on the server only and is not in git. It holds
  `POSTGRES_PASSWORD`, `JWT_SECRET`, `ADMIN_PASSWORD_HASH`, exchange API keys, the
  Tailscale hostname, and trading config.
- Templates are in the repo: `.env.prod.example` and `.env.example`. The developer
  fills in real values on the server.
- To change a value, edit `.env.prod` on the server, then recreate the affected
  service so it picks up the new env.

## Database

```bash
# Open a psql shell inside the container
docker exec -it schurfer-postgres psql -U schurfer -d schurfer

# Run a one-off query
docker exec schurfer-postgres psql -U schurfer -d schurfer -c "SELECT count(*) FROM app.pump_events;"
```

### Backup and restore

```bash
# Manual backup (also runs on a cron)
/opt/schurfer/infra/scripts/backup.sh

# Restore the latest prod backup into local dev (run from your machine)
PROD_HOST=schurfer bash infra/scripts/restore-local.sh
```

Test the restore periodically. A backup that has never been restored is not a backup.

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

- Service stuck unhealthy: check `docker logs <name>`, then confirm its dependencies
  (Postgres, Redis, NATS) are healthy first.
- Caddy unhealthy: its healthcheck hits the local admin API at
  `http://127.0.0.1:2019/config/`. Confirm the Caddyfile mounted correctly and the
  cert files exist.
- Chart or OHLCV missing for a token: check api-gateway logs for `pumps.ohlcv.fetch`
  warnings. Some tokens are only tradeable on one exchange, and exchange APIs
  occasionally change field formats.
- Scanner produced no pumps: check analytics logs and that outbound network to the
  exchanges works from the host.

## Incidents

Record notable incidents and their fixes here as they happen, so the next person (or
the same person in six months) has the context.
