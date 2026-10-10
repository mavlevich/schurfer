"""HYP-015 hold12h verdict reader -- the SQL/CLI layer.

Maps real Postgres rows into the pure reader dataclasses
(``momentum_flow_hold12h_verdict_report``) and runs the pure pipeline. This module is
the ONLY place that touches the database; all methodology lives in the pure layer.

REGISTERED 2026-09-27 (#442): the cohort bounds and the actual-funding version
(``hold12h_actual_funding_v2``, captured prospectively into
``app.hold12h_funding_settlements``) are frozen in the contract. Separate paths:

* READINESS (the default): ``load_readiness`` selects COUNTS ONLY -- decision times,
  statuses, identity -- and NEVER a single return/fee/PnL column.
* HEALTH (``--health-since``): the outcome-blind operational checkpoint.
* PREFLIGHT (``--preflight``): no database at all. Checks that the read is registered
  and open now, and prints the frozen window and the digest of this installation's
  sources (``source_digest``), so the production command can refuse a stale image before
  anything is written.
* REPUBLISH (``--republish``): finishes a read that stopped after its claim was
  completed but before ``hold12h_verdict.json`` was published, from the attempt file the
  claim names (``republish_formal_result``).
* FORMAL (``--formal-run``): the single read. Every refusal (not registered, another
  prefix, too early, cohort incomplete) happens before the durable claim; the claim is
  committed before any return is read; the verdict is computed from the snapshot pinned
  in the claim and published once (see ``publish_formal_result``).

Mapping choices (matched to the real schema, per review):
  * a resolved outcome is ``status = 'complete'``; a filled entry is
    ``entry_status = 'opened'``; a resolved position is ``position_status = 'closed'``;
  * the denominator uses the SAME filters the hold12h paper worker claims on
    (watch_version / source_exchange / market_type, watch_id & episode_id NOT NULL), so
    other cohorts (e.g. Binance) never dilute it;
  * ex-funding return = ``gross_return_pct - fees_usd / notional * 100`` (percent);
  * canonical asset = the point-in-time ``identity_key`` (``identity_status = 'ready'``)
    from the SINGLE latest universe snapshot at-or-before the decision, matched on the
    exact route (exchange + native_market_id); unresolved -> None; NEVER a bare ticker.
"""

from __future__ import annotations

# ruff: noqa: S608 -- the only interpolations into SQL are schema names, validated as
# plain identifiers by Schemas.__post_init__; every VALUE is a bound parameter.
import argparse
import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import momentum_flow_hold12h_verdict as verdict_module
from .momentum_flow_hold12h_snapshot import (
    holdings_window,
    inputs_from_snapshot,
    publish_once,
    publish_once_or_same,
    snapshot_bytes,
    snapshot_digest,
)
from .momentum_flow_hold12h_verdict import (
    HOLD12H_VERDICT_CONTRACT,
    Hold12hVerdictContract,
    decide_verdict,
)
from .momentum_flow_hold12h_verdict_report import (
    HORIZON_240,
    HORIZON_720,
    CohortEvaluation,
    HorizonOutcome,
    InstrumentRoute,
    PortfolioResult,
    ProbeClass,
    ProbeRecord,
    WatchDecision,
    cohort_rows_digest,
    evaluate_cohort,
    filter_to_cohort,
    formal_cohort_start,
    formal_decision_prefix_end,
    verdict_fingerprint,
)
from .momentum_flow_paper_contract import FROZEN_PAPER_CONTRACT, HOLD12H_PAPER_CONTRACT

if TYPE_CHECKING:
    from collections.abc import Sequence

_HORIZONS = (HORIZON_240, HORIZON_720)
_RESOLVED_OUTCOME = "complete"
_FILLED_ENTRY = "opened"
_CLOSED_POSITION = "closed"
_READY_IDENTITY = "ready"
_IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]*$")


@dataclass(frozen=True)
class Schemas:
    """Schema names, parameterized ONLY so tests can seed an isolated schema instead of
    touching the shared production tables. Validated as plain identifiers (they are
    interpolated into SQL, never bound)."""

    timeseries: str = "timeseries"
    app: str = "app"

    def __post_init__(self) -> None:
        for name in (self.timeseries, self.app):
            if not _IDENT_RE.match(name):
                raise ValueError(f"invalid schema identifier: {name!r}")


_DEFAULT_SCHEMAS = Schemas()


def _utc(value: datetime | None) -> datetime | None:
    return None if value is None else value.astimezone(UTC)


def _ex_funding_net_pct(gross_return_pct: Any, fees_usd: Any, notional: Any) -> float | None:
    """Percent return net of FEES only (funding is applied later from the ACTUAL source):
    ``gross_return_pct - fees_usd / notional * 100``. None when any input is missing or the
    notional is non-positive (the pure layer then classifies it)."""
    if gross_return_pct is None or fees_usd is None or notional is None:
        return None
    notional_f = float(notional)
    if notional_f <= 0:
        return None
    net: float = float(gross_return_pct) - float(fees_usd) / notional_f * 100.0
    return net


def _watch_filter_sql(ts: str) -> str:
    """The denominator filter -- identical to the hold12h paper worker's WATCH claim."""
    return f"""
        FROM {ts}.momentum_flow_watch_evaluations_1m w
        WHERE w.decision_status = 'watch'
          AND w.watch_version = :wv AND w.exchange = :ex AND w.market_type = :mt
          AND w.watch_id IS NOT NULL AND w.episode_id IS NOT NULL
          AND w.decision_at >= :start AND w.decision_at < :end
    """


def _watch_params(cohort_start: datetime, decision_prefix_end: datetime) -> dict[str, Any]:
    return {
        "wv": HOLD12H_PAPER_CONTRACT.watch_version,
        "ex": HOLD12H_PAPER_CONTRACT.source_exchange,
        "mt": HOLD12H_PAPER_CONTRACT.market_type,
        "start": cohort_start,
        "end": decision_prefix_end,
    }


async def _resolve_identity(
    conn: Any, app: str, exchange: str, native_id: str, as_of: datetime
) -> str | None:
    """Point-in-time identity: the SINGLE latest snapshot at-or-before ``as_of`` FIRST,
    then the instrument within THAT snapshot only. An instrument dropped or no longer
    ``ready`` in the newest snapshot resolves to None -- never a fallback to an older one."""
    from sqlalchemy import text

    result = (
        await conn.execute(
            text(
                f"""
                WITH snap AS (
                    SELECT universe_version, catalog_version
                    FROM {app}.momentum_universe_snapshots
                    WHERE exchange = :ex AND captured_at <= :as_of
                    ORDER BY captured_at DESC LIMIT 1
                )
                SELECT i.identity_key
                FROM {app}.momentum_universe_instruments i JOIN snap
                  ON i.universe_version = snap.universe_version
                 AND i.catalog_version = snap.catalog_version
                WHERE i.exchange = :ex AND i.native_market_id = :native_id
                  AND i.identity_status = :ready
                LIMIT 1
                """
            ),
            {"ex": exchange, "as_of": as_of, "native_id": native_id, "ready": _READY_IDENTITY},
        )
    ).first()
    return None if result is None else str(result[0])


