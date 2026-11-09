# Production inventory for maintenance mode (2026-11-09)

Status: **read-only inventory, 2026-11-09; changes nothing.** After the
[program stop](../research/program-review-2026-11-09.md), it records what runs in
production, what it writes, who still reads it, and which services can move to
maintenance. Every stop below needs the owner's own go; nothing was stopped, changed or
deleted. Checked over SSH with `docker compose ps`, `docker inspect`, catalog and size
queries and row counts; no research outcome was read.

## Production state

| Item                   | Value                                                                                                                                                                                  |
| ---------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Deployed revision      | `d87b3a2` (#553), clean checkout                                                                                                                                                       |
| Database migration     | `0064`                                                                                                                                                                                 |
| Live trading path      | #539 to #553 and migrations 0061 to 0064 are **deployed**; LIVE_PROBE is idle: 0 authorizations, 0 reservations, 0 live order attempts, 0 emergency closes, 0 reconciliation incidents |
| Execution modes        | `SOURCE_LEAD_MODE=shadow`, `PUMP_SHORT_MODE=disabled`, `EARLY_MOMENTUM_MODE=disabled`                                                                                                  |
| Open positions         | no trade outside closed, cancelled or rejected; no early-momentum episode armed, claimed or open                                                                                       |
| Disk                   | 55 of 75 GB used (77%), 18 GB free; database 32 GB                                                                                                                                     |
| Pending compose change | #531 pins redis, nats and caddy to the digests running now (verified equal); recreates those three containers on the next deploy                                                       |

`main` is ahead of production only by research documents and research CLIs (no change
under `apps/execution`, `apps/api-gateway`, `apps/collector`, `apps/notifier`, `apps/web`
or `infra` since `d87b3a2`).

## Largest tables

| Table                                           | Size   | In chunks of the last 7 days |
| ----------------------------------------------- | ------ | ---------------------------- |
| `timeseries.bybit_momentum_bars_1m`             | 9.5 GB | 6.2 GB (not yet compressed)  |
| `timeseries.momentum_flow_watch_evaluations_1m` | 4.7 GB | 1.3 GB                       |
| `app.pump_derivatives_context_samples`          | 3.7 GB |                              |
| `app.trade_decisions`                           | 3.4 GB |                              |
| `app.momentum_flow_paper_probes`                | 2.1 GB |                              |
| `app.trade_decision_outcomes`                   | 2.0 GB |                              |
| `app.live_long_short_ratio`                     | 1.6 GB | 0.23 GB                      |
| `app.funding_rate_snapshots`                    | 1.5 GB |                              |
| `app.oi_snapshots`                              | 1.0 GB |                              |
| `timeseries.liquidation_events`                 | 0.9 GB | 0.16 GB                      |

## Services

HYP-012 v2's code reads only `app.source_lead_captures`, `app.source_lead_qualifications`,
`app.source_lead_target_observations`, `app.pump_events` and `app.pump_event_sources`,
written by the `analytics` scanner (which fetches venue tickers itself) and by
`source-lead-exit-capture`.

| Service                                    | Writes                                                 | Reader now                                                  | Proposal                        |
| ------------------------------------------ | ------------------------------------------------------ | ----------------------------------------------------------- | ------------------------------- |
| postgres, redis, nats, caddy               | platform                                               | everything                                                  | keep                            |
| analytics (scanner, source-lead capture)   | pumps, funding and OI snapshots, source-lead captures  | **HYP-012 v2**                                              | keep                            |
| source-lead-exit-capture                   | source-lead exit observations                          | **HYP-012 v2**                                              | keep                            |
| execution                                  | trade decisions (source-lead shadow), live path tables | HYP-012 v2 shadow; LIVE_PROBE idle                          | keep                            |
| api-gateway, web, notifier                 | UI, alerts                                             | owner                                                       | keep                            |
| collector                                  | market data on NATS                                    | group 1 and 2 services only (see below)                     | stop with group 2               |
| outcome-resolver                           | `trade_decision_outcomes`                              | research reports of closed lines only (see below)           | stop with group 2               |
| momentum-capture, momentum-capture-binance | `bybit_momentum_bars_1m` and flow                      | HYP-015 (rejected); disabled early-momentum code; UI panels | **stop first** (largest growth) |
| momentum-watch                             | `momentum_flow_watch_evaluations_1m`                   | HYP-015 (rejected); UI and WATCH alerts                     | **stop first**                  |
| momentum-paper, momentum-paper-hold12h     | `momentum_flow_paper_probes`                           | HYP-015 (rejected); UI and trades page                      | **stop first**                  |
| liquidation-capture-bybit, -binance        | `liquidation_events`                                   | none (family 3 not feasible on Bybit)                       | stop second                     |
| market-hotset, lsr-subscriber              | `live_long_short_ratio`                                | UI only (the HYP-015 inputs archive is closed with HYP-015) | stop second                     |

Already stopped: burst-probe, orderflow-pilot, momentum-watch-binance,
momentum-paper-binance, momentum-paper-lev3.

## What a stop means here

- **Reversible:** `docker compose stop <service>` keeps the container and its data;
  `start` brings it back. A permanent change (a compose profile, so a deploy does not
  restart it) is a separate PR.
- **Data stays:** no table or file is deleted; backups and their drill continue.
- **Visible effects:** stopping the momentum services stops the WATCH alerts and freezes
  the momentum panels and the paper trades page; stopping the LSR services freezes its
  panel.
- **Not touched:** anything HYP-012 v2 needs, until it is terminal (deadline 2027-03-31,
  next checkpoint 2026-11-30).

## Stopping group 1 in two steps

On 2026-11-09 the paper contracts still held open probes: 19 for
`momentum_flow_paper_v1` and 63 for `momentum_flow_paper_v1_hold12h`, the latest opened
at 20:50 UTC. Stopping all five services at once would leave them open for ever. So:

1. Stop `momentum-watch` first: no new WATCH decision, so no new probe.
2. After the longest hold (720 minutes), check that no probe is open, then stop
   `momentum-paper`, `momentum-paper-hold12h`, `momentum-capture` and
   `momentum-capture-binance`, and record the time.
3. After the next routine cycles, check that `analytics`, `source-lead-exit-capture` and
   `execution` stay fresh and healthy; if not, start the stopped services again and
   find the dependency. A week later, compare the tables' growth.

A plain `docker compose up -d` (for example a deploy, or #531's three containers)
starts every stopped service that has no profile again. Until the stopped services
move under a compose profile (its own PR), deploys and #531 restart only the services
they need, by name.

## Checks for the second group (done 2026-11-09, read only)

- **collector** publishes only `market.bybit.ticker.*` and `binance.lsr.*` on NATS. Their
  subscribers are `momentum-capture` (group 1), `market-hotset` and `lsr-subscriber`;
  `api-gateway` connects to NATS for its health check only. HYP-012 v2's scanner fetches
  venue tickers itself. So `collector` has no consumer once groups 1 and 2 stop.
- **outcome-resolver** writes `app.trade_decision_outcomes`, read only by research
  reports of closed lines (episode selection, HYP-024 order flow) and not by HYP-012 v2's
  code or any running service.

Group 2 is therefore `liquidation-capture-bybit`, `liquidation-capture-binance`,
`market-hotset`, `lsr-subscriber`, `collector` and `outcome-resolver`, each on the owner's
go, after group 1 has run a week without trouble.
