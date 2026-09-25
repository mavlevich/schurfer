"""Build a burned-window portfolio diagnostic from an immutable episode snapshot.

This command is deliberately not a formal-result runner.  It reads the already
burned abnormal-flow v1 outcome window once, persists the normalized position rows,
and reports the pre-declared portfolio-engine-v2 K-slot sensitivity without choosing
a winning K.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import duckdb

from . import abnormal_flow_replay as replay_module
from .abnormal_flow_formal_runner import (
    RealGitState,
    _hash_file,
    _load_registered_contract,
    _load_registered_evaluation_manifest,
    _verified_cold_bar_paths,
    dependency_bounds,
)
from .abnormal_flow_replay import (
    Outcome,
    parquet_outcome_reader,
    priced_proxy_path_times,
    proxy_net_return,
)
from .abnormal_flow_snapshots import SnapshotReader, decision_id
from .portfolio_engine_v2 import (
    PortfolioPosition,
    PositionSizingPolicy,
    TradeDirection,
    UnresolvedCapitalPolicy,
    simulate_portfolio_v2,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from .abnormal_flow_replay import DecisionFeatures, RouteKey
    from .abnormal_flow_screen import AbnormalFlowContract

DIAGNOSTIC_VERSION: Final = "abnormal_flow_burned_portfolio_diagnostic_v1"
UNRESOLVED_REASON: Final = "missing_or_incomplete_priced_proxy_path"
DEFAULT_K_VALUES: Final = tuple(range(1, 21))


@dataclass(frozen=True)
class EpisodeOutcomeRow:
    """One adapter row per primary episode: the engine input plus its provenance."""

    position: PortfolioPosition
    exchange: str
    market_type: str
    native_market_id: str
    capture_version: str
    decision_at: datetime
    entry_price: float | None
    exit_price: float | None
    path_provenance: str


def _path_provenance(contract: AbnormalFlowContract) -> str:
    return f"{contract.entry_reference}|{contract.exit_reference}"


def _funding_bps(contract: AbnormalFlowContract, exchange: str) -> float:
    if exchange == "binance":
        value = contract.funding_bps_720m_binance
    elif exchange == "bybit":
        value = contract.funding_bps_720m_bybit
    else:
        raise ValueError(f"funding model incomplete for exchange {exchange!r}")
    if value is None:
        raise ValueError(f"funding model incomplete for exchange {exchange!r}")
    return float(value)


def build_outcome_rows(
    contract: AbnormalFlowContract,
    episodes: Sequence[DecisionFeatures],
    outcomes: Mapping[RouteKey, Outcome],
) -> list[EpisodeOutcomeRow]:
    """Normalize every primary episode into exactly one row.

    Missing outcomes remain fail-closed unresolved rows that still occupy a slot.
    Entry and exit timestamps come from the same helper the outcome reader uses.
    """

    contract.require_frozen()
    if contract.direction != "long":
        raise ValueError("the v1 diagnostic adapter supports only the frozen long direction")
    required_costs = (
        contract.entry_slippage_bps,
        contract.exit_slippage_bps,
        contract.taker_fee_bps,
    )
    if any(value is None for value in required_costs):
        raise ValueError("the frozen execution cost model is incomplete")
    assert contract.entry_slippage_bps is not None
    assert contract.exit_slippage_bps is not None
    assert contract.taker_fee_bps is not None
    entry_slippage_bps = float(contract.entry_slippage_bps)
    exit_slippage_bps = float(contract.exit_slippage_bps)
    fees_bps = 2.0 * float(contract.taker_fee_bps)

    expected_keys = {episode.route_key() for episode in episodes}
    if len(expected_keys) != len(episodes):
        raise ValueError("episode snapshot contains duplicate route keys")
    extra_keys = set(outcomes) - expected_keys
    if extra_keys:
        raise ValueError("outcome reader returned rows outside the episode snapshot")

    provenance = _path_provenance(contract)
    rows: list[EpisodeOutcomeRow] = []

    def add(
        position: PortfolioPosition, episode: DecisionFeatures, outcome: Outcome | None
    ) -> None:
        rows.append(
            EpisodeOutcomeRow(
                position=position,
                exchange=episode.exchange,
                market_type=episode.market_type,
                native_market_id=episode.native_market_id,
                capture_version=episode.capture_version,
                decision_at=episode.decision_at,
                entry_price=outcome.entry_price if outcome is not None else None,
                exit_price=outcome.exit_price if outcome is not None else None,
                path_provenance=provenance,
            )
        )

    for episode in episodes:
        entry_at, _exit_bar_start, exit_at = priced_proxy_path_times(
            episode.decision_at, contract.outcome_horizon_minutes
        )
        outcome = outcomes.get(episode.route_key())
        funding_bps = _funding_bps(contract, episode.exchange)
        if outcome is None:
            add(
                PortfolioPosition(
                    decision_id=decision_id(episode),
                    canonical_asset=episode.canonical_asset,
                    direction=TradeDirection.LONG,
                    entry_at=entry_at,
                    exit_at=None,
                    gross_return=None,
                    entry_slippage_bps=entry_slippage_bps,
                    exit_slippage_bps=exit_slippage_bps,
                    fees_bps=fees_bps,
                    funding_bps=funding_bps,
                    net_return=None,
                    unresolved_reason=UNRESOLVED_REASON,
                    planned_exit_at=exit_at,
                ),
                episode,
                None,
            )
            continue
        if (
            outcome.entry_price is None
            or outcome.exit_price is None
            or outcome.entry_price <= 0
            or outcome.exit_price <= 0
        ):
            raise ValueError("resolved outcome is missing an entry or exit price")
        gross_return = (outcome.exit_price - outcome.entry_price) / outcome.entry_price
        net_return = proxy_net_return(
            contract,
            episode.exchange,
            outcome.entry_price,
            outcome.exit_price,
        )
        if net_return is None:
            raise ValueError("resolved outcome could not be scored")
        add(
            PortfolioPosition(
                decision_id=decision_id(episode),
                canonical_asset=episode.canonical_asset,
                direction=TradeDirection.LONG,
                entry_at=entry_at,
                exit_at=exit_at,
                gross_return=gross_return,
                entry_slippage_bps=entry_slippage_bps,
                exit_slippage_bps=exit_slippage_bps,
                fees_bps=fees_bps,
                funding_bps=funding_bps,
                net_return=net_return,
                unresolved_reason=None,
                planned_exit_at=exit_at,
            ),
            episode,
            outcome,
        )
    return rows


def build_positions(
    contract: AbnormalFlowContract,
    episodes: Sequence[DecisionFeatures],
    outcomes: Mapping[RouteKey, Outcome],
) -> list[PortfolioPosition]:
    return [row.position for row in build_outcome_rows(contract, episodes, outcomes)]


def portfolio_frontier(
    positions: Sequence[PortfolioPosition],
    *,
    initial_capital: float,
    k_values: Sequence[int],
    max_positions_per_asset: int,
    sizing_policy: PositionSizingPolicy,
    unresolved_policy: UnresolvedCapitalPolicy = UnresolvedCapitalPolicy.HOLD_TO_END,
    unresolved_gross_return: float | None = None,
) -> list[dict[str, Any]]:
    if not k_values or any(k <= 0 for k in k_values) or len(set(k_values)) != len(k_values):
        raise ValueError("K values must be unique positive integers")
    rows: list[dict[str, Any]] = []
    for k_slots in sorted(k_values):
        metrics = simulate_portfolio_v2(
            positions,
            initial_capital=initial_capital,
            k_slots=k_slots,
            max_positions_per_asset=max_positions_per_asset,
            sizing_policy=sizing_policy,
            unresolved_policy=unresolved_policy,
            unresolved_gross_return=unresolved_gross_return,
        )
        row = asdict(metrics)
        row["k_slots"] = k_slots
        row["initial_position_usd"] = initial_capital / k_slots
        row["total_net_pnl_incl_assumed"] = metrics.final_equity - initial_capital
        rows.append(row)
    return rows


def unresolved_scenarios(positions: Sequence[PortfolioPosition]) -> dict[str, dict[str, Any]]:
    """Declared bracket for unresolved outcomes; none of these is an observed result.

    ``hold_to_end`` is the fail-closed engine default. The planned-exit scenarios free
    the slot when the strategy would have exited and book one explicit gross return.
    ``mean_resolved`` assumes missingness is unrelated to outcome and is optimistic.
    """

    resolved = [item.gross_return for item in positions if item.gross_return is not None]
    scenarios: dict[str, dict[str, Any]] = {
        "hold_to_end": {
            "unresolved_policy": UnresolvedCapitalPolicy.HOLD_TO_END.value,
            "unresolved_gross_return": None,
        }
    }
    if resolved:
        for name, value in (
            ("planned_exit_worst_resolved", min(resolved)),
            ("planned_exit_zero_gross", 0.0),
            ("planned_exit_mean_resolved", sum(resolved) / len(resolved)),
        ):
            scenarios[name] = {
                "unresolved_policy": UnresolvedCapitalPolicy.PLANNED_EXIT.value,
                "unresolved_gross_return": value,
            }
    return scenarios


def peak_planned_concurrency(positions: Sequence[PortfolioPosition]) -> int:
    """Most positions that overlap if every signal is taken and held to its plan."""

    edges: list[tuple[datetime, int]] = []
    for item in positions:
        end = item.exit_at or item.planned_exit_at
        if end is None:
            raise ValueError(f"{item.decision_id}: no exit or planned exit")
        edges.extend(((item.entry_at, 1), (end, -1)))
    # Exits sort before entries at the same instant, matching the engine.
    edges.sort(key=lambda edge: (edge[0], edge[1]))
    current = peak = 0
    for _at, delta in edges:
        current += delta
        peak = max(peak, current)
    return peak


def scenario_frontiers(
    positions: Sequence[PortfolioPosition],
    *,
    initial_capital: float,
    k_values: Sequence[int],
    max_positions_per_asset: int,
    sizing_policy: PositionSizingPolicy,
) -> dict[str, Any]:
    """K frontier per unresolved scenario plus the take-every-signal policy."""

    scenarios = unresolved_scenarios(positions)
    take_all_k = peak_planned_concurrency(positions) if positions else 1
    result: dict[str, Any] = {"scenarios": {}, "take_every_signal": {}}
    for name, scenario in scenarios.items():
        policy = UnresolvedCapitalPolicy(scenario["unresolved_policy"])
        assumption = scenario["unresolved_gross_return"]
        result["scenarios"][name] = {
            **scenario,
            "frontier": portfolio_frontier(
                positions,
                initial_capital=initial_capital,
                k_values=k_values,
                max_positions_per_asset=max_positions_per_asset,
                sizing_policy=sizing_policy,
                unresolved_policy=policy,
                unresolved_gross_return=assumption,
            ),
        }
        if policy is UnresolvedCapitalPolicy.PLANNED_EXIT:
            # No per-asset cap and K at peak overlap: every signal gets a slot.
            (row,) = portfolio_frontier(
                positions,
                initial_capital=initial_capital,
                k_values=(take_all_k,),
                max_positions_per_asset=max(len(positions), 1),
                sizing_policy=sizing_policy,
                unresolved_policy=policy,
                unresolved_gross_return=assumption,
            )
            result["take_every_signal"][name] = row
    result["take_every_signal_k"] = take_all_k
    return result


def _read_outcomes_once(
    paths: list[str],
    episodes: Sequence[DecisionFeatures],
    *,
    outcome_horizon_minutes: int,
    progress: Callable[[int, int], None] | None = None,
) -> dict[RouteKey, Outcome]:
    if replay_module.BURNED_DIAGNOSTIC_RETURNS_RUN_ENABLED:
        raise RuntimeError("returns reader is already enabled in this process")
    replay_module.BURNED_DIAGNOSTIC_RETURNS_RUN_ENABLED = True
    try:
        reader = parquet_outcome_reader(
            paths,
            outcome_horizon_minutes=outcome_horizon_minutes,
            progress=progress,
        )
        return reader(episodes)
    finally:
        replay_module.BURNED_DIAGNOSTIC_RETURNS_RUN_ENABLED = False


_OUTCOME_COLUMNS: Final = (
    "decision_id VARCHAR, canonical_asset VARCHAR, direction VARCHAR, "
    "entry_at TIMESTAMPTZ, exit_at TIMESTAMPTZ, gross_return DOUBLE, "
    "entry_slippage_bps DOUBLE, exit_slippage_bps DOUBLE, fees_bps DOUBLE, "
    "funding_bps DOUBLE, net_return DOUBLE, unresolved_reason VARCHAR, "
    "exchange VARCHAR, market_type VARCHAR, native_market_id VARCHAR, "
    "capture_version VARCHAR, decision_at TIMESTAMPTZ, entry_price DOUBLE, "
    "exit_price DOUBLE, path_provenance VARCHAR, planned_exit_at TIMESTAMPTZ"
)
_POSITION_COLUMNS: Final = (
    "decision_id, canonical_asset, direction, entry_at, exit_at, gross_return, "
    "entry_slippage_bps, exit_slippage_bps, fees_bps, funding_bps, net_return, "
    "unresolved_reason, planned_exit_at"
)
_FRONTIER_FIELDS: Final = (
    "scenario",
    "unresolved_gross_return",
    "k_slots",
    "initial_position_usd",
    "accepted_entries",
    "total_trades",
    "winning_trades",
    "losing_trades",
    "gross_pnl",
    "net_pnl",
    "unresolved_released",
    "unresolved_assumed_net_pnl",
    "total_net_pnl_incl_assumed",
    "final_equity",
    "max_drawdown_pct",
    "max_concurrent_positions",
    "unresolved_fail_closed",
    "available_cash",
    "reserved_capital",
    "peak_gross_exposure",
    "peak_abs_net_exposure",
    "peak_leverage",
    "min_entry_margin",
    "max_entry_margin",
    "average_entry_margin",
    "accounting_complete",
    "rejection_counts",
)


def _outcome_row_tuple(row: EpisodeOutcomeRow) -> tuple[Any, ...]:
    item = row.position
    return (
        item.decision_id,
        item.canonical_asset,
        item.direction.name.lower(),
        item.entry_at,
        item.exit_at,
        item.gross_return,
        item.entry_slippage_bps,
        item.exit_slippage_bps,
        item.fees_bps,
        item.funding_bps,
        item.net_return,
        item.unresolved_reason,
        row.exchange,
        row.market_type,
        row.native_market_id,
        row.capture_version,
        row.decision_at,
        row.entry_price,
        row.exit_price,
        row.path_provenance,
        item.planned_exit_at,
    )


def outcomes_fingerprint(rows: Sequence[EpisodeOutcomeRow]) -> str:
    """Content hash of the adapter rows, independent of Parquet encoder bytes."""

    canonical = sorted(
        [
            value.isoformat() if isinstance(value, datetime) else value
            for value in _outcome_row_tuple(row)
        ]
        for row in rows
    )
    payload = json.dumps(canonical, separators=(",", ":"), allow_nan=False)
    return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()


def bundle_fingerprint(report: Mapping[str, Any]) -> str:
    """Hash of the report minus wall-clock fields, so a rerun reproduces it."""

    stable = {key: value for key, value in report.items() if key != "generated_at"}
    payload = json.dumps(stable, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()


def load_positions(path: Path) -> list[PortfolioPosition]:
    with duckdb.connect(":memory:") as db:
        rows = db.execute(
            f"SELECT {_POSITION_COLUMNS} FROM read_parquet(?) "  # noqa: S608 -- constant columns
            "ORDER BY entry_at, canonical_asset, decision_id",
            [str(path)],
        ).fetchall()
    return [
        PortfolioPosition(
            decision_id=str(row[0]),
            canonical_asset=str(row[1]),
            direction=TradeDirection[str(row[2]).upper()],
            entry_at=row[3],
            exit_at=row[4],
            gross_return=float(row[5]) if row[5] is not None else None,
            entry_slippage_bps=float(row[6]),
            exit_slippage_bps=float(row[7]),
            fees_bps=float(row[8]),
            funding_bps=float(row[9]),
            net_return=float(row[10]) if row[10] is not None else None,
            unresolved_reason=str(row[11]) if row[11] is not None else None,
            planned_exit_at=row[12],
        )
        for row in rows
    ]


def write_outcome_rows(path: Path, rows: Sequence[EpisodeOutcomeRow]) -> None:
    with duckdb.connect(":memory:") as db:
        db.execute(f"CREATE TABLE positions ({_OUTCOME_COLUMNS})")
        if rows:
            placeholders = ", ".join("?" * len(_outcome_row_tuple(rows[0])))
            db.executemany(
                f"INSERT INTO positions VALUES ({placeholders})",  # noqa: S608 -- placeholders
                [_outcome_row_tuple(row) for row in rows],
            )
        db.execute(
            "COPY (SELECT * FROM positions ORDER BY entry_at, canonical_asset, decision_id) "
            "TO ? (FORMAT PARQUET, COMPRESSION ZSTD)",
            [str(path)],
        )


def _write_bundle(
    output_dir: Path,
    *,
    write_positions: Callable[[Path], object],
    report: dict[str, Any],
) -> None:
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    if output_dir.exists():
        raise FileExistsError(f"refusing to overwrite diagnostic bundle: {output_dir}")
    staging = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}-", dir=output_dir.parent))
    try:
        positions_path = staging / "portfolio_positions.parquet"
        write_positions(positions_path)

        frontier_path = staging / "portfolio_frontier.csv"
        with frontier_path.open("x", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=_FRONTIER_FIELDS, extrasaction="ignore")
            writer.writeheader()
            for name, scenario in report["scenarios"].items():
                for row in scenario["frontier"]:
                    writer.writerow(
                        {
                            **row,
                            "scenario": name,
                            "unresolved_gross_return": scenario["unresolved_gross_return"],
                            "rejection_counts": json.dumps(row["rejection_counts"], sort_keys=True),
                        }
                    )

        report["artifacts"] = {
            "portfolio_positions.parquet": _hash_file(positions_path),
            "portfolio_frontier.csv": _hash_file(frontier_path),
        }
        report["bundle_fingerprint"] = bundle_fingerprint(
            {key: value for key, value in report.items() if key != "artifacts"}
        )
        report_path = staging / "diagnostic_report.json"
        report_path.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n")
        (staging / "diagnostic_report.sha256").write_text(_hash_file(report_path) + "\n")
        staging.rename(output_dir)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _run_code_state() -> dict[str, Any]:
    git = RealGitState()
    return {"code_revision": git.get_revision(), "working_tree_dirty": git.is_dirty()}


def run_diagnostic(
    *,
    snapshot_dir: Path,
    contract_path: Path,
    evaluation_manifest_path: Path,
    scan_manifest_path: Path,
    cold_bars_dir: Path,
    output_dir: Path,
    k_values: Sequence[int] = DEFAULT_K_VALUES,
    max_positions_per_asset: int = 1,
    sizing_policy: PositionSizingPolicy = PositionSizingPolicy.FIXED_INITIAL_EQUITY,
    burned_window_diagnostic: bool = False,
) -> dict[str, Any]:
    started_at = time.monotonic()

    def announce(message: str) -> None:
        elapsed = time.monotonic() - started_at
        sys.stderr.write(f"[portfolio +{elapsed:.1f}s] {message}\n")
        sys.stderr.flush()

    if not burned_window_diagnostic:
        raise ValueError("refusing returns read without --burned-window-diagnostic")
    if output_dir.exists():
        raise FileExistsError(f"refusing to overwrite diagnostic bundle: {output_dir}")

    contract, contract_hash, contract_file_hash = _load_registered_contract(contract_path)
    evaluation_manifest, evaluation_manifest_file_hash = _load_registered_evaluation_manifest(
        evaluation_manifest_path
    )
    scan_manifest_hash = _hash_file(scan_manifest_path)
    if scan_manifest_hash != evaluation_manifest.input_audit_fingerprint:
        raise ValueError("scan manifest does not match the registered evaluation manifest")

    fingerprint = snapshot_dir.name
    announce("verifying immutable decision/episode snapshot")
    snapshot = SnapshotReader(snapshot_dir, fingerprint)
    identity = snapshot.manifest.identity
    expected_identity = {
        "contract_hash": contract_hash,
        "contract_version": contract.contract_version,
        "evaluation_start": contract.window_start_utc,
        "evaluation_end_exclusive": contract.window_end_utc,
    }
    mismatches = {
        name: (identity.get(name), expected)
        for name, expected in expected_identity.items()
        if identity.get(name) != expected
    }
    if mismatches:
        raise ValueError(f"snapshot identity does not match the frozen contract: {mismatches}")

    episodes = snapshot.load_episodes()
    announce(f"snapshot verified; loaded {len(episodes)} primary episodes")

    def cold_bar_progress(done: int, total: int, day: date) -> None:
        announce(f"verified cold-bars {done}/{total}: {day}")

    announce("verifying registered cold-bar files and hashes")
    verified_paths = _verified_cold_bar_paths(
        cold_bars_dir,
        scan_manifest_path,
        dependency_bounds(contract),
        progress=cold_bar_progress,
    )
    announce("reading priced outcomes; this is the longest compute step")

    def outcome_progress(scanned_streams: int, matched_routes: int) -> None:
        announce(
            f"outcome scan: {scanned_streams} instrument streams, "
            f"{matched_routes} requested routes matched"
        )

    outcomes = _read_outcomes_once(
        verified_paths,
        episodes,
        outcome_horizon_minutes=contract.outcome_horizon_minutes,
        progress=outcome_progress,
    )
    announce(f"outcomes resolved for {len(outcomes)}/{len(episodes)} episodes")
    outcome_rows = build_outcome_rows(contract, episodes, outcomes)
    if len(outcome_rows) != len(episodes):
        raise ValueError("adapter must emit exactly one row per primary episode")
    positions = [row.position for row in outcome_rows]
    assert contract.portfolio_bank_usd is not None
    initial_capital = float(contract.portfolio_bank_usd)
    announce(f"simulating {len(k_values)} K-slot policies")
    frontiers = scenario_frontiers(
        positions,
        initial_capital=initial_capital,
        k_values=k_values,
        max_positions_per_asset=max_positions_per_asset,
        sizing_policy=sizing_policy,
    )
    resolved = sum(item.exit_at is not None for item in positions)
    report: dict[str, Any] = {
        "diagnostic_version": DIAGNOSTIC_VERSION,
        "classification": "post_hoc_burned_window_diagnostic_not_formal_evidence",
        "generated_at": datetime.now(UTC).isoformat(),
        "provenance": {
            "snapshot_fingerprint": fingerprint,
            "snapshot_manifest_sha256": _hash_file(snapshot_dir / "snapshot_manifest.json"),
            "snapshot_code_revision": snapshot.manifest.code_revision,
            "snapshot_working_tree_dirty": snapshot.manifest.working_tree_dirty,
            "contract_hash": contract_hash,
            "contract_file_hash": contract_file_hash,
            "evaluation_manifest_file_hash": evaluation_manifest_file_hash,
            "scan_manifest_hash": scan_manifest_hash,
            "cold_bar_files": len(verified_paths),
            "run": _run_code_state(),
            "path_provenance": _path_provenance(contract),
            "funding_model": "contract_conservative_720m_bps_not_actual_funding",
        },
        "outcomes_fingerprint": outcomes_fingerprint(outcome_rows),
        "policy": {
            "initial_capital": initial_capital,
            "k_values": sorted(k_values),
            "max_positions_per_asset": max_positions_per_asset,
            "sizing": sizing_policy.value,
            "unresolved_outcomes": (
                "never_scored_as_observed; bracketed by hold_to_end and planned_exit scenarios"
            ),
        },
        "coverage": {
            "episodes": len(positions),
            "resolved": resolved,
            "unresolved": len(positions) - resolved,
            "resolved_fraction": resolved / len(positions) if positions else 0.0,
        },
        **frontiers,
    }
    announce("publishing immutable diagnostic bundle")
    _write_bundle(
        output_dir,
        write_positions=lambda path: write_outcome_rows(path, outcome_rows),
        report=report,
    )
    announce(f"complete: {output_dir}")
    return report


def _parse_k_values(raw: str) -> tuple[int, ...]:
    if "-" in raw and "," not in raw:
        start_text, end_text = raw.split("-", maxsplit=1)
        start, end = int(start_text), int(end_text)
        if start > end:
            raise argparse.ArgumentTypeError("K range start must not exceed its end")
        return tuple(range(start, end + 1))
    try:
        values = tuple(int(part) for part in raw.split(",") if part)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("K values must be integers") from exc
    if not values:
        raise argparse.ArgumentTypeError("at least one K value is required")
    return values


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot-dir", type=Path, required=True)
    parser.add_argument("--cold-bars-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--contract",
        type=Path,
        default=Path("docs/research/evidence/abnormal-flow-v1/formal/contract.json"),
    )
    parser.add_argument(
        "--evaluation-manifest",
        type=Path,
        default=Path("docs/research/evidence/abnormal-flow-v1/formal/evaluation_manifest.json"),
    )
    parser.add_argument(
        "--scan-manifest",
        type=Path,
        default=Path(
            "docs/research/evidence/abnormal-flow-v1/20260921T062152Z-96acbc87/manifest.json"
        ),
    )
    parser.add_argument("--k-values", type=_parse_k_values, default=DEFAULT_K_VALUES)
    parser.add_argument("--max-positions-per-asset", type=int, default=1)
    parser.add_argument(
        "--sizing-policy",
        type=PositionSizingPolicy,
        choices=tuple(PositionSizingPolicy),
        metavar="{" + ",".join(policy.value for policy in PositionSizingPolicy) + "}",
        default=PositionSizingPolicy.FIXED_INITIAL_EQUITY,
    )
    parser.add_argument("--burned-window-diagnostic", action="store_true")
    return parser


def main() -> None:
    args: Any = build_parser().parse_args()
    report = run_diagnostic(
        snapshot_dir=args.snapshot_dir,
        contract_path=args.contract,
        evaluation_manifest_path=args.evaluation_manifest,
        scan_manifest_path=args.scan_manifest,
        cold_bars_dir=args.cold_bars_dir,
        output_dir=args.output_dir,
        k_values=args.k_values,
        max_positions_per_asset=args.max_positions_per_asset,
        sizing_policy=args.sizing_policy,
        burned_window_diagnostic=args.burned_window_diagnostic,
    )
    summary = {
        "output_dir": str(args.output_dir),
        "coverage": report["coverage"],
        "k_values": report["policy"]["k_values"],
    }
    sys.stdout.write(json.dumps(summary, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