@dataclass(frozen=True)
class ReadinessSummary:
    """Outcome-blind sizing counts. NO returns are read to produce this."""

    total_watches: int
    identity_resolved: int
    entries_opened: int
    positions_closed: int
    distinct_assets: int
    distinct_utc_weeks: int


async def load_readiness(
    db_url: str,
    *,
    cohort_start: datetime,
    decision_prefix_end: datetime,
    schemas: Schemas = _DEFAULT_SCHEMAS,
) -> ReadinessSummary:
    """Outcome-blind: selects decision times, statuses and identity only -- never a
    return, fee or PnL column -- for the pre-freeze sizing accrual."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from .outcome_repository import async_database_url

    engine = create_async_engine(async_database_url(db_url), pool_pre_ping=True, pool_size=1)
    try:
        async with engine.connect() as conn:
            watch_rows = (
                (
                    await conn.execute(
                        text(
                            "SELECT w.watch_id, w.decision_at, w.exchange, w.symbol"
                            + _watch_filter_sql(schemas.timeseries)
                        ),
                        _watch_params(cohort_start, decision_prefix_end),
                    )
                )
                .mappings()
                .all()
            )
            probe_rows = (
                (
                    await conn.execute(
                        text(
                            f"""
                            SELECT p.watch_id, p.market_id, p.entry_status, p.position_status
                            FROM {schemas.app}.momentum_flow_paper_probes p
                            JOIN {schemas.timeseries}.momentum_flow_watch_evaluations_1m w
                              ON w.watch_id = p.watch_id
                            WHERE p.paper_version = :pv
                              AND w.decision_status = 'watch'
                              AND w.decision_at >= :start AND w.decision_at < :end
                            """
                        ),
                        {
                            "pv": HOLD12H_PAPER_CONTRACT.paper_version,
                            "start": cohort_start,
                            "end": decision_prefix_end,
                        },
                    )
                )
                .mappings()
                .all()
            )
            market_id_by_watch = {str(p["watch_id"]): str(p["market_id"] or "") for p in probe_rows}
            assets: set[str] = set()
            resolved = 0
            for w in watch_rows:
                native = market_id_by_watch.get(str(w["watch_id"])) or str(w["symbol"])
                identity = await _resolve_identity(
                    conn, schemas.app, str(w["exchange"]), native, w["decision_at"]
                )
                if identity is not None:
                    resolved += 1
                    assets.add(identity)
    finally:
        await engine.dispose()

    weeks = {(_utc(w["decision_at"]) or cohort_start).isocalendar()[:2] for w in watch_rows}
    return ReadinessSummary(
        total_watches=len(watch_rows),
        identity_resolved=resolved,
        entries_opened=sum(1 for p in probe_rows if p["entry_status"] == _FILLED_ENTRY),
        positions_closed=sum(1 for p in probe_rows if p["position_status"] == _CLOSED_POSITION),
        distinct_assets=len(assets),
        distinct_utc_weeks=len(weeks),
    )


# Operational health rule, registered before the cohort (colleague review): over every
# eligible WATCH, the hold12h lost-entry fraction (stale or never claimed) may exceed the
# baseline worker's by at most 2 percentage points, and never exceed 5% absolute. Checked
# outcome-blind on a fixed schedule; a breach is logged, never a reason to change
# thresholds or restart the cohort.
HEALTH_MAX_STALE_EXCESS = 0.02
HEALTH_MAX_STALE_FRACTION = 0.05
# Funding for a closed position is only expected once the settlement lag has passed.
HEALTH_FUNDING_LAG = timedelta(hours=8)


@dataclass(frozen=True)
class HealthCheckpoint:
    """Outcome-blind operational counts for one window over EVERY eligible WATCH (the
    verdict denominator), per worker. A WATCH a worker never claimed counts as lost, so a
    stopped worker cannot drop out of the check. NO return/fee/PnL column is read."""

    since: datetime
    until: datetime
    eligible_watches: int
    baseline_unclaimed: int
    baseline_stale: int
    hold12h_unclaimed: int
    hold12h_stale: int
    hold12h_claim_p50_seconds: float | None
    hold12h_claim_p90_seconds: float | None
    closed_positions_past_lag: int
    funding_covered: int
    accounting_complete: int

    def _fraction(self, count: int) -> float:
        return count / self.eligible_watches if self.eligible_watches else 0.0

    @property
    def baseline_lost_fraction(self) -> float:
        return self._fraction(self.baseline_unclaimed + self.baseline_stale)

    @property
    def hold12h_lost_fraction(self) -> float:
        return self._fraction(self.hold12h_unclaimed + self.hold12h_stale)

    @property
    def funding_covered_fraction(self) -> float:
        if not self.closed_positions_past_lag:
            return 0.0
        return self.funding_covered / self.closed_positions_past_lag


def health_breaches(checkpoint: HealthCheckpoint) -> list[str]:
    """The registered rule on LOST entries (stale or never claimed); an empty list means
    healthy. A window with no eligible WATCH rows is itself a breach."""
    if checkpoint.eligible_watches == 0:
        return ["no eligible WATCH rows in the window"]
    breaches: list[str] = []
    excess = checkpoint.hold12h_lost_fraction - checkpoint.baseline_lost_fraction
    if excess > HEALTH_MAX_STALE_EXCESS:
        breaches.append(f"hold12h lost entries exceed baseline by {excess:.4f}")
    if checkpoint.hold12h_lost_fraction > HEALTH_MAX_STALE_FRACTION:
        breaches.append(f"hold12h lost-entry fraction {checkpoint.hold12h_lost_fraction:.4f}")
    return breaches


def _funding_covered_sql(app: str) -> str:
    """A closed probe's funding is covered: a `complete` run of the registered version
    spans its whole holding interval and no overlapping run is an integrity conflict."""
    return f"""EXISTS (
            SELECT 1 FROM {app}.hold12h_funding_coverage_runs r
            WHERE r.exchange = p.exchange
              AND r.native_market_id = p.market_id
              AND r.source_version = :fv AND r.status = 'complete'
              AND r.requested_since <= p.entry_at
              AND r.requested_until >= p.exit_at)
          AND NOT EXISTS (
            SELECT 1 FROM {app}.hold12h_funding_coverage_runs c
            WHERE c.exchange = p.exchange
              AND c.native_market_id = p.market_id
              AND c.source_version = :fv
              AND c.status = 'integrity_conflict'
              AND c.requested_since < p.exit_at
              AND c.requested_until > p.entry_at)"""


async def load_health_checkpoint(
    db_url: str,
    *,
    since: datetime,
    until: datetime,
    funding_version: str,
    schemas: Schemas = _DEFAULT_SCHEMAS,
) -> HealthCheckpoint:
    """Outcome-blind: statuses, timestamps and coverage runs only."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from .outcome_repository import async_database_url

    ts, app = schemas.timeseries, schemas.app
    params = {
        **_watch_params(since, until),
        "base": FROZEN_PAPER_CONTRACT.paper_version,
        "hold": HOLD12H_PAPER_CONTRACT.paper_version,
        "fv": funding_version,
        "lag_cutoff": until - HEALTH_FUNDING_LAG,
    }
    engine = create_async_engine(async_database_url(db_url), pool_pre_ping=True, pool_size=1)
    try:
        async with engine.connect() as conn:
            entries = (
                (
                    await conn.execute(
                        text(
                            f"""
                            WITH w AS (SELECT w.watch_id {_watch_filter_sql(ts)}),
                            per AS (
                                SELECT w.watch_id,
                                    coalesce(bool_or(p.paper_version = :base), false) AS b_seen,
                                    coalesce(bool_or(p.paper_version = :base
                                        AND p.entry_status = 'rejected_stale'), false) AS b_stale,
                                    coalesce(bool_or(p.paper_version = :hold), false) AS h_seen,
                                    coalesce(bool_or(p.paper_version = :hold
                                        AND p.entry_status = 'rejected_stale'), false) AS h_stale
                                FROM w
                                LEFT JOIN {app}.momentum_flow_paper_probes p
                                  ON p.watch_id = w.watch_id
                                 AND p.paper_version IN (:base, :hold)
                                GROUP BY w.watch_id
                            )
                            SELECT count(*) AS eligible,
                                count(*) FILTER (WHERE NOT b_seen) AS baseline_unclaimed,
                                count(*) FILTER (WHERE b_stale) AS baseline_stale,
                                count(*) FILTER (WHERE NOT h_seen) AS hold12h_unclaimed,
                                count(*) FILTER (WHERE h_stale) AS hold12h_stale
                            FROM per
                            """
                        ),
                        params,
                    )
                )
                .mappings()
                .one()
            )
            latency = (
                (
                    await conn.execute(
                        text(
                            f"""
                            WITH w AS (SELECT w.watch_id {_watch_filter_sql(ts)})
                            SELECT
                                percentile_cont(0.5) WITHIN GROUP (ORDER BY extract(
                                    epoch FROM p.claimed_at - p.watch_decision_at)) AS p50,
                                percentile_cont(0.9) WITHIN GROUP (ORDER BY extract(
                                    epoch FROM p.claimed_at - p.watch_decision_at)) AS p90
                            FROM {app}.momentum_flow_paper_probes p
                            JOIN w ON w.watch_id = p.watch_id
                            WHERE p.paper_version = :hold
                            """
                        ),
                        params,
                    )
                )
                .mappings()
                .one()
            )
            funding = (
                (
                    await conn.execute(
                        text(
                            f"""
                            WITH w AS (SELECT w.watch_id {_watch_filter_sql(ts)})
                            SELECT count(*) AS closed,
                                count(*) FILTER (WHERE {_funding_covered_sql(app)}) AS covered,
                                count(*) FILTER (
                                    WHERE p.accounting_status = 'complete') AS accounting_complete
                            FROM {app}.momentum_flow_paper_probes p
                            JOIN w ON w.watch_id = p.watch_id
                            WHERE p.paper_version = :hold AND p.position_status = 'closed'
                              AND p.exit_at < :lag_cutoff
                            """
                        ),
                        params,
                    )
                )
                .mappings()
                .one()
            )
    finally:
        await engine.dispose()

    def _seconds(value: Any) -> float | None:
        return None if value is None else float(value)

    return HealthCheckpoint(
        since=since,
        until=until,
        eligible_watches=int(entries["eligible"] or 0),
        baseline_unclaimed=int(entries["baseline_unclaimed"] or 0),
        baseline_stale=int(entries["baseline_stale"] or 0),
        hold12h_unclaimed=int(entries["hold12h_unclaimed"] or 0),
        hold12h_stale=int(entries["hold12h_stale"] or 0),
        hold12h_claim_p50_seconds=_seconds(latency["p50"]),
        hold12h_claim_p90_seconds=_seconds(latency["p90"]),
        closed_positions_past_lag=int(funding["closed"] or 0),
        funding_covered=int(funding["covered"] or 0),
        accounting_complete=int(funding["accounting_complete"] or 0),
    )


