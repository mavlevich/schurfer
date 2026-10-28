# Liquidation heartbeat data-loss flag: queue backlog, not loss (2026-10-28)

A read-only measurement of `timeseries.liquidation_capture_heartbeats_1m` on production,
run 2026-10-28 from the host's `psql` against the `schurfer-postgres` container. Nothing
was changed. It records why the ROADMAP support queue defers a liquidation-writer fix.

## Mechanism (code)

`CoverageTracker.SnapshotAndReset` in
`apps/collector/internal/liquidationcapture/coverage.go` sets `DataLossDetected` when
`writer.QueueDepth > 0`, that is when accepted events are still waiting for the writer
at the heartbeat. Those events are persisted later; the flag marks the minute as lost
anyway.

## Window and query

Window `[2026-10-14T00:00:00Z, 2026-10-28T00:00:00Z)`, 20,160 minutes per exchange.
Counters are cumulative per process session, so per-minute values are differences
within `(exchange, process_session_id)`. Backlog is received minus persisted, duplicate
and dropped events. A minute is `queue_only` when it is flagged, all connections are
up, it is not a session's first minute, and it has no queue drop, invalid event or
reconnect.

```sql
WITH h AS (
  SELECT exchange, process_session_id, bucket_start, data_loss_detected,
         connected_connections = expected_connections AS conn_ok,
         events_received_total - lag(events_received_total) OVER w AS recv,
         queue_drops_total - lag(queue_drops_total) OVER w AS drops,
         invalid_events_total - lag(invalid_events_total) OVER w AS invalid,
         reconnect_total - lag(reconnect_total) OVER w AS reconnects,
         lag(bucket_start) OVER w IS NULL AS session_first,
         events_received_total - events_persisted_total - duplicate_events_total - queue_drops_total AS backlog,
         lead(events_received_total - events_persisted_total - duplicate_events_total - queue_drops_total) OVER w AS next_backlog
  FROM timeseries.liquidation_capture_heartbeats_1m
  WHERE bucket_start >= '2026-10-14T00:00:00Z' AND bucket_start < '2026-10-28T00:00:00Z'
  WINDOW w AS (PARTITION BY exchange, process_session_id ORDER BY bucket_start)
), q AS (
  SELECT *, data_loss_detected AND conn_ok AND NOT session_first
            AND coalesce(drops, 0) = 0 AND coalesce(invalid, 0) = 0 AND coalesce(reconnects, 0) = 0 AS queue_only
  FROM h
)
SELECT exchange,
       count(*) AS minutes,
       count(DISTINCT process_session_id) AS sessions,
       count(*) FILTER (WHERE data_loss_detected) AS flagged,
       count(*) FILTER (WHERE queue_only) AS queue_only,
       count(*) FILTER (WHERE queue_only AND backlog > 0) AS queue_only_backlog_pos,
       count(*) FILTER (WHERE queue_only AND next_backlog <= 0) AS queue_only_cleared_next,
       round(avg(recv) FILTER (WHERE queue_only), 1) AS recv_per_min_queue_only,
       round(avg(recv) FILTER (WHERE NOT data_loss_detected), 1) AS recv_per_min_unflagged,
       coalesce(sum(drops), 0) AS queue_drops,
       coalesce(sum(reconnects), 0) AS reconnects
FROM q GROUP BY exchange ORDER BY exchange;
```

## Result

| exchange | minutes | sessions | flagged | queue_only | backlog > 0 | cleared next minute | events/min queue_only | events/min unflagged | queue drops | reconnects |
| -------- | ------- | -------- | ------- | ---------- | ----------- | ------------------- | --------------------- | -------------------- | ----------- | ---------- |
| binance  | 20160   | 1        | 267     | 265        | 263         | 247                 | 246.8                 | 22.6                 | 0           | 2          |
| bybit    | 20160   | 1        | 99      | 45         | 37          | 44                  | 405.6                 | 9.8                  | 0           | 60         |

At the last heartbeat before the window end (2026-10-27 23:59Z) the backlog is 0 on both
exchanges: within the one session each, every received event was persisted or counted
as a duplicate, and none was dropped.

## Reading

- The `queue_only` minutes are liquidation bursts (about 247 and 406 events a minute
  against about 23 and 10 otherwise) whose events were still queued at the heartbeat.
  They are not losses: drops are 0 and the backlog is 0 at the window end.
- Most clear by the next minute (247 of 265 and 44 of 45); the rest clear within a
  burst of several busy minutes.
- Not explained by the heartbeat columns: Binance 2 and Bybit 8 `queue_only` minutes
  with no positive backlog at the heartbeat (the flag can also come from persist errors
  or a payload hash mismatch, which the heartbeat does not store).
- The other flagged minutes (Binance 2, Bybit 54) have reconnects: those are real
  coverage gaps and the flag is right for them.

## Consequence

No reader uses the flag today. The fix (count only real loss, or write a minute's
heartbeat once its backlog is persisted) waits for the first liquidation study that
needs per-minute completeness. The heartbeat hypertable keeps 180 days (retention
policy), so the query above reproduces this table until about 2027-04-12.
