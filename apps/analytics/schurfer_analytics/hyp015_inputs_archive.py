"""HYP-015 reader inputs: archive, snapshot sets and a restore check, never a verdict.

Design: docs/runbooks/hyp015-inputs-archive-design-v1.md. The registered reader
(`momentum_flow_hold12h_verdict_reader`) needs the WATCH decisions in
`timeseries.momentum_flow_watch_evaluations_1m`, whose 45-day retention drops the
cohort's first day on 2026-11-19, plus plain tables that are not at risk. This module:

- archives the watch chunks of the cohort window with the shared engine
  (`history_archive`), no fence and no deletion;
- takes a **snapshot set**: every plain input exported from ONE `REPEATABLE READ`
  snapshot, the verified watch revisions pinned and re-fingerprinted against the source
  in that same snapshot, and a blind **composition reference** recorded with them;
- runs a **restore check** into a throwaway database: the plain inputs whole and the
  rows of the reader's WATCH denominator from the pinned chunks, then the fingerprints,
  the composition and the registered reader's readiness and health paths on the
  restored copy.

Blindness: nothing here selects a return, fee, funding amount, price or PnL value into
a computation or an output. The composition reads ids, counts, statuses, horizons and
coverage only, through the reader's own filter and coverage rules. The formal path
(`load_cohort`, formal claims, the formal CLI flag) is not imported or reachable.
"""

from __future__ import annotations

# ruff: noqa: S608 -- the only interpolations into SQL are schema names (validated by
# Schemas), table names from pinned contracts and module constants; values are bound.
import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .cold_bar_fetch import FetchError, sha256_of, stream_member
from .cold_bar_gated_deletion_collectors import borg_list_members_args, parse_short_list
from .history_archive import (
    DEFAULT_RESERVE_BYTES,
    FINGERPRINT_VERSION,
    MAX_MANIFEST_BYTES,
    ArchiveError,
    CatalogRow,
    DatasetContract,
    StepReport,
    _run,
    _snapshot,
    archiver_session,
    borg_env,
    columns_from_spec,
    covering_ranges,
    duck_row_text,
    export_snapshot_member,
    list_chunks,
    live_rows,
    pg_fingerprint_sql,
    pg_fingerprint_where_sql,
    record_export,
    restore_into,
    run_archive,
    run_export,
    run_verify,
    snapshot_file_names,
    snapshot_rows,
    verify_extracted,
)
from .momentum_flow_hold12h_verdict import ACTUAL_FUNDING_VERSION, HOLD12H_VERDICT_CONTRACT
from .momentum_flow_hold12h_verdict_reader import (
    HEALTH_FUNDING_LAG,
    Schemas,
    _funding_covered_sql,
    _watch_filter_sql,
    _watch_params,
    load_health_checkpoint,
    load_readiness,
)
from .momentum_flow_hold12h_verdict_report import (
    formal_cohort_start,
    formal_decision_prefix_end,
)
from .momentum_flow_paper_contract import FROZEN_PAPER_CONTRACT, HOLD12H_PAPER_CONTRACT

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

PURPOSE = "hyp015_inputs"
ARCHIVE_PREFIX = "history-hyp015-"
CONTRACT_VERSION = "hyp015_inputs_v1"
REQUIRED_HORIZONS = (240, 720)
RESTORED = Schemas(timeseries="hyp015_restore_ts", app="hyp015_restore_app")


def _window() -> tuple[datetime, datetime]:
    start = formal_cohort_start(HOLD12H_VERDICT_CONTRACT)
    end = formal_decision_prefix_end(HOLD12H_VERDICT_CONTRACT)
    if start is None or end is None:
        raise ArchiveError("the HYP-015 contract has no registered cohort window")
    return start, end


COHORT_START, DECISION_PREFIX_END = _window()
# Daily chunks on bucket_start; the denominator filters on decision_at, so one day of
# margin on each side keeps every WATCH decision of the window inside archived chunks.
WATCH_WINDOW = (COHORT_START - timedelta(days=1), DECISION_PREFIX_END + timedelta(days=1))