async def load_cohort(
    db_url: str,
    *,
    cohort_start: datetime,
    decision_prefix_end: datetime,
    schemas: Schemas = _DEFAULT_SCHEMAS,
) -> tuple[tuple[WatchDecision, ...], dict[str, ProbeRecord]]:
    """FORMAL path: reads returns. Only call under a registered formal run. Maps the WATCH
    denominator and hold12h probes for the half-open window into the pure dataclasses."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from .outcome_repository import async_database_url

    ts, app = schemas.timeseries, schemas.app
    engine = create_async_engine(async_database_url(db_url), pool_pre_ping=True, pool_size=1)
    try:
        async with engine.connect() as conn:
            watch_rows = (
                (
                    await conn.execute(
                        text(
                            "SELECT w.watch_id, w.decision_at, w.exchange, w.market_type, w.symbol"
                            + _watch_filter_sql(ts)
                        ),
                        _watch_params(cohort_start, decision_prefix_end),
                    )
                )
                .mappings()
                .all()
            )
            probe_rows = (
                (
                    await conn.execute(
                        text(
                            f"""
                            SELECT p.paper_id, p.watch_id, p.exchange, p.market_type, p.symbol,
                                   p.unified_symbol, p.market_id, p.entry_status,
                                   p.position_status, p.exit_reason,
                                   p.entry_at, p.exit_at, p.gross_return_pct, p.fees_usd,
                                   p.max_adverse_return_pct, p.entry_filled_notional_usd
                            FROM {app}.momentum_flow_paper_probes p
                            JOIN {ts}.momentum_flow_watch_evaluations_1m w
                              ON w.watch_id = p.watch_id
                            WHERE p.paper_version = :pv
                              AND w.decision_status = 'watch'
                              AND w.decision_at >= :start AND w.decision_at < :end
                            """
                        ),
                        {
                            "pv": HOLD12H_PAPER_CONTRACT.paper_version,
                            "start": cohort_start,
                            "end": decision_prefix_end,
                        },
                    )
                )
                .mappings()
                .all()
            )
            paper_ids = [r["paper_id"] for r in probe_rows]
            outcome_rows: Sequence[Any] = []
            if paper_ids:
                outcome_rows = (
                    (
                        await conn.execute(
                            text(
                                f"""
                                SELECT o.paper_id, o.horizon_minutes, o.status,
                                       o.quote_observed_at, o.gross_return_pct, o.fees_usd,
                                       o.filled_notional_usd
                                FROM {app}.momentum_flow_paper_outcomes o
                                WHERE o.paper_id = ANY(:ids) AND o.horizon_minutes = ANY(:hz)
                                """
                            ),
                            {"ids": paper_ids, "hz": list(_HORIZONS)},
                        )
                    )
                    .mappings()
                    .all()
                )
            identities: dict[str, str | None] = {}
            market_id_by_watch = {str(p["watch_id"]): str(p["market_id"] or "") for p in probe_rows}
            for w in watch_rows:
                wid = str(w["watch_id"])
                native = market_id_by_watch.get(wid) or str(w["symbol"])
                identities[wid] = await _resolve_identity(
                    conn, app, str(w["exchange"]), native, w["decision_at"]
                )
    finally:
        await engine.dispose()

    outcomes_by_paper: dict[Any, dict[int, HorizonOutcome]] = {}
    for row in outcome_rows:
        outcomes_by_paper.setdefault(row["paper_id"], {})[int(row["horizon_minutes"])] = (
            HorizonOutcome(
                horizon_minutes=int(row["horizon_minutes"]),
                resolved=row["status"] == _RESOLVED_OUTCOME,
                observed_at=_utc(row["quote_observed_at"]),
                gross_return_pct=_ex_funding_net_pct(
                    row["gross_return_pct"], row["fees_usd"], row["filled_notional_usd"]
                ),
                notional_usd=None
                if row["filled_notional_usd"] is None
                else float(row["filled_notional_usd"]),
            )
        )

    probes: dict[str, ProbeRecord] = {}
    for row in probe_rows:
        wid = str(row["watch_id"])
        probes[wid] = ProbeRecord(
            watch_id=wid,
            route=InstrumentRoute(
                exchange=str(row["exchange"]),
                market_type=str(row["market_type"]),
                market_id=str(row["market_id"] or ""),
                unified_symbol=str(row["unified_symbol"] or ""),
            ),
            entry_at=_utc(row["entry_at"]) or cohort_start,
            entry_ok=row["entry_status"] == _FILLED_ENTRY,
            exit_at=_utc(row["exit_at"]),
            exit_resolved=row["position_status"] == _CLOSED_POSITION,
            exit_reason=row["exit_reason"],
            actual_gross_return_pct=_ex_funding_net_pct(
                row["gross_return_pct"], row["fees_usd"], row["entry_filled_notional_usd"]
            ),
            actual_notional_usd=None
            if row["entry_filled_notional_usd"] is None
            else float(row["entry_filled_notional_usd"]),
            max_adverse_return_pct=None
            if row["max_adverse_return_pct"] is None
            else float(row["max_adverse_return_pct"]),
            horizons=outcomes_by_paper.get(row["paper_id"], {}),
        )

    watches = tuple(
        WatchDecision(
            watch_id=str(r["watch_id"]),
            canonical_asset=identities.get(str(r["watch_id"])),
            decision_at=_utc(r["decision_at"]) or cohort_start,
        )
        for r in watch_rows
    )
    return watches, probes


def _formal_artifact(
    contract: Hold12hVerdictContract,
    evaluation: CohortEvaluation,
    *,
    cohort_start: datetime,
    decision_prefix_end: datetime,
    fingerprint: str,
    code_revision: str,
    working_tree_dirty: bool,
) -> dict[str, Any]:
    result = decide_verdict(contract, evaluation.inputs)
    window_hours = (decision_prefix_end - cohort_start).total_seconds() / 3600.0
    return {
        "mode": "formal",
        "contract_sha256": contract.sha256_hex(),
        "cohort_start": cohort_start.isoformat(),
        "decision_prefix_end": decision_prefix_end.isoformat(),
        "code_revision": code_revision,
        "working_tree_dirty": working_tree_dirty,
        "fingerprint": fingerprint,
        "funnel": {cls.value: evaluation.funnel[cls] for cls in ProbeClass},
        "analyzable_pairs": evaluation.inputs.analyzable_pairs,
        "portfolio": {
            "720m": portfolio_summary(
                evaluation.portfolio_720,
                max_slots=contract.max_concurrent_slots,
                window_hours=window_hours,
            ),
            "240m": portfolio_summary(
                evaluation.portfolio_240,
                max_slots=contract.max_concurrent_slots,
                window_hours=window_hours,
            ),
        },
        "verdict": result.outcome.value,
        "gate": result.gate,
        "reason": result.reason,
    }


def portfolio_summary(
    result: PortfolioResult, *, max_slots: int, window_hours: float
) -> dict[str, Any]:
    """Capital-time view of one policy: displacement (``skipped_slots_full``) and slot
    occupancy explain the dollar difference between the two holds on a fixed bank."""
    capacity_hours = max_slots * window_hours
    return {
        "window_pnl_usd": result.window_pnl_usd,
        "taken": result.taken,
        "skipped_slots_full": result.skipped_slots_full,
        "incomplete_taken": result.incomplete_taken,
        "incomplete_taken_fraction": result.incomplete_taken_fraction,
        "slot_hours": result.slot_hours,
        "occupancy_fraction": result.slot_hours / capacity_hours if capacity_hours > 0 else 0.0,
        "longest_losing_streak": result.longest_losing_streak,
        "adverse_from_entry_usd_diagnostic": result.adverse_from_entry_usd,
        "window_pnl_zero_funding_sensitivity_usd": (result.window_pnl_zero_funding_sensitivity_usd),
    }


def formal_read_window(
    contract: Hold12hVerdictContract,
    *,
    registered: bool,
    requested_prefix_end: datetime,
    now: datetime,
) -> tuple[datetime, datetime]:
    """Refuse any formal read that is not THE single pre-declared one: the contract must be
    registered with both frozen bounds, the requested prefix must equal the frozen one,
    and the read must wait until the last positions and their funding can be complete."""
    if not registered:
        raise SystemExit("formal-run refused: the verdict contract is not registered")
    cohort_start = formal_cohort_start(contract)
    prefix_end = formal_decision_prefix_end(contract)
    if cohort_start is None or prefix_end is None:
        raise SystemExit("formal-run refused: cohort bounds are not frozen in the contract")
    if requested_prefix_end != prefix_end:
        raise SystemExit(
            f"formal-run refused: prefix {requested_prefix_end.isoformat()} is not the frozen "
            f"{prefix_end.isoformat()}"
        )
    earliest = prefix_end + timedelta(hours=contract.min_read_delay_hours)
    if now < earliest:
        raise SystemExit(f"formal-run refused: too early, the read opens at {earliest.isoformat()}")
    return cohort_start, prefix_end


CLAIM_LEASE = timedelta(minutes=60)


@dataclass(frozen=True)
class FormalCoverage:
    """Outcome-blind readiness of the whole cohort for the single read: statuses and
    coverage runs only, NO return column."""

    filled: int
    open_positions: int
    closed: int
    funding_covered: int
    accounting_complete: int

    def shortfalls(self) -> list[str]:
        missing = []
        if self.open_positions:
            missing.append(f"{self.open_positions} filled positions are not closed")
        if self.funding_covered < self.closed:
            missing.append(f"funding covered for {self.funding_covered}/{self.closed} closed")
        if self.accounting_complete < self.closed:
            missing.append(
                f"accounting complete for {self.accounting_complete}/{self.closed} closed"
            )
        return missing


async def load_cohort_watch_ids(
    db_url: str,
    *,
    cohort_start: datetime,
    decision_prefix_end: datetime,
    schemas: Schemas = _DEFAULT_SCHEMAS,
) -> list[str]:
    """The cohort's WATCH denominator, ordered, with no return read: the claim pins it."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from .outcome_repository import async_database_url

    engine = create_async_engine(async_database_url(db_url), pool_pre_ping=True, pool_size=1)
    try:
        async with engine.connect() as conn:
            rows = await conn.execute(
                text(
                    "SELECT w.watch_id"
                    + _watch_filter_sql(schemas.timeseries)
                    + " ORDER BY w.decision_at, w.watch_id"
                ),
                _watch_params(cohort_start, decision_prefix_end),
            )
            return [str(r[0]) for r in rows]
    finally:
        await engine.dispose()


