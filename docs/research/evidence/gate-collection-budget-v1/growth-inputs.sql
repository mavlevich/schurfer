-- Gate collection budget (PR 5): disk growth inputs from database metadata only.
-- Reads relation and chunk sizes, retention policies and row counts by creation
-- time inside a window that ends before the HYP-012 v2 blind window (2026-09-29).
-- No value column is read.
\set window_start '2026-09-15'
\set window_end '2026-09-29'
WITH plain(name, col) AS (
    VALUES
        ('app.pump_derivatives_context_samples', 'created_at'),
        ('app.trade_decisions', 'created_at'),
        ('app.momentum_flow_paper_probes', 'created_at'),
        ('app.trade_decision_outcomes', 'created_at'),
        ('app.funding_rate_snapshots', 'recorded_at'),
        ('app.pump_events', 'first_seen_at'),
        ('app.pump_event_sources', 'first_seen_at'),
        ('app.oi_snapshots', 'recorded_at'),
        ('app.pump_derivatives_context_runs', 'created_at'),
        ('app.pump_event_snapshots', 'recorded_at'),
        ('app.momentum_flow_paper_outcomes', 'created_at')
),
chunk AS (
    SELECT
        c.hypertable_schema || '.' || c.hypertable_name AS hypertable,
        c.range_start,
        c.range_end,
        pg_total_relation_size(format('%I.%I', c.chunk_schema, c.chunk_name)::regclass)
        + coalesce((
            SELECT pg_total_relation_size(format('%I.%I', cc.schema_name, cc.table_name)::regclass)
            FROM _timescaledb_catalog.chunk AS k
            JOIN _timescaledb_catalog.chunk AS cc ON cc.id = k.compressed_chunk_id
            WHERE k.schema_name = c.chunk_schema AND k.table_name = c.chunk_name
        ), 0) AS bytes
    FROM timescaledb_information.chunks AS c
)
SELECT json_build_object(
    'measured_at', now(),
    'window', json_build_object('start', :'window_start', 'end', :'window_end'),
    'database_bytes', pg_database_size(current_database()),
    'plain_tables', (
        SELECT json_agg(json_build_object(
            'table', name,
            'time_column', col,
            'bytes', pg_total_relation_size(name::regclass),
            'rows', (xpath('/row/c/text()', query_to_xml(
                format('SELECT count(*) AS c FROM %s', name), false, true, '')))[1]::text::bigint,
            'rows_in_window', (xpath('/row/c/text()', query_to_xml(
                format('SELECT count(*) AS c FROM %s WHERE %I >= %L AND %I < %L',
                       name, col, :'window_start', col, :'window_end'), false, true, '')))[1]::text::bigint
        ) ORDER BY name)
        FROM plain
    ),
    'hypertables', (
        SELECT json_agg(json_build_object(
            'hypertable', h.hypertable_schema || '.' || h.hypertable_name,
            'bytes', hypertable_size(format('%I.%I', h.hypertable_schema, h.hypertable_name)::regclass),
            'oldest_chunk_start', (SELECT min(range_start) FROM chunk WHERE hypertable = h.hypertable_schema || '.' || h.hypertable_name),
            'retention_drop_after', (
                SELECT j.config ->> 'drop_after' FROM timescaledb_information.jobs AS j
                WHERE j.proc_name = 'policy_retention'
                  AND j.hypertable_schema = h.hypertable_schema AND j.hypertable_name = h.hypertable_name
            ),
            'chunks', (
                SELECT json_agg(json_build_object('start', range_start, 'end', range_end, 'bytes', bytes) ORDER BY range_start)
                FROM chunk WHERE hypertable = h.hypertable_schema || '.' || h.hypertable_name
            )
        ) ORDER BY h.hypertable_schema, h.hypertable_name)
        FROM timescaledb_information.hypertables AS h
    )
);
