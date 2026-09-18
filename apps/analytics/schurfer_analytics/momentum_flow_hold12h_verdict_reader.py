"""HYP-015 hold12h verdict reader -- the SQL/CLI layer.

Maps real Postgres rows into the pure reader dataclasses
(``momentum_flow_hold12h_verdict_report``) and runs the pure pipeline. This module is
the ONLY place that touches the database; all methodology lives in the pure layer.

DRAFT / NOT FROZEN. A ``--formal-run`` FAIL-CLOSES: with no registered actual-funding
source it uses ``NoRegisteredFundingSource`` (every probe accounting_incomplete) and it
refuses to run unless the contract carries a literal ``cohort_start_iso``. Readiness mode
is outcome-blind by construction here only in that it still fail-closes funding; reading
returns for a real verdict waits for the funding prerequisite PR and the freeze PR.

Mapping choices (flagged for review, per the colleague's answers):
  * ex-funding return = ``gross_return_pct - fees_usd / notional * 100`` (percent), NOT
    an add-back from the fixed-funding ``net_return_pct``;
  * canonical asset = the point-in-time ``identity_key`` from the latest universe
    snapshot at-or-before the decision, matched on the EXACT route (exchange +
    native_market_id); an unresolved identity is ``None`` (=> identity_unresolved),
    never a bare-ticker fallback;
  * exit is the probe's real ``exit_reason`` + the outcome rows' actual
    ``quote_observed_at``, never a nominal ``entry+240m``.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

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
    NoRegisteredFundingSource,
    ProbeClass,
    ProbeRecord,
    WatchDecision,
    cohort_rows_digest,
    evaluate_cohort,
    filter_to_cohort,
    formal_cohort_start,
    verdict_fingerprint,
)
from .momentum_flow_paper_contract import HOLD12H_PAPER_CONTRACT

if TYPE_CHECKING:
    from collections.abc import Sequence

_HORIZONS = (HORIZON_240, HORIZON_720)


def _utc(value: datetime | None) -> datetime | None:
    return None if value is None else value.astimezone(UTC)


def _ex_funding_net_pct(gross_return_pct: Any, fees_usd: Any, notional: Any) -> float | None:
    """Percent return net of FEES only (funding is applied later from the ACTUAL source).
    ``gross_return_pct - fees_usd / notional * 100``; None when any input is missing or
    the notional is non-positive (the pure layer then classifies it)."""
    if gross_return_pct is None or fees_usd is None or notional is None:
        return None
    notional_f = float(notional)
    if notional_f <= 0:
        return None
    net: float = float(gross_return_pct) - float(fees_usd) / notional_f * 100.0
    return net


async def load_cohort(
    db_url: str, *, cohort_start: datetime, decision_prefix_end: datetime
) -> tuple[tuple[WatchDecision, ...], dict[str, ProbeRecord]]:
    """Load the WATCH denominator and the hold12h probes for the half-open window, mapping
    each row into the pure dataclasses. Identity is resolved point-in-time per WATCH."""
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
                            """
                        SELECT w.watch_id, w.decision_at, w.exchange, w.market_type, w.symbol
                        FROM timeseries.momentum_flow_watch_evaluations_1m w
                        WHERE w.decision_status = 'watch'
                          AND w.decision_at >= :start AND w.decision_at < :end
                        """
                        ),
                        {"start": cohort_start, "end": decision_prefix_end},
                    )
                )
                .mappings()
                .all()
            )

            probe_rows = (
                (
                    await conn.execute(
                        text(
                            """
                        SELECT p.paper_id, p.watch_id, p.exchange, p.market_type, p.symbol,
                               p.market_id, p.entry_status, p.position_status, p.exit_reason,
                               p.entry_at, p.exit_at, p.gross_return_pct, p.fees_usd,
                               p.max_adverse_return_pct, p.entry_filled_notional_usd
                        FROM app.momentum_flow_paper_probes p
                        JOIN timeseries.momentum_flow_watch_evaluations_1m w
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
                                """
                            SELECT o.paper_id, o.horizon_minutes, o.status, o.quote_observed_at,
                                   o.gross_return_pct, o.fees_usd, o.filled_notional_usd
                            FROM app.momentum_flow_paper_outcomes o
                            WHERE o.paper_id = ANY(:ids) AND o.horizon_minutes = ANY(:hz)
                            """
                            ),
                            {"ids": paper_ids, "hz": list(_HORIZONS)},
                        )
                    )
                    .mappings()
                    .all()
                )

            identities = await _resolve_identities(conn, watch_rows, probe_rows)
    finally:
        await engine.dispose()

    outcomes_by_paper: dict[Any, dict[int, HorizonOutcome]] = {}
    for row in outcome_rows:
        outcomes_by_paper.setdefault(row["paper_id"], {})[int(row["horizon_minutes"])] = (
            HorizonOutcome(
                horizon_minutes=int(row["horizon_minutes"]),
                resolved=row["status"] == "resolved",
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
        watch_id = str(row["watch_id"])
        probes[watch_id] = ProbeRecord(
            watch_id=watch_id,
            route=InstrumentRoute(
                exchange=str(row["exchange"]),
                market_type=str(row["market_type"]),
                market_id=str(row["market_id"] or ""),
                unified_symbol=str(row["symbol"]),
            ),
            entry_at=_utc(row["entry_at"]) or cohort_start,
            entry_ok=row["entry_status"] == "opened",
            exit_at=_utc(row["exit_at"]),
            exit_resolved=row["position_status"] == "closed",
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


async def _resolve_identities(
    conn: Any, watch_rows: Sequence[Any], probe_rows: Sequence[Any]
) -> dict[str, str | None]:
    """Point-in-time canonical identity per WATCH: the ``identity_key`` of the matching
    instrument in the latest universe snapshot at-or-before the decision, keyed on the
    EXACT route (exchange + native_market_id, taken from the probe when present, else the
    WATCH symbol). Unresolved -> None."""
    from sqlalchemy import text

    market_id_by_watch = {str(p["watch_id"]): str(p["market_id"] or "") for p in probe_rows}
    identities: dict[str, str | None] = {}
    for row in watch_rows:
        watch_id = str(row["watch_id"])
        native_id = market_id_by_watch.get(watch_id) or str(row["symbol"])
        result = (
            await conn.execute(
                text(
                    """
                    SELECT i.identity_key
                    FROM app.momentum_universe_snapshots s
                    JOIN app.momentum_universe_instruments i
                      ON i.exchange = s.exchange
                     AND i.universe_version = s.universe_version
                     AND i.catalog_version = s.catalog_version
                    WHERE s.exchange = :exchange
                      AND s.captured_at <= :as_of
                      AND i.native_market_id = :native_id
                      AND i.identity_status = 'resolved'
                    ORDER BY s.captured_at DESC
                    LIMIT 1
                    """
                ),
                {
                    "exchange": str(row["exchange"]),
                    "as_of": row["decision_at"],
                    "native_id": native_id,
                },
            )
        ).first()
        identities[watch_id] = None if result is None else str(result[0])
    return identities


def _artifact(
    contract: Hold12hVerdictContract,
    evaluation: CohortEvaluation,
    *,
    cohort_start: datetime,
    decision_prefix_end: datetime,
    fingerprint: str,
    code_revision: str,
    working_tree_dirty: bool,
    formal: bool,
) -> dict[str, Any]:
    result = decide_verdict(contract, evaluation.inputs)
    return {
        "contract_version": contract.contract_version,
        "contract_sha256": contract.sha256_hex(),
        "registered": contract.cohort_start_iso is not None,
        "formal_run": formal,
        "cohort_start": cohort_start.isoformat(),
        "decision_prefix_end": decision_prefix_end.isoformat(),
        "code_revision": code_revision,
        "working_tree_dirty": working_tree_dirty,
        "fingerprint": fingerprint,
        "funnel": {cls.value: evaluation.funnel[cls] for cls in ProbeClass},
        "analyzable_pairs": evaluation.inputs.analyzable_pairs,
        "verdict": result.outcome.value,
        "gate": result.gate,
        "reason": result.reason,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="HYP-015 hold12h verdict reader (DRAFT)")
    parser.add_argument(
        "--formal-run",
        action="store_true",
        help="require a registered cohort boundary; fail-closed on funding",
    )
    parser.add_argument(
        "--decision-prefix-end",
        required=True,
        help="UTC ISO upper bound (exclusive) of the decision window",
    )
    parser.add_argument("--code-revision", default=os.getenv("SCHURFER_GIT_SHA", "unknown"))
    parser.add_argument("--working-tree-dirty", action=argparse.BooleanOptionalAction, default=True)
    return parser


async def _run(args: argparse.Namespace) -> str:
    contract = HOLD12H_VERDICT_CONTRACT
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise ValueError("DATABASE_URL is required for the hold12h verdict reader")

    boundary = formal_cohort_start(contract)
    if args.formal_run and boundary is None:
        raise SystemExit(
            "formal-run refused: the contract has no registered cohort_start_iso (fail-closed)"
        )
    cohort_start = boundary or datetime(1970, 1, 1, tzinfo=UTC)
    decision_prefix_end = datetime.fromisoformat(args.decision_prefix_end).astimezone(UTC)

    watches, probes = await load_cohort(
        db_url, cohort_start=cohort_start, decision_prefix_end=decision_prefix_end
    )
    watches = filter_to_cohort(
        watches, cohort_start=cohort_start, decision_prefix_end=decision_prefix_end
    )
    # DRAFT: no registered actual-funding source -> fail-closed accounting_incomplete.
    funding = NoRegisteredFundingSource()
    evaluation = evaluate_cohort(contract, watches, probes, funding)
    fingerprint = verdict_fingerprint(
        contract_sha256=contract.sha256_hex(),
        cohort_start=cohort_start,
        decision_prefix_end=decision_prefix_end,
        code_revision=args.code_revision,
        working_tree_dirty=args.working_tree_dirty,
        funding_source_id="no_registered_funding_source",
        data_versions={"paper_contract": contract.paper_contract_sha256},
        rows_digest=cohort_rows_digest(watches, probes, funding),
        funnel=evaluation.funnel,
        inputs=evaluation.inputs,
    )
    artifact = _artifact(
        contract,
        evaluation,
        cohort_start=cohort_start,
        decision_prefix_end=decision_prefix_end,
        fingerprint=fingerprint,
        code_revision=args.code_revision,
        working_tree_dirty=args.working_tree_dirty,
        formal=args.formal_run,
    )
    return json.dumps(artifact, indent=2, sort_keys=True)


def main() -> None:
    import asyncio
    import sys

    args = build_parser().parse_args()
    sys.stdout.write(asyncio.run(_run(args)) + "\n")


if __name__ == "__main__":
    main()
