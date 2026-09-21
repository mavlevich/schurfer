"""Outcome-blind candidate table on the calibration slice (freeze-PR input).

Scans ONLY the 14-day calibration slice (no returns) and, for a grid of OI-growth
percentiles with FIXED buy-pressure / containment / OI-notional-floor percentiles,
reports fires, independent episodes after cooldown, distinct identity keys, venues,
weeks, participation rejections, and PROJECTED evaluation episodes extrapolated from the
calibration episode RATE (never counted on the evaluation window). The power-floor rule
picks the HIGHEST OI percentile whose projected evaluation episodes clear a floor
(default 150 = 1.5x over the formal floor of 100). Percentiles are non-circular: the
OI-notional floor comes from ALL available OI-notional; the feature percentiles come from
the floor+participation-eligible decisions. Reads funding? No -- funding is separate.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .abnormal_flow_replay import (
    form_episodes,
    oi_notional_usd,
    participation_frac,
)
from .abnormal_flow_scan import Histogram, _iter_decisions, load_identity_resolver
from .abnormal_flow_screen import AbnormalFlowContract

if TYPE_CHECKING:
    from collections.abc import Callable

CANDIDATE_TABLE_VERSION = "abnormal_flow_candidate_table_v1"
_OI_PERCENTILES = (0.90, 0.95, 0.975, 0.99)


@dataclass(frozen=True)
class _Fire:
    exchange: str
    identity_key: str
    decision_at: datetime
    iso_week: str
    oi_growth: float


def select_oi_percentile(rows: list[dict[str, Any]], min_projected: float) -> float | None:
    """Power-floor rule: the HIGHEST OI percentile whose projected evaluation episodes
    clear ``min_projected``. ``None`` if none clear it."""
    for row in sorted(rows, key=lambda r: r["oi_percentile"], reverse=True):
        if row["projected_evaluation_episodes"] >= min_projected:
            return float(row["oi_percentile"])
    return None


def _logspace(lo_exp: float, hi_exp: float, n: int) -> tuple[float, ...]:
    return tuple(10 ** (lo_exp + (hi_exp - lo_exp) * i / n) for i in range(1, n))


def _linspace(lo: float, hi: float, n: int) -> tuple[float, ...]:
    return tuple(lo + (hi - lo) * i / n for i in range(1, n))


def build_candidate_table(
    paths: list[str],
    *,
    calib_start: datetime,
    calib_end: datetime,
    eval_scorable_days: float,
    contract: AbnormalFlowContract,
    resolver: Callable[..., str | None],
    buy_pct: float = 0.90,
    containment_pct: float = 0.25,
    floor_pct: float = 0.25,
    oi_pcts: tuple[float, ...] = _OI_PERCENTILES,
    min_projected: float = 150.0,
) -> dict[str, Any]:
    calib_days = (calib_end - calib_start).total_seconds() / 86400.0
    cap = contract.max_participation_frac
    position_usd = contract.position_usd

    def _iter() -> Any:
        return _iter_decisions(
            paths,
            window_start=calib_start,
            window_end=calib_end,
            contract=contract,
            resolver=resolver,
        )

    # Pass 1: OI-notional floor from ALL available decisions (non-circular).
    oi_notional = Histogram(_logspace(3, 10, 800))
    for d in _iter():
        if d.unavailable_reason is not None:
            continue
        oi_usd = oi_notional_usd(
            d.exchange, d.oi_native_amount, d.oi_native_value_usd, d.decision_price
        )
        if oi_usd is not None:
            oi_notional.add(oi_usd)
    floor = oi_notional.quantile(floor_pct)

    # Pass 2: feature percentiles over floor+participation-eligible decisions.
    buy_h = Histogram(_linspace(0.5, 1.0, 500))
    cont_h = Histogram(_linspace(0.0, 1.0, 1000))
    oi_h = Histogram(_linspace(-100.0, 1000.0, 1100))
    eligible = 0
    participation_rejections = 0
    for d in _iter():
        if d.unavailable_reason is not None:
            continue
        oi_usd = oi_notional_usd(
            d.exchange, d.oi_native_amount, d.oi_native_value_usd, d.decision_price
        )
        if oi_usd is None or floor is None or oi_usd < floor:
            continue
        part = participation_frac(position_usd, d.pre_decision_turnover_usd)
        if part is None or (cap is not None and part > cap):
            participation_rejections += 1
            continue
        eligible += 1
        if d.buy_pressure is not None:
            buy_h.add(d.buy_pressure)
        if d.containment is not None:
            cont_h.add(d.containment)
        if d.oi_growth_pct is not None:
            oi_h.add(d.oi_growth_pct)
    buy_thr = buy_h.quantile(buy_pct)
    cont_thr = cont_h.quantile(containment_pct)
    oi_thr = {p: oi_h.quantile(p) for p in oi_pcts}
    oi_thr_min = min(v for v in oi_thr.values() if v is not None)

    # Pass 3: collect fires (eligible + buy>=buy_thr + cont<=cont_thr + oi>=lowest OI thr).
    fires: list[_Fire] = []
    for d in _iter():
        if d.unavailable_reason is not None:
            continue
        oi_usd = oi_notional_usd(
            d.exchange, d.oi_native_amount, d.oi_native_value_usd, d.decision_price
        )
        if oi_usd is None or floor is None or oi_usd < floor:
            continue
        part = participation_frac(position_usd, d.pre_decision_turnover_usd)
        if part is None or (cap is not None and part > cap):
            continue
        if (
            d.buy_pressure is None
            or d.containment is None
            or d.oi_growth_pct is None
            or buy_thr is None
            or cont_thr is None
        ):
            continue
        if (
            d.buy_pressure >= buy_thr
            and d.containment <= cont_thr
            and d.oi_growth_pct >= oi_thr_min
        ):
            fires.append(
                _Fire(d.exchange, d.canonical_asset, d.decision_at, d.iso_week, d.oi_growth_pct)
            )

    cooldown = contract.cooldown_minutes
    rows: list[dict[str, Any]] = []
    for p in oi_pcts:
        threshold = oi_thr[p]
        subset = [f for f in fires if threshold is not None and f.oi_growth >= threshold]
        episodes = form_episodes(subset, cooldown)  # type: ignore[arg-type]
        projected = len(episodes) * (eval_scorable_days / calib_days) if calib_days > 0 else 0.0
        rows.append(
            {
                "oi_percentile": p,
                "oi_growth_threshold_pct": threshold,
                "calibration_fires": len(subset),
                "calibration_episodes": len(episodes),
                "distinct_identity_keys": len({(f.exchange, f.identity_key) for f in subset}),
                "venues": sorted({f.exchange for f in subset}),
                "weeks": sorted({f.iso_week for f in subset}),
                "projected_evaluation_episodes": round(projected, 1),
                "passes_power_floor": projected >= min_projected,
            }
        )

    selected = select_oi_percentile(rows, min_projected)

    return {
        "candidate_table_version": CANDIDATE_TABLE_VERSION,
        "calibration_window": {"start": calib_start.isoformat(), "end": calib_end.isoformat()},
        "calibration_days": calib_days,
        "eval_scorable_days": eval_scorable_days,
        "projection": (
            "projected = calibration_episodes * eval_scorable_days / calibration_days "
            "(calib rate; NO evaluation read)"
        ),
        "fixed_percentiles": {
            "buy_pressure_pct": buy_pct,
            "buy_pressure_threshold": buy_thr,
            "containment_pct": containment_pct,
            "containment_threshold": cont_thr,
            "oi_notional_floor_pct": floor_pct,
            "oi_notional_floor": floor,
        },
        "eligible_calibration_decisions": eligible,
        "participation_rejections": participation_rejections,
        "min_projected_episodes": min_projected,
        "rows": rows,
        "selected_oi_percentile": selected,
        "note": (
            "Power-floor rule: highest OI percentile whose PROJECTED evaluation episodes clear "
            f"{min_projected}. 150 is a power FLOOR, not sufficiency (cluster-robust SE + "
            "leave-one-out still govern). No returns read."
        ),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cold-bars-dir", type=Path, required=True)
    parser.add_argument("--identity-snapshot", type=Path, required=True)
    parser.add_argument("--contract-json", type=Path, required=True)
    parser.add_argument("--calib-start", type=date.fromisoformat, required=True)
    parser.add_argument("--calib-end", type=date.fromisoformat, required=True, help="exclusive")
    parser.add_argument(
        "--data-end", type=date.fromisoformat, required=True, help="exclusive last data day"
    )
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--min-projected", type=float, default=150.0)
    return parser


def main() -> None:
    args: Any = build_parser().parse_args()
    contract = AbnormalFlowContract(**json.loads(args.contract_json.read_text()))
    resolver, _ = load_identity_resolver(args.identity_snapshot)
    calib_start = datetime(
        args.calib_start.year, args.calib_start.month, args.calib_start.day, tzinfo=UTC
    )
    calib_end = datetime(args.calib_end.year, args.calib_end.month, args.calib_end.day, tzinfo=UTC)
    data_end = datetime(args.data_end.year, args.data_end.month, args.data_end.day, tzinfo=UTC)
    # Evaluation scorable span: from calib_end to the last decision with a full 720m path.
    horizon = timedelta(minutes=contract.outcome_horizon_minutes)
    last_scorable = data_end - horizon - timedelta(minutes=2)  # entry + exit bar allowance
    eval_scorable_days = max(0.0, (last_scorable - calib_end).total_seconds() / 86400.0)
    calib_files = sorted(
        str(f)
        for f in args.cold_bars_dir.glob("bars-*.parquet")
        if args.calib_start.isoformat() <= f.name[5:15] < args.calib_end.isoformat()
    )
    table = build_candidate_table(
        calib_files,
        calib_start=calib_start,
        calib_end=calib_end,
        eval_scorable_days=eval_scorable_days,
        contract=contract,
        resolver=resolver.identity_key,
        min_projected=args.min_projected,
    )
    args.out.write_text(json.dumps(table, indent=2, sort_keys=True) + "\n")
    sys.stdout.write(f"{args.out}\n")


if __name__ == "__main__":
    main()