FUNDING_RUN_COLUMNS = columns_from_spec(
    "id:bigint,exchange:character varying(32),native_market_id:character varying(128),"
    "unified_symbol:character varying(128),market_type:character varying(16),"
    "requested_since:timestamp with time zone,requested_until:timestamp with time zone,"
    "status:character varying(24),request_count:integer,settlements_written:integer,"
    "error:text,source_version:character varying(32),created_at:timestamp with time zone"
)
FUNDING_SETTLEMENT_COLUMNS = columns_from_spec(
    "id:bigint,exchange:character varying(32),native_market_id:character varying(128),"
    "unified_symbol:character varying(128),market_type:character varying(16),"
    "settlement_at:timestamp with time zone,funding_rate:double precision,"
    "source_at:timestamp with time zone,observed_at:timestamp with time zone,"
    "fetched_at:timestamp with time zone,native_payload:jsonb,"
    "source_version:character varying(32),created_at:timestamp with time zone"
)
OUTCOME_COLUMNS = columns_from_spec(
    "paper_id:uuid,horizon_minutes:integer,due_at:timestamp with time zone,"
    "status:character varying(24),quote_requested_at:timestamp with time zone,"
    "quote_observed_at:timestamp with time zone,"
    "exchange_event_at:timestamp with time zone,quote_latency_ms:integer,"
    "best_bid:double precision,best_ask:double precision,mid:double precision,"
    "spread_bps:double precision,bid_vwap:double precision,"
    "bid_impact_bps:double precision,filled_notional_usd:double precision,"
    "gross_return_pct:double precision,net_return_pct:double precision,"
    "gross_pnl_usd:double precision,net_pnl_usd:double precision,"
    "fees_usd:double precision,funding_usd:double precision,"
    "accounting_status:character varying(16),accounting_error:text,error:text,"
    "created_at:timestamp with time zone,updated_at:timestamp with time zone"
)
PROBE_COLUMNS = columns_from_spec(
    "paper_id:uuid,paper_version:character varying(64),"
    "watch_version:character varying(64),watch_id:uuid,episode_id:uuid,"
    "exchange:character varying(32),market_type:character varying(16),"
    "symbol:character varying(32),watch_bucket_start:timestamp with time zone,"
    "watch_decision_at:timestamp with time zone,claimed_at:timestamp with time zone,"
    "entry_status:character varying(32),entry_reason:character varying(64),"
    "entry_quote_requested_at:timestamp with time zone,"
    "entry_quote_observed_at:timestamp with time zone,"
    "entry_exchange_event_at:timestamp with time zone,entry_quote_latency_ms:integer,"
    "unified_symbol:character varying(64),market_id:character varying(64),"
    "contract_size:double precision,entry_best_bid:double precision,"
    "entry_best_ask:double precision,entry_mid:double precision,"
    "entry_spread_bps:double precision,entry_vwap:double precision,"
    "entry_impact_bps:double precision,entry_filled_notional_usd:double precision,"
    "entry_at:timestamp with time zone,position_status:character varying(24),"
    "exit_reason:character varying(32),exit_quote_requested_at:timestamp with time zone,"
    "exit_quote_observed_at:timestamp with time zone,"
    "exit_exchange_event_at:timestamp with time zone,exit_quote_latency_ms:integer,"
    "exit_best_bid:double precision,exit_best_ask:double precision,"
    "exit_mid:double precision,exit_spread_bps:double precision,"
    "exit_vwap:double precision,exit_impact_bps:double precision,"
    "exit_filled_notional_usd:double precision,exit_at:timestamp with time zone,"
    "max_favorable_return_pct:double precision,max_adverse_return_pct:double precision,"
    "gross_return_pct:double precision,net_return_pct:double precision,"
    "gross_pnl_usd:double precision,net_pnl_usd:double precision,"
    "fees_usd:double precision,funding_usd:double precision,"
    "accounting_status:character varying(16),accounting_error:text,last_error:text,"
    "created_at:timestamp with time zone,updated_at:timestamp with time zone"
)
UNIVERSE_INSTRUMENT_COLUMNS = columns_from_spec(
    "exchange:character varying(32),universe_version:character varying(64),"
    "catalog_version:character varying(64),native_market_id:character varying(64),"
    "base:character varying(32),quote:character varying(32),settle:character varying(32),"
    "native_market_type:character varying(64),"
    "canonical_market_type:character varying(64),onboarded_at:timestamp with time zone,"
    "identity_status:character varying(32),identity_key:text,metadata_hash:bytea"
)
UNIVERSE_SNAPSHOT_COLUMNS = columns_from_spec(
    "exchange:character varying(32),universe_version:character varying(64),"
    "catalog_version:character varying(64),capture_version:character varying(32),"
    "schema_version:character varying(32),captured_at:timestamp with time zone,"
    "instrument_count:integer,payload_hash:bytea,created_at:timestamp with time zone"
)
WATCH_COLUMNS = columns_from_spec(
    "exchange:character varying(32),market_type:character varying(16),"
    "symbol:character varying(32),capture_version:character varying(32),"
    "watch_version:character varying(64),bucket_start:timestamp with time zone,"
    "universe_version:character varying(64),quality_ready:boolean,raw_qualified:boolean,"
    "decision_status:character varying(40),reason_codes:text[],"
    "price_return_60m_pct:double precision,price_return_15m_pct:double precision,"
    "oi_growth_60m_pct:double precision,buy_notional_15m_usd:double precision,"
    "sell_notional_15m_usd:double precision,flow_notional_15m_usd:double precision,"
    "buy_imbalance_15m:double precision,"
    "flow_acceleration_15m_vs_prior_45m:double precision,cross_section_size:integer,"
    "oi_growth_threshold_pct:double precision,buy_imbalance_threshold:double precision,"
    "flow_acceleration_threshold:double precision,"
    "source_event_at:timestamp with time zone,"
    "source_received_at:timestamp with time zone,"
    "bucket_ready_at:timestamp with time zone,"
    "evaluator_started_at:timestamp with time zone,"
    "evaluator_completed_at:timestamp with time zone,"
    "decision_at:timestamp with time zone,episode_id:uuid,watch_id:uuid,"
    "state_active_after:boolean,state_clear_streak_after:integer,"
    "state_last_watch_at_after:timestamp with time zone,input_hash:bytea,"
    "created_at:timestamp with time zone"
)


WATCH_CONTRACT = DatasetContract(
    dataset="hyp015_watch_evaluations",
    contract_version=CONTRACT_VERSION,
    schema_version="hyp015_watch_parquet_v1",
    export_version="hyp015_export_v1",
    source_schema="timeseries",
    source_table="momentum_flow_watch_evaluations_1m",
    time_column="bucket_start",
    key=("exchange", "market_type", "symbol", "watch_version", "bucket_start"),
    columns=WATCH_COLUMNS,
    archive_prefix=ARCHIVE_PREFIX,
    window=WATCH_WINDOW,
)


