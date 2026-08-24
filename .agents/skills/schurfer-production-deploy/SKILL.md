---
name: schurfer-production-deploy
description: Plan, execute when explicitly authorized, or verify Schurfer production deployments, migrations, repairs, reports, and post-deploy health. Use for questions about what to deploy after merge or how to validate production; never treat this skill as authorization for a live mutation.
---

# Schurfer Production Deploy

Read `AI_RULES.md`. Deployment guidance does not itself authorize SSH, a
database mutation, repair, restart, migration, or order-mode change. Obtain
that authority from the user's request and keep the action within scope.

## Classify the change

Inspect the merged diff and map files to affected services, migrations,
workers, reports, environment variables, volumes, and one-shot repair steps.

- Code-only, one service, no migration: use
  `make prod-deploy-svc SERVICE=<compose-service>` from a clean `main`.
- Any Alembic migration, cross-service contract, Compose dependency, durable
  volume change, or data repair: take a verified backup and use the migration
  or full `make prod-deploy` path.
- Analytics report code that runs only through `docker compose run --rm` may
  need an image rebuild but not a persistent service restart. Verify its
  mounted input/output paths survive the disposable container.
- Never deploy all services merely because a merge completed; select the
  smallest scope that activates the change safely.

## Preflight

Verify the host repository is on clean `main`, `git pull --ff-only` succeeded,
and record the expected revision. Confirm disk/RAM headroom, current container
health/restart counts, backup destination and retention when applicable, and
that required environment variables are present without printing secrets.
For a repair, run read-only discovery/dry-run first, save an immutable manifest,
and require idempotent re-application.

## Post-deploy evidence

Do not call a deployment successful after `docker compose up` alone. Verify:

1. host revision and image/container creation time;
2. service health, restart count, and logs since the deployment boundary;
3. migration version in `app.alembic_version` and expected schema objects;
4. strategy mode ceilings (`AUTO_TRADE`, `DRY_RUN`, per-strategy mode) without
   exposing credentials;
5. relevant worker heartbeats, source freshness, queue/backlog, error counters,
   and last successful action;
6. DB/Redis/episode/trade consistency, duplicates, overdue claims, incomplete
   accounting, and unresolved closes for the changed path;
7. one naturally occurring or bounded smoke case proving the new fields and
   lifecycle, without forcing a market trade.

Define a rollback trigger and command before changing state. If a migration or
repair is not safely reversible, state that explicitly and use forward repair
rather than pretending rollback is available.
