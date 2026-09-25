"""HYP-015 hold12h verdict reader -- the SQL/CLI layer.

Maps real Postgres rows into the pure reader dataclasses
(``momentum_flow_hold12h_verdict_report``) and runs the pure pipeline. This module is
the ONLY place that touches the database; all methodology lives in the pure layer.

DRAFT / NOT FROZEN. Two physically separate paths:

* READINESS (the default): ``load_readiness`` selects COUNTS ONLY -- decision times,
  statuses, identity -- and NEVER a single return/fee/PnL column, so the pre-freeze
  sizing accrual cannot see outcomes. This is what runs before registration.
* FORMAL (``--formal-run``): ``load_cohort`` reads returns and runs the verdict, but is
  a fail-closed PREREQUISITE gate -- it refuses unless the contract carries a literal
  ``cohort_start_iso`` AND a registered actual-funding source exists (none does yet: the
  prospective per-instrument capture is a separate PR), so it does not run today.

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
                                count(*) FILTER (WHERE EXISTS (
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
                                      AND c.requested_until > p.entry_at)) AS covered,
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


def claim_formal_output(output_dir: Path) -> None:
    """Exclusive local artifact directory, so a run never overwrites another's artifact.
    Not the one-read guarantee: that is the durable database claim below."""
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    try:
        output_dir.mkdir()
    except FileExistsError as exc:
        raise SystemExit(
            f"formal-run refused: {output_dir} exists; the single formal read was already claimed"
        ) from exc


async def claim_formal_read(
    db_url: str,
    contract: Hold12hVerdictContract,
    *,
    cohort_start: datetime,
    decision_prefix_end: datetime,
    code_revision: str,
    working_tree_dirty: bool,
    output_dir: Path,
    schemas: Schemas = _DEFAULT_SCHEMAS,
) -> None:
    """The durable one-read claim, committed BEFORE any return is read. Unique per cohort
    (contract version + both frozen bounds) rather than per contract sha, so neither another
    output directory nor an edited contract can read the same cohort twice."""
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.ext.asyncio import create_async_engine

    from .outcome_repository import async_database_url

    engine = create_async_engine(async_database_url(db_url), pool_pre_ping=True, pool_size=1)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    f"""
                    INSERT INTO {schemas.app}.hold12h_formal_read_claims
                        (contract_version, contract_sha256, cohort_start, decision_prefix_end,
                         code_revision, working_tree_dirty, output_dir)
                    VALUES (:cv, :sha, :start, :end, :rev, :dirty, :out)
                    """
                ),
                {
                    "cv": contract.contract_version,
                    "sha": contract.sha256_hex(),
                    "start": cohort_start,
                    "end": decision_prefix_end,
                    "rev": code_revision[:64],
                    "dirty": working_tree_dirty,
                    "out": str(output_dir),
                },
            )
    except IntegrityError as exc:
        raise SystemExit(
            "formal-run refused: this cohort's single formal read was already claimed"
        ) from exc
    finally:
        await engine.dispose()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="HYP-015 hold12h verdict reader (DRAFT)")
    parser.add_argument(
        "--formal-run",
        action="store_true",
        help="run the verdict; a fail-closed prerequisite gate (see below)",
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
    parser.add_argument("--code-revision", default=os.getenv("SCHURFER_GIT_SHA", "unknown"))
    parser.add_argument("--working-tree-dirty", action=argparse.BooleanOptionalAction, default=True)
    return parser


async def _run(args: argparse.Namespace) -> str:
    contract = HOLD12H_VERDICT_CONTRACT
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise ValueError("DATABASE_URL is required for the hold12h verdict reader")
    decision_prefix_end = datetime.fromisoformat(args.decision_prefix_end).astimezone(UTC)

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
        now=datetime.now(UTC),
    )
    output_dir: Path = args.output_dir
    if output_dir.exists():
        raise SystemExit(f"formal-run refused: {output_dir} already exists")
    await claim_formal_read(
        db_url,
        contract,
        cohort_start=cohort_start,
        decision_prefix_end=prefix_end,
        code_revision=args.code_revision,
        working_tree_dirty=args.working_tree_dirty,
        output_dir=output_dir,
    )
    claim_formal_output(output_dir)

    from .momentum_flow_hold12h_funding import load_stored_funding

    watches, probes = await load_cohort(
        db_url, cohort_start=cohort_start, decision_prefix_end=prefix_end
    )
    watches = filter_to_cohort(watches, cohort_start=cohort_start, decision_prefix_end=prefix_end)
    routes = sorted(
        {probe.route for probe in probes.values()},
        key=lambda route: (route.exchange, route.market_type, route.market_id),
    )
    funding = await load_stored_funding(
        db_url, routes, source_version=contract.actual_funding_version
    )
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
    report_path = output_dir / "hold12h_verdict.json"
    report_path.write_text(text)
    digest = hashlib.sha256(text.encode()).hexdigest()
    (output_dir / "hold12h_verdict.sha256").write_text(f"sha256:{digest}\n")
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
    """Test-only helper: run the FULL formal pipeline with an injected funding source
    (the CLI itself fail-closes because no funding source is registered)."""
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