def _plain(
    table: str,
    columns: Any,
    key: tuple[str, ...],
    *,
    data_key_column: str | None = "exchange",
) -> DatasetContract:
    return DatasetContract(
        dataset=f"hyp015_{table}",
        contract_version=CONTRACT_VERSION,
        schema_version=f"hyp015_{table}_parquet_v1",
        export_version="hyp015_export_v1",
        source_schema="app",
        source_table=table,
        time_column=None,
        key=key,
        columns=columns,
        archive_prefix=ARCHIVE_PREFIX,
        unit="snapshot",
        data_key_column=data_key_column,
    )


# Supersets of what the reader selects: whole tables (probes and outcomes of every paper
# version, since the health rule compares hold12h with the baseline worker).
PLAIN_CONTRACTS = (
    _plain("momentum_flow_paper_probes", PROBE_COLUMNS, ("paper_id",)),
    _plain(
        "momentum_flow_paper_outcomes",
        OUTCOME_COLUMNS,
        ("paper_id", "horizon_minutes"),
        data_key_column=None,
    ),
    _plain(
        "momentum_universe_snapshots",
        UNIVERSE_SNAPSHOT_COLUMNS,
        ("exchange", "universe_version", "catalog_version"),
    ),
    _plain(
        "momentum_universe_instruments",
        UNIVERSE_INSTRUMENT_COLUMNS,
        ("exchange", "universe_version", "catalog_version", "native_market_id"),
    ),
    _plain("hold12h_funding_coverage_runs", FUNDING_RUN_COLUMNS, ("id",)),
    _plain("hold12h_funding_settlements", FUNDING_SETTLEMENT_COLUMNS, ("id",)),
)
ALL_CONTRACTS = (WATCH_CONTRACT, *PLAIN_CONTRACTS)


# The reader's WATCH denominator in DuckDB syntax, for picking the restored rows out of
# the archived chunks. The same constants as `_watch_filter_sql`.
def denominator_duck(window_end: datetime) -> str:
    """The reader's WATCH denominator in DuckDB syntax, up to `window_end`, for picking
    the restored rows out of the archived chunks. The same constants as
    `_watch_filter_sql`."""
    return (
        "decision_status = 'watch' "
        f"AND watch_version = '{HOLD12H_PAPER_CONTRACT.watch_version}' "
        f"AND exchange = '{HOLD12H_PAPER_CONTRACT.source_exchange}' "
        f"AND market_type = '{HOLD12H_PAPER_CONTRACT.market_type}' "
        "AND watch_id IS NOT NULL AND episode_id IS NOT NULL "
        f"AND decision_at >= '{COHORT_START.isoformat()}'::TIMESTAMPTZ "
        f"AND decision_at < '{window_end.isoformat()}'::TIMESTAMPTZ"
    )


# ---------- blind composition ----------


def _psycopg(sql: str) -> str:
    """The reader's SQLAlchemy `:name` parameters as psycopg `%(name)s` (no `::` casts
    occur in the reused fragments)."""
    return re.sub(r"(?<!:):([a-z_]+)", r"%(\1)s", sql)


def _params(window_end: datetime) -> dict[str, Any]:
    return {
        **_watch_params(COHORT_START, window_end),
        "base": FROZEN_PAPER_CONTRACT.paper_version,
        "hold": HOLD12H_PAPER_CONTRACT.paper_version,
        "fv": ACTUAL_FUNDING_VERSION,
        "lag_cutoff": window_end - HEALTH_FUNDING_LAG,
        "horizons": list(REQUIRED_HORIZONS),
    }


def _counts(conn: Any, sql: str, params: Mapping[str, Any]) -> dict[str, int]:
    return {str(k): int(v) for k, v in conn.execute(sql, params).fetchall()}


