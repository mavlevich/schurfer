# api-gateway

Go service exposing REST + WebSocket API to the web frontend.
Handles authentication (JWT in HttpOnly cookie) and infrastructure health checks.

## Endpoints

| Method | Path                      | Auth   | Description                                             |
| ------ | ------------------------- | ------ | ------------------------------------------------------- |
| POST   | `/auth/login`             | public | Login with password, sets JWT cookie                    |
| POST   | `/auth/logout`            | JWT    | Clears JWT cookie                                       |
| GET    | `/healthz`                | public | Liveness probe - always 200 while process is alive      |
| GET    | `/api/health`             | JWT    | Dependencies, host load, and market-pipeline telemetry  |
| GET    | `/api/research/readiness` | JWT    | Lightweight collection progress for registered research |
| WS     | `/ws/status`              | JWT    | Live status stream, pushes every 5s                     |

`/api/health` and `/ws/status` include container-visible one/five/fifteen-minute
load, CPU count, memory, root-filesystem usage, uptime, and the latest bounded
`market:hotset:health` counters. These diagnostics are informational and never
turn missing optional telemetry into a failed readiness probe.

`/api/research/readiness` does not run CCXT or strategy replay. It reports exact
exit-quote calibration counts, mature database-input proxies for the HYP-008 and
HYP-010 cohorts, and operational order-flow capture estimates from Redis. The
response labels estimates explicitly; only the frozen analytics reports can issue
formal research output.

## Environment variables

| Variable              | Default                                                      | Required |
| --------------------- | ------------------------------------------------------------ | -------- |
| `DATABASE_URL`        | `postgresql://schurfer:schurfer_dev@localhost:5432/schurfer` |          |
| `REDIS_ADDR`          | `localhost:6379`                                             |          |
| `NATS_URL`            | `nats://localhost:4222`                                      |          |
| `PORT`                | `8000`                                                       |          |
| `ENV`                 | `development`                                                |          |
| `ADMIN_PASSWORD_HASH` | -                                                            | yes      |
| `JWT_SECRET`          | -                                                            | yes      |

## Setup

```bash
# Generate password hash
go run ./cmd/hash-password your_password

# Generate JWT secret
openssl rand -hex 32

# Copy and fill .env (from project root)
cp .env.example .env
```

## Run

```bash
# Local (from project root, with .env loaded)
go run ./apps/api-gateway/cmd/api-gateway

# Docker
docker compose -f infra/docker/docker-compose.dev.yml up api-gateway
```

## Auth flow

1. `POST /auth/login` with `{"password": "..."}` → sets `schurfer_token` HttpOnly cookie
2. All subsequent requests carry the cookie automatically (browser / Capacitor)
3. `POST /auth/logout` clears the cookie