async def load_formal_coverage(
    db_url: str,
    *,
    cohort_start: datetime,
    decision_prefix_end: datetime,
    funding_version: str,
    schemas: Schemas = _DEFAULT_SCHEMAS,
) -> FormalCoverage:
    """Checked before the claim, so a read is never claimed on data still in flight."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from .outcome_repository import async_database_url

    ts, app = schemas.timeseries, schemas.app
    engine = create_async_engine(async_database_url(db_url), pool_pre_ping=True, pool_size=1)
    try:
        async with engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        text(
                            f"""
                            WITH w AS (SELECT w.watch_id {_watch_filter_sql(ts)})
                            SELECT
                                count(*) FILTER (WHERE p.entry_status = :filled) AS filled,
                                count(*) FILTER (WHERE p.entry_status = :filled
                                    AND p.position_status IS DISTINCT FROM :closed) AS open,
                                count(*) FILTER (WHERE p.position_status = :closed) AS closed,
                                count(*) FILTER (WHERE p.position_status = :closed
                                    AND {_funding_covered_sql(app)}) AS covered,
                                count(*) FILTER (WHERE p.position_status = :closed
                                    AND p.accounting_status = 'complete') AS accounting
                            FROM {app}.momentum_flow_paper_probes p
                            JOIN w ON w.watch_id = p.watch_id
                            WHERE p.paper_version = :hold
                            """
                        ),
                        {
                            **_watch_params(cohort_start, decision_prefix_end),
                            "hold": HOLD12H_PAPER_CONTRACT.paper_version,
                            "fv": funding_version,
                            "filled": _FILLED_ENTRY,
                            "closed": _CLOSED_POSITION,
                        },
                    )
                )
                .mappings()
                .one()
            )
    finally:
        await engine.dispose()
    return FormalCoverage(
        filled=int(row["filled"] or 0),
        open_positions=int(row["open"] or 0),
        closed=int(row["closed"] or 0),
        funding_covered=int(row["covered"] or 0),
        accounting_complete=int(row["accounting"] or 0),
    )


@dataclass(frozen=True)
class FormalClaim:
    id: int
    owner: str
    watch_ids: tuple[str, ...]
    inputs_digest: str | None
    resumed: bool


def _watch_ids_sha256(watch_ids: list[str]) -> str:
    return hashlib.sha256(json.dumps(watch_ids, separators=(",", ":")).encode()).hexdigest()


async def open_formal_claim(
    db_url: str,
    contract: Hold12hVerdictContract,
    *,
    cohort_start: datetime,
    decision_prefix_end: datetime,
    watch_ids: list[str],
    coverage: FormalCoverage,
    accepted_incomplete_coverage: bool,
    code_revision: str,
    working_tree_dirty: bool,
    output_dir: Path,
    schemas: Schemas = _DEFAULT_SCHEMAS,
) -> FormalClaim:
    """The durable one-read claim, committed BEFORE any return is read. Unique per cohort
    (contract version + both frozen bounds) rather than per contract sha, so neither another
    output directory nor an edited contract can read the same cohort twice.

    A new claim is inserted with this run as the lease owner. An existing OPEN claim is
    taken over only after its lease expired and only on the same contract sha and the same
    ordered WATCH ids; a completed claim, a held lease or any mismatch refuses."""
    import uuid

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from .outcome_repository import async_database_url

    owner = uuid.uuid4().hex
    table = f"{schemas.app}.hold12h_formal_read_claims"
    key = {
        "cv": contract.contract_version,
        "start": cohort_start,
        "end": decision_prefix_end,
    }
    pins = {"sha": contract.sha256_hex(), "wsha": _watch_ids_sha256(watch_ids)}
    lease = {"owner": owner, "lease": CLAIM_LEASE.total_seconds()}
    returning = "RETURNING id, watch_ids, inputs_digest"
    engine = create_async_engine(async_database_url(db_url), pool_pre_ping=True, pool_size=1)
    try:
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    text(
                        f"""
                        INSERT INTO {table}
                            (contract_version, contract_sha256, cohort_start,
                             decision_prefix_end, code_revision, working_tree_dirty, output_dir,
                             watch_ids, watch_ids_sha256, coverage_closed,
                             coverage_funding_covered, coverage_accounting_complete,
                             coverage_open_positions, accepted_incomplete_coverage,
                             lease_owner, lease_expires_at)
                        VALUES (:cv, :sha, :start, :end, :rev, :dirty, :out,
                                CAST(:wids AS jsonb), :wsha, :closed, :covered, :accounting,
                                :open, :accepted, :owner,
                                now() + make_interval(secs => :lease))
                        ON CONFLICT ON CONSTRAINT uq_hold12h_formal_read_claim_cohort
                        DO NOTHING
                        {returning}
                        """
                    ),
                    {
                        **key,
                        **pins,
                        **lease,
                        "rev": code_revision[:64],
                        "dirty": working_tree_dirty,
                        "out": str(output_dir),
                        "wids": json.dumps(watch_ids),
                        "closed": coverage.closed,
                        "covered": coverage.funding_covered,
                        "accounting": coverage.accounting_complete,
                        "open": coverage.open_positions,
                        "accepted": accepted_incomplete_coverage,
                    },
                )
            ).first()
            resumed = False
            if row is None:
                resumed = True
                row = (
                    await conn.execute(
                        text(
                            f"""
                            UPDATE {table}
                            SET lease_owner = :owner,
                                lease_expires_at = now() + make_interval(secs => :lease)
                            WHERE contract_version = :cv AND cohort_start = :start
                              AND decision_prefix_end = :end AND status = 'claimed'
                              AND lease_expires_at < now()
                              AND contract_sha256 = :sha AND watch_ids_sha256 = :wsha
                            {returning}
                            """
                        ),
                        {**key, **pins, **lease},
                    )
                ).first()
            if row is None:
                existing = (
                    await conn.execute(
                        text(
                            f"""
                            SELECT status, lease_expires_at < now() AS expired,
                                   contract_sha256 = :sha AS same_sha,
                                   watch_ids_sha256 = :wsha AS same_watches
                            FROM {table}
                            WHERE contract_version = :cv AND cohort_start = :start
                              AND decision_prefix_end = :end
                            """
                        ),
                        {**key, **pins},
                    )
                ).one()
    finally:
        await engine.dispose()
    if row is None:
        if existing.status == "completed":
            raise SystemExit(
                "formal-run refused: this cohort's single formal read was already claimed "
                "and completed (if hold12h_verdict.json or its .sha256 is missing, the run "
                "stopped after completing the claim: finish it with --republish)"
            )
        if not existing.same_sha:
            raise SystemExit("formal-run refused: the open claim pins another contract sha")
        if not existing.same_watches:
            raise SystemExit(
                "formal-run refused: the cohort's WATCH set changed since the open claim"
            )
        raise SystemExit(
            "formal-run refused: another run holds the open claim's lease; it resumes only "
            f"after that run finishes or its lease ({CLAIM_LEASE}) expires"
        )
    stored = row[1] if isinstance(row[1], list) else json.loads(row[1])
    return FormalClaim(
        id=int(row[0]),
        owner=owner,
        watch_ids=tuple(str(w) for w in stored),
        inputs_digest=row[2],
        resumed=resumed,
    )


async def _owned_update(
    db_url: str, claim: FormalClaim, sql_set: str, params: dict[str, Any], schemas: Schemas
) -> bool:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from .outcome_repository import async_database_url

    engine = create_async_engine(async_database_url(db_url), pool_pre_ping=True, pool_size=1)
    try:
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    text(
                        f"""
                        UPDATE {schemas.app}.hold12h_formal_read_claims SET {sql_set}
                        WHERE id = :id AND lease_owner = :owner AND status = 'claimed'
                          AND lease_expires_at > now()
                        RETURNING id
                        """
                    ),
                    {**params, "id": claim.id, "owner": claim.owner},
                )
            ).first()
    finally:
        await engine.dispose()
    return row is not None


async def pin_inputs_digest(
    db_url: str, claim: FormalClaim, digest: str, *, schemas: Schemas = _DEFAULT_SCHEMAS
) -> None:
    """Pin the loaded inputs on the first attempt; a resumed attempt must load the same."""
    if claim.inputs_digest is not None and claim.inputs_digest != digest:
        raise SystemExit(
            "formal-run refused: the cohort's inputs changed since the claim was pinned; "
            "the claim is kept open and nothing is published"
        )
    if not await _owned_update(
        db_url,
        claim,
        "inputs_digest = :digest",
        {"digest": digest},
        schemas,
    ):
        raise SystemExit("formal-run refused: this run no longer owns the claim's lease")


async def complete_formal_claim(
    db_url: str,
    claim: FormalClaim,
    fingerprint: str,
    *,
    artifact_name: str,
    artifact_sha256: str,
    schemas: Schemas = _DEFAULT_SCHEMAS,
) -> None:
    """Only the current lease owner completes the claim, naming its durable artifact."""
    if not await _owned_update(
        db_url,
        claim,
        "status = 'completed', completed_at = now(), result_fingerprint = :fp, "
        "artifact_name = :artifact, artifact_sha256 = :artifact_sha",
        {"fp": fingerprint, "artifact": artifact_name, "artifact_sha": artifact_sha256},
        schemas,
    ):
        raise SystemExit("formal-run refused: this run no longer owns the claim's lease")


async def load_snapshot_from_db(
    db_url: str,
    contract: Hold12hVerdictContract,
    claim: FormalClaim,
    *,
    cohort_start: datetime,
    decision_prefix_end: datetime,
) -> bytes:
    """Load the cohort and its funding and serialize them into the input snapshot. The
    funding part keeps only settlements and runs overlapping the cohort's holdings, so
    later captures of the same instruments do not change it."""
    from .momentum_flow_hold12h_funding import load_stored_funding

    watches, probes = await load_cohort(
        db_url, cohort_start=cohort_start, decision_prefix_end=decision_prefix_end
    )
    if sorted(w.watch_id for w in watches) != sorted(claim.watch_ids):
        raise SystemExit(
            "formal-run refused: the loaded WATCH set differs from the claim's pinned ids"
        )
    routes = sorted(
        {probe.route for probe in probes.values()},
        key=lambda route: (route.exchange, route.market_type, route.market_id),
    )
    funding = await load_stored_funding(
        db_url, routes, source_version=contract.actual_funding_version
    )
    return snapshot_bytes(watches, probes, funding, window=holdings_window(probes))


async def pinned_inputs(
    db_url: str,
    contract: Hold12hVerdictContract,
    claim: FormalClaim,
    output_dir: Path,
    *,
    cohort_start: datetime,
    decision_prefix_end: datetime,
    schemas: Schemas = _DEFAULT_SCHEMAS,
) -> bytes:
    """The snapshot the verdict is computed from, pinned in the claim BEFORE any verdict.
    A resumed attempt reads the pinned snapshot file; only if that file is gone does it
    reload, and then it must produce the same digest or refuse."""
    if claim.inputs_digest is not None:
        stored = output_dir / f"inputs.{claim.inputs_digest}.json"
        if stored.exists():
            body = stored.read_bytes()
            if snapshot_digest(body) != claim.inputs_digest:
                raise SystemExit(f"formal-run refused: {stored} does not match its digest")
            return body
    body = await load_snapshot_from_db(
        db_url,
        contract,
        claim,
        cohort_start=cohort_start,
        decision_prefix_end=decision_prefix_end,
    )
    digest = snapshot_digest(body)
    if claim.inputs_digest is not None and digest != claim.inputs_digest:
        raise SystemExit(
            "formal-run refused: the cohort's inputs changed since the claim was pinned; "
            "the claim is kept open and nothing is published"
        )
    publish_once_or_same(output_dir / f"inputs.{digest}.json", body)
    await pin_inputs_digest(db_url, claim, digest, schemas=schemas)
    return body


async def publish_formal_result(
    db_url: str,
    claim: FormalClaim,
    output_dir: Path,
    body: bytes,
    fingerprint: str,
    *,
    schemas: Schemas = _DEFAULT_SCHEMAS,
) -> Path:
    """Every attempt writes its OWN immutable, fsynced artifact first; the claim is then
    completed only by the lease owner, naming that artifact; only after that does the
    winner publish the result under the stable name. A stale owner that lost its lease
    cannot complete and so never publishes or overwrites anything."""
    sha = hashlib.sha256(body).hexdigest()
    attempt = output_dir / f"hold12h_verdict.attempt-{claim.owner}.json"
    publish_once(attempt, body)
    publish_once(attempt.with_name(attempt.name + ".sha256"), f"sha256:{sha}\n".encode())
    await complete_formal_claim(
        db_url, claim, fingerprint, artifact_name=attempt.name, artifact_sha256=sha, schemas=schemas
    )
    final = output_dir / "hold12h_verdict.json"
    publish_once_or_same(final, body)
    publish_once_or_same(output_dir / "hold12h_verdict.sha256", f"sha256:{sha}\n".encode())
    return final


async def republish_formal_result(
    db_url: str,
    contract: Hold12hVerdictContract,
    output_dir: Path,
    *,
    cohort_start: datetime,
    decision_prefix_end: datetime,
    schemas: Schemas = _DEFAULT_SCHEMAS,
) -> bytes:
    """Finish a read that stopped after its claim was completed but before the result was
    published under its stable name. It reads only the claim row and the attempt file the
    claim names, checks that file against the claim's SHA-256 and its own sidecar, and
    publishes those exact bytes. Nothing is recomputed, no cohort row is read and the
    claim is not written; an existing published file with other bytes is an incident."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from .outcome_repository import async_database_url

    engine = create_async_engine(async_database_url(db_url), pool_pre_ping=True, pool_size=1)
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        f"""
                        SELECT status, output_dir, artifact_name, artifact_sha256
                        FROM {schemas.app}.hold12h_formal_read_claims
                        WHERE contract_version = :cv AND cohort_start = :start
                          AND decision_prefix_end = :end
                        """
                    ),
                    {
                        "cv": contract.contract_version,
                        "start": cohort_start,
                        "end": decision_prefix_end,
                    },
                )
            ).first()
    finally:
        await engine.dispose()
    if row is None:
        raise SystemExit("republish refused: this cohort has no formal-read claim")
    status, claimed_dir, artifact_name, artifact_sha256 = row
    if status != "completed":
        raise SystemExit(
            "republish refused: the claim is not completed; rerun --formal-run after its lease"
        )
    if str(output_dir) != claimed_dir:
        raise SystemExit(f"republish refused: the claim's output directory is {claimed_dir}")
    attempt = output_dir / str(artifact_name)
    sidecar = attempt.with_name(attempt.name + ".sha256")
    if not attempt.is_file() or not sidecar.is_file():
        raise SystemExit(f"republish refused: {attempt.name} or its .sha256 is missing")
    body = attempt.read_bytes()
    sha = hashlib.sha256(body).hexdigest()
    if sha != artifact_sha256 or sidecar.read_text() != f"sha256:{sha}\n":
        raise SystemExit(
            f"republish refused: {attempt.name} does not match the claim's sha256; "
            "this is an integrity incident"
        )
    try:
        publish_once_or_same(output_dir / "hold12h_verdict.json", body)
        publish_once_or_same(output_dir / "hold12h_verdict.sha256", f"sha256:{sha}\n".encode())
    except FileExistsError as exc:
        raise SystemExit(
            f"republish refused: {exc.filename} exists with other bytes than the claimed "
            "artifact; this is an integrity incident"
        ) from exc
    return body


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="HYP-015 hold12h verdict reader")
    parser.add_argument(
        "--formal-run",
        action="store_true",
        help="the single registered read; refuses before the claim unless it is open now",
    )
    parser.add_argument(
        "--preflight",
        action="store_true",
        help="no database: refuse unless the read is open now, print the window and digest",
    )
    parser.add_argument(
        "--republish",
        action="store_true",
        help=(
            "finish a read stopped after its claim completed: publish the attempt file the "
            "claim names, checked by sha256; nothing is recomputed"
        ),
    )
    parser.add_argument(
        "--decision-prefix-end",
        required=True,
        help="UTC ISO upper bound (exclusive) of the decision window",
    )
    parser.add_argument(
        "--health-since",
        default=None,
        help="outcome-blind health checkpoint: UTC ISO lower bound; the prefix end is the upper",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="formal only: artifact directory, claimed exclusively before returns are read",
    )
    parser.add_argument(
        "--accept-incomplete-coverage",
        action="store_true",
        help=(
            "formal run only: claim even though some positions are open or lack funding or "
            "accounting (recorded in the claim); by default such a read is refused"
        ),
    )
    parser.add_argument("--code-revision", default=os.getenv("SCHURFER_GIT_SHA", "unknown"))
    parser.add_argument("--working-tree-dirty", action=argparse.BooleanOptionalAction, default=True)
    return parser