def composition(
    conn: Any, schemas: Schemas, window_end: datetime = DECISION_PREFIX_END
) -> dict[str, Any]:
    """Ids, counts, statuses, horizons and coverage of the cohort's inputs. Never a
    return, fee, funding amount, price or PnL. Identical SQL runs on the source (inside
    the snapshot set's transaction) and on a restored copy."""
    ts, app = schemas.timeseries, schemas.app
    p = _params(window_end)
    w = f"WITH w AS (SELECT w.watch_id {_psycopg(_watch_filter_sql(ts))})"
    probes = (
        f"{w}, p AS (SELECT p.* FROM {app}.momentum_flow_paper_probes p "
        "JOIN w ON w.watch_id = p.watch_id WHERE p.paper_version = %(hold)s)"
    )
    eligible = conn.execute(
        f"{w} SELECT count(*), encode(sha256(convert_to(coalesce(string_agg("
        "watch_id::text, ',' ORDER BY watch_id::text), ''), 'UTF8')), 'hex') FROM w",
        p,
    ).fetchone()
    cohort = conn.execute(
        f"{probes} SELECT count(*), encode(sha256(convert_to(coalesce(string_agg("
        "paper_id::text, ',' ORDER BY paper_id::text), ''), 'UTF8')), 'hex') FROM p",
        p,
    ).fetchone()
    health = conn.execute(
        f"""{w}, per AS (
            SELECT w.watch_id,
                coalesce(bool_or(p.paper_version = %(base)s), false) AS b_seen,
                coalesce(bool_or(p.paper_version = %(base)s
                    AND p.entry_status = 'rejected_stale'), false) AS b_stale,
                coalesce(bool_or(p.paper_version = %(hold)s), false) AS h_seen,
                coalesce(bool_or(p.paper_version = %(hold)s
                    AND p.entry_status = 'rejected_stale'), false) AS h_stale
            FROM w LEFT JOIN {app}.momentum_flow_paper_probes p
              ON p.watch_id = w.watch_id AND p.paper_version IN (%(base)s, %(hold)s)
            GROUP BY w.watch_id)
        SELECT count(*) FILTER (WHERE NOT b_seen), count(*) FILTER (WHERE b_stale),
            count(*) FILTER (WHERE NOT h_seen), count(*) FILTER (WHERE h_stale)
        FROM per""",
        p,
    ).fetchone()
    covered = _psycopg(_funding_covered_sql(app))
    past_lag = conn.execute(
        f"""{probes} SELECT count(*), count(*) FILTER (WHERE {covered}),
            count(*) FILTER (WHERE p.accounting_status = 'complete')
        FROM p WHERE p.position_status = 'closed' AND p.exit_at < %(lag_cutoff)s""",
        p,
    ).fetchone()
    readiness_join = conn.execute(
        f"""SELECT count(*) FILTER (WHERE p.entry_status = 'opened'),
            count(*) FILTER (WHERE p.position_status = 'closed')
        FROM {app}.momentum_flow_paper_probes p
        JOIN {ts}.momentum_flow_watch_evaluations_1m w ON w.watch_id = p.watch_id
        WHERE p.paper_version = %(hold)s AND w.decision_status = 'watch'
          AND w.decision_at >= %(start)s AND w.decision_at < %(end)s""",
        p,
    ).fetchone()
    complete_horizons = (
        f"(SELECT count(DISTINCT o.horizon_minutes) FROM {app}.momentum_flow_paper_outcomes o "
        "WHERE o.paper_id = p.paper_id AND o.horizon_minutes = ANY(%(horizons)s) "
        "AND o.status = 'complete')"
    )
    formal = conn.execute(
        f"""{probes} SELECT
            count(*) FILTER (WHERE p.entry_status = 'opened'),
            count(*) FILTER (WHERE p.position_status = 'closed'),
            count(*) FILTER (WHERE p.entry_status = 'opened'
                AND p.position_status IS DISTINCT FROM 'closed'),
            count(*) FILTER (WHERE p.position_status = 'closed'
                AND {complete_horizons} < {len(REQUIRED_HORIZONS)}),
            count(*) FILTER (WHERE p.position_status = 'closed'
                AND p.accounting_status IS DISTINCT FROM 'complete'),
            count(*) FILTER (WHERE p.position_status = 'closed' AND NOT ({covered}))
        FROM p""",
        p,
    ).fetchone()
    by = "coalesce(%s::text, 'NULL')"
    return {
        "window": [COHORT_START.isoformat(), window_end.isoformat()],
        "eligible_watches": {"count": int(eligible[0]), "ids_sha256": eligible[1]},
        "hold12h_probes": {"count": int(cohort[0]), "ids_sha256": cohort[1]},
        "probe_status": {
            name: _counts(
                conn,
                f"{probes} SELECT {by % ('p.' + name)}, count(*) FROM p GROUP BY 1",
                p,
            )
            for name in ("entry_status", "position_status", "accounting_status")
        },
        "outcomes": _counts(
            conn,
            f"""{probes} SELECT o.horizon_minutes || '/' || {by % "o.status"} || '/'
                || {by % "o.accounting_status"}, count(*)
            FROM {app}.momentum_flow_paper_outcomes o JOIN p ON p.paper_id = o.paper_id
            GROUP BY 1""",
            p,
        ),
        "health": {
            "eligible": int(eligible[0]),
            "baseline_unclaimed": int(health[0]),
            "baseline_stale": int(health[1]),
            "hold12h_unclaimed": int(health[2]),
            "hold12h_stale": int(health[3]),
            "closed_positions_past_lag": int(past_lag[0]),
            "funding_covered": int(past_lag[1]),
            "accounting_complete": int(past_lag[2]),
        },
        "readiness": {
            "entries_opened": int(readiness_join[0]),
            "positions_closed": int(readiness_join[1]),
        },
        "formal_readiness": {
            "opened": int(formal[0]),
            "closed": int(formal[1]),
            "opened_not_closed": int(formal[2]),
            "closed_missing_horizons": int(formal[3]),
            "closed_accounting_incomplete": int(formal[4]),
            "closed_funding_uncovered": int(formal[5]),
        },
        "tables": {
            name: int(conn.execute(f"SELECT count(*) FROM {app}.{name}").fetchone()[0])
            for name in (
                "momentum_universe_snapshots",
                "momentum_universe_instruments",
                "hold12h_funding_coverage_runs",
                "hold12h_funding_settlements",
            )
        },
        "funding_runs": _counts(
            conn,
            f"SELECT {by % 'status'}, count(*) FROM {app}.hold12h_funding_coverage_runs GROUP BY 1",
            p,
        ),
    }


def formal_ready(reference: Mapping[str, Any]) -> bool:
    """Every event that needs it is closed, has both horizons complete, complete
    accounting and covered funding. Rejected and unresolved entries need nothing."""
    f = reference["formal_readiness"]
    return bool(
        f["opened_not_closed"] == 0
        and f["closed_missing_horizons"] == 0
        and f["closed_accounting_incomplete"] == 0
        and f["closed_funding_uncovered"] == 0
    )


def reference_sha256(reference: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(reference, sort_keys=True).encode()).hexdigest()


# ---------- snapshot sets ----------


def _catalog_row_by_id(conn: Any, row_id: int) -> CatalogRow:
    r = conn.execute(
        "SELECT id, state, chunk_name, range_start, range_end, revision, row_count, file_name, "
        "file_bytes, file_sha256, content_fingerprint, borg_archive, manifest_sha256 "
        "FROM app.history_archive_datasets WHERE id = %s AND unit = 'chunk'",
        (row_id,),
    ).fetchone()
    if r is None:
        raise ArchiveError(f"pinned catalog row {row_id} is missing")
    return CatalogRow(
        int(r[0]),
        str(r[1]),
        str(r[2]),
        r[3].astimezone(UTC),
        r[4].astimezone(UTC),
        int(r[5]),
        int(r[6]),
        str(r[7]),
        int(r[8]),
        str(r[9]),
        str(r[10]),
        r[11],
        str(r[12]),
    )


