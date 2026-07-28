# Docker

Local development environment.

## Quick start

```bash
make dev        # start all services
make dev-stop   # stop all services
make dev-reset  # stop and remove all data
```

## Services

| Service    | Port | Description                    |
| ---------- | ---- | ------------------------------ |
| PostgreSQL | 5432 | Main database with TimescaleDB |
| Redis      | 6379 | Hot state cache, pub/sub       |
| NATS       | 4222 | Message bus (with JetStream)   |
| NATS HTTP  | 8222 | NATS monitoring dashboard      |

## Connection strings

```
postgres://schurfer:schurfer_dev@localhost:5432/schurfer
redis://localhost:6379
nats://localhost:4222
```

## Notes

- TimescaleDB is bundled in the `timescale/timescaledb` image (no separate container needed)
- NATS runs with JetStream enabled for persistent messaging
- Redis is configured with bounded memory and `noeviction`, so critical state is
  never silently removed
- Data is persisted in Docker volumes between restarts