def _now() -> datetime:
    """The clock the read window is checked against. A module function only so the
    rehearsal test can stand at the registered read time; no flag or setting moves it."""
    return datetime.now(UTC)


def _preflight(contract: Hold12hVerdictContract, decision_prefix_end: datetime) -> str:
    from .source_digest import source_digest

    cohort_start, prefix_end = formal_read_window(
        contract,
        registered=verdict_module.REGISTERED,
        requested_prefix_end=decision_prefix_end,
        now=_now(),
    )
    return json.dumps(
        {
            "mode": "preflight",
            "contract_sha256": contract.sha256_hex(),
            "cohort_start": cohort_start.isoformat(),
            "decision_prefix_end": prefix_end.isoformat(),
            "read_opens_at": (
                prefix_end + timedelta(hours=contract.min_read_delay_hours)
            ).isoformat(),
            "source_digest": source_digest(Path(__file__).resolve().parent),
        },
        indent=2,
        sort_keys=True,
    )


async def _run(args: argparse.Namespace) -> str:
    contract = HOLD12H_VERDICT_CONTRACT
    decision_prefix_end = datetime.fromisoformat(args.decision_prefix_end).astimezone(UTC)
    if args.preflight:
        if args.formal_run or args.republish or args.health_since is not None:
            raise SystemExit("--preflight runs alone")
        return _preflight(contract, decision_prefix_end)
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise ValueError("DATABASE_URL is required for the hold12h verdict reader")
    if args.republish:
        if args.formal_run or args.health_since is not None:
            raise SystemExit("--republish runs alone")
        if args.output_dir is None:
            raise SystemExit("republish refused: --output-dir is required")
        cohort_start, prefix_end = formal_read_window(
            contract,
            registered=verdict_module.REGISTERED,
            requested_prefix_end=decision_prefix_end,
            now=_now(),
        )
        body = await republish_formal_result(
            db_url,
            contract,
            args.output_dir,
            cohort_start=cohort_start,
            decision_prefix_end=prefix_end,
        )
        return body.decode().rstrip("\n")

    if args.health_since is not None:
        if args.formal_run:
            raise SystemExit("--health-since is outcome-blind and cannot be combined")
        checkpoint = await load_health_checkpoint(
            db_url,
            since=datetime.fromisoformat(args.health_since).astimezone(UTC),
            until=decision_prefix_end,
            funding_version=contract.actual_funding_version,
        )
        breaches = health_breaches(checkpoint)
        return json.dumps(
            {
                "mode": "health_checkpoint",
                **{
                    key: value.isoformat() if isinstance(value, datetime) else value
                    for key, value in checkpoint.__dict__.items()
                },
                "baseline_lost_fraction": checkpoint.baseline_lost_fraction,
                "hold12h_lost_fraction": checkpoint.hold12h_lost_fraction,
                "funding_covered_fraction": checkpoint.funding_covered_fraction,
                "healthy": not breaches,
                "breaches": breaches,
            },
            indent=2,
            sort_keys=True,
        )

    if not args.formal_run:
        # Outcome-blind readiness -- no returns read. Before registration the window opens
        # at the epoch (readiness spans all accrued operational probes).
        readiness_start = formal_cohort_start(contract) or datetime(1970, 1, 1, tzinfo=UTC)
        summary = await load_readiness(
            db_url, cohort_start=readiness_start, decision_prefix_end=decision_prefix_end
        )
        return json.dumps({"mode": "readiness", **summary.__dict__}, indent=2, sort_keys=True)

    # FORMAL -- every refusal happens before a return is read.
    if args.output_dir is None:
        raise SystemExit("formal-run refused: --output-dir is required")
    cohort_start, prefix_end = formal_read_window(
        contract,
        registered=verdict_module.REGISTERED,
        requested_prefix_end=decision_prefix_end,
        now=_now(),
    )
    output_dir: Path = args.output_dir
    # Outcome-blind, before the claim: the pinned denominator and the coverage it is read on.
    watch_ids = await load_cohort_watch_ids(
        db_url, cohort_start=cohort_start, decision_prefix_end=prefix_end
    )
    coverage = await load_formal_coverage(
        db_url,
        cohort_start=cohort_start,
        decision_prefix_end=prefix_end,
        funding_version=contract.actual_funding_version,
    )
    shortfalls = coverage.shortfalls()
    if shortfalls and not args.accept_incomplete_coverage:
        raise SystemExit(
            "formal-run refused before the claim: cohort not complete yet ("
            + "; ".join(shortfalls)
            + "). Retry later, or pass --accept-incomplete-coverage to read it as it is."
        )
    claim = await open_formal_claim(
        db_url,
        contract,
        cohort_start=cohort_start,
        decision_prefix_end=prefix_end,
        watch_ids=watch_ids,
        coverage=coverage,
        accepted_incomplete_coverage=bool(shortfalls),
        code_revision=args.code_revision,
        working_tree_dirty=args.working_tree_dirty,
        output_dir=output_dir,
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    body = await pinned_inputs(
        db_url,
        contract,
        claim,
        output_dir,
        cohort_start=cohort_start,
        decision_prefix_end=prefix_end,
    )
    # From here on the verdict sees only the pinned snapshot, never live rows.
    watches, probes, funding = inputs_from_snapshot(body)
    watches = filter_to_cohort(watches, cohort_start=cohort_start, decision_prefix_end=prefix_end)
    evaluation = evaluate_cohort(contract, watches, probes, funding)
    fingerprint = verdict_fingerprint(
        contract_sha256=contract.sha256_hex(),
        cohort_start=cohort_start,
        decision_prefix_end=prefix_end,
        code_revision=args.code_revision,
        working_tree_dirty=args.working_tree_dirty,
        funding_source_id=f"StoredFundingSource:{contract.actual_funding_version}",
        data_versions={"paper_contract": contract.paper_contract_sha256},
        rows_digest=cohort_rows_digest(watches, probes, funding),
        funnel=evaluation.funnel,
        inputs=evaluation.inputs,
    )
    artifact = _formal_artifact(
        contract,
        evaluation,
        cohort_start=cohort_start,
        decision_prefix_end=prefix_end,
        fingerprint=fingerprint,
        code_revision=args.code_revision,
        working_tree_dirty=args.working_tree_dirty,
    )
    text = json.dumps(artifact, indent=2, sort_keys=True, allow_nan=False) + "\n"
    await publish_formal_result(db_url, claim, output_dir, text.encode(), fingerprint)
    return text


async def run_formal_for_test(
    contract: Hold12hVerdictContract,
    db_url: str,
    *,
    cohort_start: datetime,
    decision_prefix_end: datetime,
    funding: Any,
    schemas: Schemas = _DEFAULT_SCHEMAS,
    code_revision: str = "test",
    working_tree_dirty: bool = False,
) -> tuple[dict[str, Any], CohortEvaluation]:
    """Test-only helper: the formal pipeline with an injected funding source and no
    claim, for unit tests of the mapping. The CLI path (``--formal-run``) is rehearsed
    end to end in ``test_hyp015_formal_read_rehearsal_integration``."""
    watches, probes = await load_cohort(
        db_url, cohort_start=cohort_start, decision_prefix_end=decision_prefix_end, schemas=schemas
    )
    watches = filter_to_cohort(
        watches, cohort_start=cohort_start, decision_prefix_end=decision_prefix_end
    )
    evaluation = evaluate_cohort(contract, watches, probes, funding)
    fingerprint = verdict_fingerprint(
        contract_sha256=contract.sha256_hex(),
        cohort_start=cohort_start,
        decision_prefix_end=decision_prefix_end,
        code_revision=code_revision,
        working_tree_dirty=working_tree_dirty,
        funding_source_id=type(funding).__name__,
        data_versions={"paper_contract": contract.paper_contract_sha256},
        rows_digest=cohort_rows_digest(watches, probes, funding),
        funnel=evaluation.funnel,
        inputs=evaluation.inputs,
    )
    artifact = _formal_artifact(
        contract,
        evaluation,
        cohort_start=cohort_start,
        decision_prefix_end=decision_prefix_end,
        fingerprint=fingerprint,
        code_revision=code_revision,
        working_tree_dirty=working_tree_dirty,
    )
    return artifact, evaluation


def main() -> None:
    import asyncio
    import sys

    args = build_parser().parse_args()
    sys.stdout.write(asyncio.run(_run(args)) + "\n")


if __name__ == "__main__":
    main()