def verified_prefix_end(catalog: Mapping[datetime, CatalogRow]) -> datetime | None:
    """The end of the contiguous run of verified watch ranges from the window start."""
    cursor = WATCH_WINDOW[0]
    reached = None
    for start in sorted(catalog):
        row = catalog[start]
        if row.state != "verified" or row.range_start != cursor:
            break
        cursor = reached = row.range_end
    return reached


def pin_watch_chunks(conn: Any, *, final: bool) -> tuple[list[dict[str, Any]], datetime]:
    """The verified watch revisions that tile the window (final) or its verified prefix
    (preliminary), each re-fingerprinted from the source in the caller's snapshot.
    Returns them with the coverage end. A changed chunk fails: re-export it first. A
    chunk already dropped by retention cannot be rechecked, so it fails too: a set must
    be taken while the source still holds what it pins."""
    catalog = live_rows(conn, WATCH_CONTRACT)
    start, end = WATCH_WINDOW
    if not final:
        prefix = verified_prefix_end(catalog)
        # A preliminary set must cover at least one decision day of the cohort.
        if prefix is None or prefix <= COHORT_START:
            raise ArchiveError("no verified watch chunk covers a cohort day yet")
        end = min(prefix, end)
    rows = covering_ranges(catalog, start, end)
    present = {c.range_start for c in list_chunks(conn, WATCH_CONTRACT)}
    pinned: list[dict[str, Any]] = []
    for row in rows:
        if row.range_start not in present:
            raise ArchiveError(f"watch chunk {row.chunk_name} is gone from the source")
        live = conn.execute(
            pg_fingerprint_sql(WATCH_CONTRACT), (row.range_start, row.range_end)
        ).fetchone()[0]
        if f"{FINGERPRINT_VERSION}:{live}" != row.content_fingerprint:
            raise ArchiveError(
                f"watch chunk {row.chunk_name} changed since export; re-export it and take "
                "the set again"
            )
        pinned.append(
            {
                "id": row.id,
                "range_start": row.range_start.isoformat(),
                "file_sha256": row.file_sha256,
                "content_fingerprint": row.content_fingerprint,
            }
        )
    return pinned, end


def decision_window_end(coverage_end: datetime) -> datetime:
    """Decisions counted by a set: up to the registered prefix end, or up to the pinned
    coverage for a preliminary set (a decision never precedes its bucket, so every
    decision before the coverage end lies in a pinned chunk)."""
    return min(DECISION_PREFIX_END, coverage_end)


def set_manifest_name(set_id: str) -> str:
    return f"{set_id}.set.manifest.json"


@dataclass
class SetReport:
    set_id: str
    snapshot_at: str
    kind: str
    coverage_end: str
    members: dict[str, int]
    pinned_chunks: int
    formal_ready: bool
    reference_sha256: str


def take_snapshot_set(
    dsn: str,
    out_dir: Path,
    *,
    code_revision: str,
    final: bool = True,
    reserve_bytes: int = DEFAULT_RESERVE_BYTES,
) -> SetReport:
    """One snapshot set: pin and recheck the watch revisions, record the composition
    reference and export every plain input, all in one REPEATABLE READ snapshot; then
    write the set's own manifest (the reference, the pinned revisions and every member)
    for the archive, and catalogue the set as `building` and its members as `exported`.
    A preliminary set pins the verified prefix of the window and counts decisions only
    up to it; a final set needs the whole window."""
    with archiver_session(dsn, WATCH_CONTRACT, out_dir) as writer:
        with _snapshot(dsn) as conn:
            snapshot_at = conn.execute("SELECT now()").fetchone()[0].astimezone(UTC)
            set_id = f"hyp015-{snapshot_at:%Y%m%dT%H%M%S}Z"
            pinned, coverage_end = pin_watch_chunks(conn, final=final)
            reference = composition(conn, Schemas(), decision_window_end(coverage_end))
            manifests = [
                export_snapshot_member(
                    conn,
                    contract,
                    out_dir,
                    set_id=set_id,
                    revision=1,
                    snapshot_at=snapshot_at,
                    code_revision=code_revision,
                    reserve_bytes=reserve_bytes,
                )
                for contract in PLAIN_CONTRACTS
            ]
        digest = reference_sha256(reference)
        kind = "final" if final else "preliminary"
        members = [
            {
                "dataset": m.dataset,
                "file_name": m.file_name,
                "file_sha256": m.file_sha256,
                "manifest_sha256": sha256_of(out_dir / snapshot_file_names(c, set_id, 1)[1]),
                "content_fingerprint": m.content_fingerprint,
                "row_count": m.row_count,
            }
            for c, m in zip(PLAIN_CONTRACTS, manifests, strict=True)
        ]
        set_manifest = {
            "set_id": set_id,
            "purpose": PURPOSE,
            "kind": kind,
            "snapshot_at": snapshot_at.isoformat(),
            "coverage_end": coverage_end.isoformat(),
            "code_revision": code_revision,
            "contract_version": CONTRACT_VERSION,
            "pinned_chunks": pinned,
            "reference": reference,
            "reference_sha256": digest,
            "members": members,
        }
        manifest_path = out_dir / set_manifest_name(set_id)
        manifest_path.write_text(json.dumps(set_manifest, indent=2, sort_keys=True) + "\n")
        manifest_sha = sha256_of(manifest_path)
        with writer.transaction():
            writer.execute(
                "INSERT INTO app.history_archive_snapshot_sets (set_id, purpose, "
                "required_datasets, snapshot_at, kind, coverage_end, code_revision, "
                "pinned_chunks, reference, reference_sha256, manifest_sha256, state) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'building')",
                (
                    set_id,
                    PURPOSE,
                    [c.dataset for c in PLAIN_CONTRACTS],
                    snapshot_at,
                    kind,
                    coverage_end,
                    code_revision,
                    json.dumps(pinned),
                    json.dumps(reference, sort_keys=True),
                    digest,
                    manifest_sha,
                ),
            )
            for contract, manifest in zip(PLAIN_CONTRACTS, manifests, strict=True):
                manifest_path = out_dir / snapshot_file_names(contract, set_id, 1)[1]
                record_export(writer, contract, manifest, sha256_of(manifest_path), 1)
    return SetReport(
        set_id=set_id,
        snapshot_at=snapshot_at.isoformat(),
        kind=kind,
        coverage_end=coverage_end.isoformat(),
        members={m.dataset: m.row_count for m in manifests},
        pinned_chunks=len(pinned),
        formal_ready=formal_ready(reference),
        reference_sha256=digest,
    )


def archive_set_manifests(
    dsn: str, out_dir: Path, *, repo: str, env: Mapping[str, str], now: datetime
) -> list[str]:
    """Put the manifests of building sets not yet offsite into one new archive, accept
    it only if its listing is exact, and record the archive on each set."""
    with archiver_session(dsn, WATCH_CONTRACT, out_dir) as conn:
        pending = conn.execute(
            "SELECT set_id, manifest_sha256 FROM app.history_archive_snapshot_sets "
            "WHERE purpose = %s AND state = 'building' AND manifest_archive IS NULL",
            (PURPOSE,),
        ).fetchall()
        members = []
        for set_id, sha in pending:
            path = out_dir / set_manifest_name(str(set_id))
            if not path.exists() or sha256_of(path) != sha:
                raise ArchiveError(f"set manifest of {set_id} is missing or changed")
            members.append((str(set_id), path.name))
        if not members:
            return []
        archive = f"{ARCHIVE_PREFIX}sets-{now:%Y-%m-%dT%H:%M:%S}"
        names = [name for _, name in members]
        _run(
            ["borg", "create", "--compression", "zstd,3", f"{repo}::{archive}", *names],
            env,
            cwd=out_dir,
        )
        listed = parse_short_list(_run(borg_list_members_args(repo, archive), env))
        if listed != frozenset(names):
            raise ArchiveError(f"{archive} lists {sorted(listed)}, expected {sorted(names)}")
        for set_id, _ in members:
            conn.execute(
                "UPDATE app.history_archive_snapshot_sets SET manifest_archive = %s "
                "WHERE set_id = %s AND manifest_archive IS NULL",
                (archive, set_id),
            )
        return [set_id for set_id, _ in members]


def archive_inputs(
    dsn: str, out_dir: Path, *, repo: str, env: Mapping[str, str], now: datetime
) -> StepReport:
    """The archive step: every exported input, then the manifests of new sets (in an
    archive of their own, one second later so the names differ)."""
    report = run_archive(dsn, ALL_CONTRACTS, out_dir, repo=repo, env=env, now=now)
    if not report.failed:
        later = now + timedelta(seconds=1)
        for set_id in archive_set_manifests(dsn, out_dir, repo=repo, env=env, now=later):
            report.done.append(f"set manifest {set_id} archived")
    return report


def _extracted_set_manifest(
    conn: Any, set_id: str, work_dir: Path, *, repo: str, env: Mapping[str, str]
) -> dict[str, Any]:
    """The set's manifest extracted from its archive, checked against the recorded
    SHA-256."""
    row = conn.execute(
        "SELECT manifest_archive, manifest_sha256 FROM app.history_archive_snapshot_sets "
        "WHERE set_id = %s",
        (set_id,),
    ).fetchone()
    if row is None or row[0] is None:
        raise ArchiveError(f"snapshot set {set_id} has no archived manifest")
    work_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=work_dir) as tmp:
        path = Path(tmp) / set_manifest_name(set_id)
        sha = stream_member(repo, str(row[0]), path.name, path, dict(env), MAX_MANIFEST_BYTES)
        if sha != row[1]:
            raise ArchiveError(f"archived manifest of {set_id}: sha256 {sha} != {row[1]}")
        payload: dict[str, Any] = json.loads(path.read_text())
    return payload


def verify_set(
    dsn: str,
    set_id: str,
    *,
    now: datetime,
    repo: str,
    env: Mapping[str, str],
    work_dir: Path,
) -> list[str]:
    """Mark a set verified once its own manifest is extracted from its archive and
    matches (the database also refuses unless every member is verified), then retire
    older verified sets of the same purpose in its favour. Returns retired ids."""
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        _extracted_set_manifest(conn, set_id, work_dir, repo=repo, env=env)
    with psycopg.connect(dsn, autocommit=True) as conn, conn.transaction():
        updated = conn.execute(
            "UPDATE app.history_archive_snapshot_sets SET state = 'verified', verified_at = %s "
            "WHERE set_id = %s AND state = 'building'",
            (now, set_id),
        ).rowcount
        if updated != 1:
            raise ArchiveError(f"snapshot set {set_id} is not building")
        retired = conn.execute(
            "UPDATE app.history_archive_snapshot_sets SET state = 'superseded', "
            "superseded_by = %s WHERE purpose = %s AND state = 'verified' AND set_id <> %s "
            "AND snapshot_at < (SELECT snapshot_at FROM app.history_archive_snapshot_sets "
            "WHERE set_id = %s) RETURNING set_id",
            (set_id, PURPOSE, set_id, set_id),
        ).fetchall()
    return [str(r[0]) for r in retired]


# ---------- restore check ----------


@dataclass
class RestoreReport:
    set_id: str
    restored: dict[str, int] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)
    readiness: dict[str, Any] = field(default_factory=dict)
    health: dict[str, Any] = field(default_factory=dict)
    formal_ready: bool = False

    @property
    def ok(self) -> bool:
        return not self.failures

    def to_json(self) -> str:
        return json.dumps({**asdict(self), "ok": self.ok}, indent=1, default=str) + "\n"


def _load_set(conn: Any, set_id: str) -> tuple[list[dict[str, Any]], dict[str, Any], str, datetime]:
    row = conn.execute(
        "SELECT pinned_chunks, reference, state, coverage_end "
        "FROM app.history_archive_snapshot_sets WHERE set_id = %s AND purpose = %s",
        (set_id, PURPOSE),
    ).fetchone()
    if row is None:
        raise ArchiveError(f"snapshot set {set_id} does not exist")
    return list(row[0]), dict(row[1]), str(row[2]), row[3].astimezone(UTC)


def restore_check(
    dsn: str,
    target_dsn: str,
    set_id: str,
    work_dir: Path,
    *,
    repo: str,
    env: Mapping[str, str],
    reserve_bytes: int = DEFAULT_RESERVE_BYTES,
) -> RestoreReport:
    """Restore a verified set into `target_dsn` (a throwaway database) and prove the
    registered reader can run on it, without computing a verdict.

    Only the two `RESTORED` schemas in the target are created and dropped. Each archived
    file is extracted from its own archive and checked (SHA-256, content, manifest)
    before it is loaded."""
    import duckdb
    import psycopg

    report = RestoreReport(set_id=set_id)
    work_dir.mkdir(parents=True, exist_ok=True)
    with psycopg.connect(dsn, autocommit=True) as catalog:
        pinned, reference, state, coverage_end = _load_set(catalog, set_id)
        if state != "verified":
            raise ArchiveError(f"snapshot set {set_id} is {state}, not verified")
        set_manifest = _extracted_set_manifest(catalog, set_id, work_dir, repo=repo, env=env)
        watch_rows = [_catalog_row_by_id(catalog, int(p["id"])) for p in pinned]
        members = {
            c.dataset: [r for r in snapshot_rows(catalog, c) if r.snapshot_set == set_id]
            for c in PLAIN_CONTRACTS
        }
    window_end = decision_window_end(coverage_end)
    denominator = denominator_duck(window_end)
    report.failures += _manifest_disagreements(set_manifest, pinned, reference, members)
    with psycopg.connect(target_dsn, autocommit=True) as target:
        for schema in (RESTORED.timeseries, RESTORED.app):
            target.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            target.execute(f"CREATE SCHEMA {schema}")
        watch_table = f"{RESTORED.timeseries}.{WATCH_CONTRACT.source_table}"
        hashes: list[str] = []
        for index, row in enumerate(watch_rows):
            path = work_dir / row.file_name
            try:
                verify_extracted(
                    WATCH_CONTRACT,
                    row,
                    work_dir,
                    repo=repo,
                    env=env,
                    reserve_bytes=reserve_bytes,
                    keep=path,
                )
                restore_into(
                    target,
                    WATCH_CONTRACT,
                    path,
                    watch_table,
                    where=denominator,
                    create=index == 0,
                )
                source = "read_parquet('" + str(path).replace("'", "''") + "')"
                hashes += [
                    str(h[0])
                    for h in duckdb.connect()
                    .execute(
                        f"SELECT sha256({duck_row_text(WATCH_CONTRACT)}) FROM {source} "
                        f"WHERE {denominator}"
                    )
                    .fetchall()
                ]
            except (ArchiveError, FetchError) as exc:
                report.failures.append(f"watch {row.chunk_name}: {exc}")
            finally:
                path.unlink(missing_ok=True)
        if not watch_rows:
            report.failures.append("the set pins no watch chunk")
        report.restored[WATCH_CONTRACT.dataset] = len(hashes)
        expected_watch = hashlib.sha256("".join(sorted(hashes)).encode()).hexdigest()
        got_watch = _scalar(
            target.execute(pg_fingerprint_where_sql(WATCH_CONTRACT, "TRUE", table=watch_table))
        )
        if got_watch != expected_watch:
            report.failures.append("restored WATCH denominator differs from the archive")
        for contract in PLAIN_CONTRACTS:
            rows = members[contract.dataset]
            if len(rows) != 1 or rows[0].state != "verified":
                report.failures.append(f"{contract.dataset}: no verified member in the set")
                continue
            member = rows[0]
            path = work_dir / member.file_name
            table = f"{RESTORED.app}.{contract.source_table}"
            try:
                verify_extracted(
                    contract,
                    member,
                    work_dir,
                    repo=repo,
                    env=env,
                    reserve_bytes=reserve_bytes,
                    keep=path,
                )
                report.restored[contract.dataset] = restore_into(target, contract, path, table)
            except (ArchiveError, FetchError) as exc:
                report.failures.append(f"{contract.dataset}: {exc}")
                continue
            finally:
                path.unlink(missing_ok=True)
            got = _scalar(target.execute(pg_fingerprint_where_sql(contract, "TRUE", table=table)))
            if f"{FINGERPRINT_VERSION}:{got}" != member.content_fingerprint:
                report.failures.append(f"{contract.dataset}: restored content differs")
        if report.failures:
            return report
        restored_reference = composition(target, RESTORED, window_end)
        if restored_reference != reference:
            differ = sorted(k for k in reference if restored_reference.get(k) != reference[k])
            report.failures.append(f"composition differs from the reference on {differ}")
    _check_reader(report, target_dsn, reference, window_end)
    report.formal_ready = formal_ready(reference)
    return report


def _manifest_disagreements(
    manifest: Mapping[str, Any],
    pinned: Sequence[Mapping[str, Any]],
    reference: Mapping[str, Any],
    members: Mapping[str, Sequence[Any]],
) -> list[str]:
    """The archived set manifest must say what the catalog says: the same pinned watch
    revisions, the same reference and the same member files."""
    wrong = []
    if manifest.get("pinned_chunks") != list(pinned):
        wrong.append("pinned watch revisions")
    if manifest.get("reference") != dict(reference):
        wrong.append("composition reference")
    listed = {m["dataset"]: (m["file_sha256"], m["manifest_sha256"]) for m in manifest["members"]}
    catalogued = {
        name: (rows[0].file_sha256, rows[0].manifest_sha256)
        for name, rows in members.items()
        if len(rows) == 1
    }
    if listed != catalogued:
        wrong.append("member files")
    return [f"archived set manifest disagrees with the catalog on {w}" for w in wrong]


def _check_reader(
    report: RestoreReport,
    target_dsn: str,
    reference: Mapping[str, Any],
    window_end: datetime,
) -> None:
    """The registered reader's own readiness and health paths, on the restored copy."""
    readiness = asyncio.run(
        load_readiness(
            target_dsn,
            cohort_start=COHORT_START,
            decision_prefix_end=window_end,
            schemas=RESTORED,
        )
    )
    health = asyncio.run(
        load_health_checkpoint(
            target_dsn,
            since=COHORT_START,
            until=window_end,
            funding_version=ACTUAL_FUNDING_VERSION,
            schemas=RESTORED,
        )
    )
    report.readiness = asdict(readiness)
    report.health = asdict(health)
    expected = {
        "total_watches": reference["eligible_watches"]["count"],
        "entries_opened": reference["readiness"]["entries_opened"],
        "positions_closed": reference["readiness"]["positions_closed"],
    }
    for key, value in expected.items():
        if report.readiness[key] != value:
            report.failures.append(f"readiness {key} {report.readiness[key]} != {value}")
    names = {"eligible": "eligible_watches"}
    for key, value in reference["health"].items():
        got = report.health[names.get(key, key)]
        if got != value:
            report.failures.append(f"health {key} {got} != {value}")


def _scalar(cursor: Any) -> Any:
    row = cursor.fetchone()
    if row is None:
        raise ArchiveError("a single-value query returned no row")
    return row[0]


# ---------- CLI ----------


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="step", required=True)
    for name in ("export", "archive", "verify", "snapshot-set", "verify-set", "restore-check"):
        step = sub.add_parser(name)
        step.add_argument("--out-dir", type=Path, required=True)
        step.add_argument("--reserve-bytes", type=int, default=DEFAULT_RESERVE_BYTES)
        if name in ("archive", "verify", "verify-set", "restore-check"):
            step.add_argument("--backup-env", type=Path, required=True)
        if name in ("export", "snapshot-set"):
            step.add_argument("--code-revision", required=True)
        if name in ("verify-set", "restore-check"):
            step.add_argument("--set-id", required=True)
    sub.choices["export"].add_argument("--max-chunks", type=int, default=7)
    sub.choices["snapshot-set"].add_argument(
        "--preliminary",
        action="store_true",
        help="pin the verified prefix of the window (an early restore check)",
    )
    sub.choices["restore-check"].add_argument("--target-dsn-env", default="RESTORE_DATABASE_URL")
    args = parser.parse_args(argv)
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        raise SystemExit("DATABASE_URL is required")
    now = datetime.now(UTC)
    if args.step == "export":
        result: Any = run_export(
            dsn,
            WATCH_CONTRACT,
            args.out_dir,
            code_revision=args.code_revision,
            now=now,
            max_chunks=args.max_chunks,
            reserve_bytes=args.reserve_bytes,
        )
        sys.stdout.write(result.to_json())
        return 1 if result.failed else 0
    if args.step == "snapshot-set":
        made = take_snapshot_set(
            dsn,
            args.out_dir,
            code_revision=args.code_revision,
            final=not args.preliminary,
            reserve_bytes=args.reserve_bytes,
        )
        sys.stdout.write(json.dumps(asdict(made), indent=1) + "\n")
        return 0
    repo, env = borg_env(args.backup_env)
    if args.step == "verify-set":
        retired = verify_set(dsn, args.set_id, now=now, repo=repo, env=env, work_dir=args.out_dir)
        sys.stdout.write(json.dumps({"verified": args.set_id, "retired": retired}) + "\n")
        return 0
    if args.step == "restore-check":
        target = os.getenv(args.target_dsn_env)
        if not target:
            raise SystemExit(f"{args.target_dsn_env} is required")
        checked = restore_check(
            dsn,
            target,
            args.set_id,
            args.out_dir,
            repo=repo,
            env=env,
            reserve_bytes=args.reserve_bytes,
        )
        sys.stdout.write(checked.to_json())
        return 0 if checked.ok else 1
    if args.step == "archive":
        result = archive_inputs(dsn, args.out_dir, repo=repo, env=env, now=now)
    else:
        result = run_verify(
            dsn,
            ALL_CONTRACTS,
            args.out_dir,
            repo=repo,
            env=env,
            now=now,
            reserve_bytes=args.reserve_bytes,
        )
    sys.stdout.write(result.to_json())
    return 1 if result.failed else 0


if __name__ == "__main__":
    sys.exit(main())
