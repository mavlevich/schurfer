"""One-shot bounded-memory runner for the frozen abnormal-flow v1 evaluation.

The runner verifies every registered artifact and all cold-bar manifests before it
enables the returns reader. Feature assembly and control selection are streamed by
instrument; only independent primary episodes and bounded matched-control sets remain
in memory. A run id is claimed atomically and is terminal on success or failure.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass, fields
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Protocol

from .abnormal_flow_input_audit import verified_input
from .abnormal_flow_replay import (
    EvaluationManifest,
    Funnel,
    RouteKey,
    _decision_eligible,
    assemble_decisions,
    build_funnel,
    control_band_key,
    evaluate_outcomes,
    form_episodes,
    iter_instrument_bars,
    oi_freshness_limit_for,
    parquet_outcome_reader,
    primary_cell_fires,
    select_portfolio,
)
from .abnormal_flow_scan import IdentityResolver, load_identity_resolver
from .abnormal_flow_screen import AbnormalFlowContract

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from .abnormal_flow_replay import DecisionFeatures

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# Anchors to the artifacts frozen and merged in PR #435. A different contract or
# evaluation manifest requires a reviewed code change, not a different CLI path.
REGISTERED_CONTRACT_FILE_HASH: Final = (
    "sha256:36502eeb4ffb63cb2d0eead5e81c97947c97130a85ac046e6d2759459e492256"
)
REGISTERED_EVALUATION_MANIFEST_FILE_HASH: Final = (
    "sha256:7f4d3f58044d84898b40c2c5bcedc90a6173340a93f3d6e07e09c3e5cf6f2b8d"
)


class GitStateProvider(Protocol):
    def get_revision(self) -> str: ...

    def is_dirty(self) -> bool: ...


class RealGitState(GitStateProvider):
    @staticmethod
    def _git() -> str:
        executable = shutil.which("git")
        if executable is None:
            raise RuntimeError("git executable is unavailable")
        return executable

    def get_revision(self) -> str:
        return subprocess.check_output(  # noqa: S603 -- resolved git binary, fixed argv
            [self._git(), "rev-parse", "HEAD"], text=True
        ).strip()

    def is_dirty(self) -> bool:
        return bool(
            subprocess.check_output(  # noqa: S603 -- resolved git binary, fixed argv
                [self._git(), "status", "--porcelain"], text=True
            ).strip()
        )


def _hash_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return f"sha256:{hasher.hexdigest()}"


def _require_hash(path: Path, expected: str, label: str) -> str:
    observed = _hash_file(path)
    if observed != expected:
        raise ValueError(f"{label} hash mismatch: expected {expected}, got {observed}")
    return observed


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    parsed = json.loads(path.read_bytes())
    if not isinstance(parsed, dict):
        raise ValueError(f"{label} must be a JSON object")
    return parsed


def _load_registered_contract(path: Path) -> tuple[AbnormalFlowContract, str, str]:
    file_hash = _require_hash(path, REGISTERED_CONTRACT_FILE_HASH, "registered contract file")
    raw = _read_json_object(path, "registered contract")
    embedded_hash = raw.pop("contract_hash", None)
    if not isinstance(embedded_hash, str):
        raise ValueError("registered contract is missing contract_hash")
    contract = AbnormalFlowContract(**raw)
    contract.require_frozen()
    computed_hash = contract.compute_hash()
    if embedded_hash != computed_hash:
        raise ValueError(
            f"registered contract hash mismatch: embedded {embedded_hash}, computed {computed_hash}"
        )
    return contract, computed_hash, file_hash


def _load_registered_evaluation_manifest(path: Path) -> tuple[EvaluationManifest, str]:
    file_hash = _require_hash(
        path,
        REGISTERED_EVALUATION_MANIFEST_FILE_HASH,
        "registered evaluation manifest file",
    )
    raw = _read_json_object(path, "registered evaluation manifest")
    expected_fields = {field.name for field in fields(EvaluationManifest)}
    if set(raw) != expected_fields:
        raise ValueError(
            "registered evaluation manifest fields mismatch: "
            f"expected {sorted(expected_fields)}, got {sorted(raw)}"
        )
    return EvaluationManifest(**raw), file_hash


def _verify_registered_inputs(
    manifest: EvaluationManifest,
    *,
    scan_manifest_path: Path,
    identity_snapshot_path: Path,
    funding_snapshot_path: Path,
    funding_settlements_path: Path,
    candidate_table_path: Path,
) -> dict[str, str]:
    observed = {
        "scan_manifest": _hash_file(scan_manifest_path),
        "identity_snapshot": _hash_file(identity_snapshot_path),
        "candidate_table": _hash_file(candidate_table_path),
        "funding_snapshot": _hash_file(funding_snapshot_path),
        "funding_settlements": _hash_file(funding_settlements_path),
    }
    expected = {
        "scan_manifest": manifest.input_audit_fingerprint,
        "identity_snapshot": manifest.identity_snapshot_hash,
        "candidate_table": manifest.candidate_table_version,
        "funding_snapshot": manifest.funding_snapshot_hash,
        "funding_settlements": manifest.funding_settlements_hash,
    }
    mismatches = [
        f"{name}: expected {expected[name]}, got {observed[name]}"
        for name in expected
        if observed[name] != expected[name]
    ]
    if mismatches:
        raise ValueError("registered input hash mismatch: " + "; ".join(mismatches))
    return observed


def _parse_scan_days(path: Path) -> dict[str, dict[str, Any]]:
    raw = _read_json_object(path, "scan manifest")
    days = raw.get("days")
    if not isinstance(days, list):
        raise ValueError("scan manifest days must be a list")
    parsed: dict[str, dict[str, Any]] = {}
    for item in days:
        if not isinstance(item, dict) or not isinstance(item.get("day"), str):
            raise ValueError("scan manifest contains an invalid day record")
        day = str(item["day"])
        if day in parsed:
            raise ValueError(f"scan manifest contains duplicate day {day}")
        parsed[day] = item
    return parsed


@dataclass(frozen=True)
class DependencyBounds:
    feature_start: datetime
    outcome_end_exclusive: datetime
    first_day: date
    day_end_exclusive: date


def dependency_bounds(contract: AbnormalFlowContract) -> DependencyBounds:
    """Return the exact bars needed by the registered minute-grid replay."""

    assert contract.window_start_utc and contract.window_end_utc
    assert contract.scan_lag_minutes is not None
    start = datetime.fromisoformat(contract.window_start_utc.replace("Z", "+00:00"))
    end = datetime.fromisoformat(contract.window_end_utc.replace("Z", "+00:00"))
    # decision = end_bar + 1m + scan_lag; the feature includes `lookback`
    # preceding bars in addition to end_bar.
    feature_start = start - timedelta(
        minutes=contract.lookback_minutes + 1 + contract.scan_lag_minutes
    )
    # Latest decision is end-1m; its entry is end and its exit bucket is
    # end+horizon. Include that entire one-minute exit bucket.
    outcome_end_exclusive = end + timedelta(minutes=contract.outcome_horizon_minutes + 1)
    if outcome_end_exclusive.timetz().replace(tzinfo=None) == time.min:
        day_end_exclusive = outcome_end_exclusive.date()
    else:
        day_end_exclusive = outcome_end_exclusive.date() + timedelta(days=1)
    return DependencyBounds(
        feature_start=feature_start,
        outcome_end_exclusive=outcome_end_exclusive,
        first_day=feature_start.date(),
        day_end_exclusive=day_end_exclusive,
    )


def _verified_cold_bar_paths(
    cold_bars_dir: Path,
    scan_manifest_path: Path,
    bounds: DependencyBounds,
    *,
    progress: Callable[[int, int, date], None] | None = None,
) -> list[str]:
    scan_days = _parse_scan_days(scan_manifest_path)
    verified_paths: list[str] = []
    total_days = (bounds.day_end_exclusive - bounds.first_day).days
    day = bounds.first_day
    while day < bounds.day_end_exclusive:
        expected = scan_days.get(day.isoformat())
        if expected is None:
            raise ValueError(f"missing dependency day {day} in registered scan manifest")
        path, manifest = verified_input(cold_bars_dir, day)
        if manifest.sha256 != expected.get("sha256"):
            raise ValueError(f"cold-bar sha256 mismatch for {day}")
        if manifest.source_fingerprint != expected.get("source_fingerprint"):
            raise ValueError(f"cold-bar source fingerprint mismatch for {day}")
        verified_paths.append(str(path))
        if progress is not None:
            progress(len(verified_paths), total_days, day)
        day += timedelta(days=1)
    return verified_paths


def merge_funnels(left: Funnel, right: Funnel) -> Funnel:
    merged = Funnel()
    for field in (
        "scanned",
        "unavailable_feature",
        "ineligible",
        "eligible",
        "primary_fires",
        "no_oi_fires",
        "no_buy_fires",
        "no_containment_fires",
        "p99_fires",
        "sub_p99_fires",
        "primary_episodes",
        "no_oi_episodes",
        "no_buy_episodes",
        "no_containment_episodes",
        "p99_episodes",
        "sub_p99_episodes",
    ):
        setattr(merged, field, getattr(left, field) + getattr(right, field))
    merged.reasons = {
        key: left.reasons.get(key, 0) + right.reasons.get(key, 0)
        for key in set(left.reasons) | set(right.reasons)
    }
    return merged


def _controls_for_episodes(
    contract: AbnormalFlowContract,
    episodes: Sequence[DecisionFeatures],
    verified_paths: Sequence[str],
    *,
    feature_start: datetime,
    evaluation_start: datetime,
    evaluation_end: datetime,
    resolver: IdentityResolver,
) -> dict[RouteKey, list[DecisionFeatures]]:
    episodes_by_band: dict[tuple[str, str, int, int], list[DecisionFeatures]] = defaultdict(list)
    controls: dict[RouteKey, list[DecisionFeatures]] = {ep.route_key(): [] for ep in episodes}
    for episode in episodes:
        band = control_band_key(episode)
        if band is not None:
            episodes_by_band[band].append(episode)

    max_controls = contract.controls_per_episode or 0
    for bars in iter_instrument_bars(
        list(verified_paths), window_start=feature_start, window_end=evaluation_end
    ):
        if not bars:
            continue
        first = bars[0]
        freshness = oi_freshness_limit_for(contract, first.exchange)
        if freshness is None:
            continue

        def resolve(
            at: datetime,
            exchange: str = first.exchange,
            market_type: str = first.market_type,
            native_market_id: str = first.native_market_id,
            capture_version: str = first.capture_version,
        ) -> str | None:
            return resolver.identity_key(
                exchange,
                market_type,
                native_market_id,
                capture_version,
                at,
            )

        decisions = assemble_decisions(
            bars,
            scan_lag_minutes=contract.scan_lag_minutes or 0,
            entry_execution_window_minutes=contract.entry_execution_window_minutes or 0,
            oi_freshness_limit_seconds=freshness,
            resolve_canonical=resolve,
        )
        for candidate in decisions:
            if not (evaluation_start <= candidate.decision_at < evaluation_end):
                continue
            if not _decision_eligible(contract, candidate) or primary_cell_fires(
                contract, candidate
            ):
                continue
            band = control_band_key(candidate)
            if band is None:
                continue
            for episode in episodes_by_band.get(band, ()):
                if (
                    candidate.decision_at == episode.decision_at
                    and candidate.symbol == episode.symbol
                ):
                    continue
                selected = controls[episode.route_key()]
                selected.append(candidate)
                selected.sort(
                    key=lambda item: (
                        abs((item.decision_at - episode.decision_at).total_seconds()),
                        item.symbol,
                    )
                )
                del selected[max_controls:]
    return controls


_FORMAL_CAPABILITY_TOKEN: Final = object()


class FormalCapability:
    """Token issued only after explicit CLI consent."""

    def __init__(self, token: object) -> None:
        if token is not _FORMAL_CAPABILITY_TOKEN:
            raise ValueError("formal capability can only be issued by the formal CLI")
        self._token = token


def _authorize_formal_run(enabled: bool) -> FormalCapability:
    if not enabled:
        raise ValueError("--formal-run is required")
    return FormalCapability(_FORMAL_CAPABILITY_TOKEN)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("x") as stream:
        json.dump(payload, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


class FormalRunner:
    def __init__(self, git_state: GitStateProvider) -> None:
        self.git_state = git_state

    def run(
        self,
        capability: FormalCapability,
        contract_path: Path,
        evaluation_manifest_path: Path,
        scan_manifest_path: Path,
        identity_snapshot_path: Path,
        funding_snapshot_path: Path,
        funding_settlements_path: Path,
        candidate_table_path: Path,
        cold_bars_dir: Path,
        output_dir: Path,
    ) -> dict[str, Any]:
        if (
            not isinstance(capability, FormalCapability)
            or capability._token is not _FORMAL_CAPABILITY_TOKEN
        ):
            raise ValueError("formal CLI capability required")
        if self.git_state.is_dirty():
            raise ValueError("dirty tree detected")
        revision = self.git_state.get_revision()

        contract, contract_hash, contract_file_hash = _load_registered_contract(contract_path)
        evaluation_manifest, evaluation_manifest_file_hash = _load_registered_evaluation_manifest(
            evaluation_manifest_path
        )
        observed_hashes = _verify_registered_inputs(
            evaluation_manifest,
            scan_manifest_path=scan_manifest_path,
            identity_snapshot_path=identity_snapshot_path,
            funding_snapshot_path=funding_snapshot_path,
            funding_settlements_path=funding_settlements_path,
            candidate_table_path=candidate_table_path,
        )
        evaluation_fingerprint = evaluation_manifest.compute_fingerprint()
        if evaluation_fingerprint != contract.input_fingerprint:
            raise ValueError(
                "evaluation fingerprint mismatch: "
                f"expected {contract.input_fingerprint}, got {evaluation_fingerprint}"
            )

        run_key = (
            f"{contract_hash.removeprefix('sha256:')}-"
            f"{evaluation_fingerprint.removeprefix('sha256:')}"
        )
        run_dir = output_dir / run_key
        try:
            run_dir.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            if (run_dir / "formal_run_failed.json").exists():
                raise ValueError(f"terminal failed formal run already exists: {run_key}") from None
            raise ValueError(f"formal run already exists: {run_key}") from None

        try:
            resolver, identity_hash = load_identity_resolver(identity_snapshot_path)
            if identity_hash != evaluation_manifest.identity_snapshot_hash:
                raise ValueError("identity resolver did not reproduce the registered hash")

            assert contract.window_start_utc and contract.window_end_utc
            evaluation_start = datetime.fromisoformat(
                contract.window_start_utc.replace("Z", "+00:00")
            )
            evaluation_end = datetime.fromisoformat(contract.window_end_utc.replace("Z", "+00:00"))
            bounds = dependency_bounds(contract)
            verified_paths = _verified_cold_bar_paths(cold_bars_dir, scan_manifest_path, bounds)

            logging.info("Pass 1/2: assembling funnel and primary episodes")
            funnel = Funnel()
            primary_fires: list[DecisionFeatures] = []
            for bars in iter_instrument_bars(
                verified_paths,
                window_start=bounds.feature_start,
                window_end=evaluation_end,
            ):
                if not bars:
                    continue
                first = bars[0]
                freshness = oi_freshness_limit_for(contract, first.exchange)
                if freshness is None:
                    continue

                def resolve(
                    at: datetime,
                    exchange: str = first.exchange,
                    market_type: str = first.market_type,
                    native_market_id: str = first.native_market_id,
                    capture_version: str = first.capture_version,
                ) -> str | None:
                    return resolver.identity_key(
                        exchange,
                        market_type,
                        native_market_id,
                        capture_version,
                        at,
                    )

                decisions = assemble_decisions(
                    bars,
                    scan_lag_minutes=contract.scan_lag_minutes or 0,
                    entry_execution_window_minutes=contract.entry_execution_window_minutes or 0,
                    oi_freshness_limit_seconds=freshness,
                    resolve_canonical=resolve,
                )
                decisions = [
                    decision
                    for decision in decisions
                    if evaluation_start <= decision.decision_at < evaluation_end
                ]
                funnel = merge_funnels(funnel, build_funnel(contract, decisions))
                primary_fires.extend(
                    decision
                    for decision in decisions
                    if _decision_eligible(contract, decision)
                    and primary_cell_fires(contract, decision)
                )

            episodes = form_episodes(primary_fires, contract.cooldown_minutes)
            # Per-instrument funnels cannot deduplicate two native routes that resolve
            # to the same point-in-time asset. The formal primary count is the global
            # episode set actually evaluated.
            funnel.primary_episodes = len(episodes)
            selected_episodes, skipped_capacity = select_portfolio(contract, episodes)

            logging.info("Pass 2/2: selecting bounded matched controls")
            controls_by_episode = _controls_for_episodes(
                contract,
                episodes,
                verified_paths,
                feature_start=bounds.feature_start,
                evaluation_start=evaluation_start,
                evaluation_end=evaluation_end,
                resolver=resolver,
            )

            # Re-check immediately before the only returns-bearing operation.
            if self.git_state.is_dirty() or self.git_state.get_revision() != revision:
                raise ValueError("git state changed before the returns read")

            requested: dict[RouteKey, DecisionFeatures] = {
                episode.route_key(): episode for episode in episodes
            }
            for controls in controls_by_episode.values():
                requested.update((control.route_key(), control) for control in controls)

            from . import abnormal_flow_replay as replay_module

            replay_module.FORMAL_RETURNS_RUN_ENABLED = True
            try:
                outcomes = parquet_outcome_reader(
                    verified_paths,
                    outcome_horizon_minutes=contract.outcome_horizon_minutes,
                )(list(requested.values()))
                replay, _records = evaluate_outcomes(
                    contract,
                    episodes,
                    controls_by_episode,
                    outcomes,
                    selected_episodes,
                    skipped_capacity,
                    funnel,
                )
            finally:
                replay_module.FORMAL_RETURNS_RUN_ENABLED = False

            report = {
                "run_version": "abnormal_flow_formal_runner_v1",
                "revision": revision,
                "clean_tree": True,
                "contract_hash": contract_hash,
                "evaluation_fingerprint": evaluation_fingerprint,
                "artifact_hashes": {
                    "contract_file": contract_file_hash,
                    "evaluation_manifest_file": evaluation_manifest_file_hash,
                    **observed_hashes,
                },
                "bounds": {
                    "dependency_start": bounds.feature_start.isoformat(),
                    "dependency_end_exclusive": bounds.outcome_end_exclusive.isoformat(),
                    "evaluation_start": evaluation_start.isoformat(),
                    "evaluation_end_exclusive": evaluation_end.isoformat(),
                },
                "funnel": asdict(replay.funnel),
                "replay": {
                    "version": replay.replay_version,
                    "resolved_episodes": replay.resolved_episodes,
                    "unresolved_episodes": replay.unresolved_episodes,
                    "episodes_with_matched_control": replay.episodes_with_matched_control,
                    "resolved_controls": replay.resolved_controls,
                    "unresolved_controls": replay.unresolved_controls,
                },
                "economics": asdict(replay.report),
                "verdict": replay.report.verdict,
            }
            _atomic_json(run_dir / "formal_run_report.json", report)
            return report
        except Exception as exc:
            _atomic_json(
                run_dir / "formal_run_failed.json",
                {
                    "run_version": "abnormal_flow_formal_runner_v1",
                    "revision": revision,
                    "contract_hash": contract_hash,
                    "evaluation_fingerprint": evaluation_fingerprint,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
            )
            raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--formal-run", action="store_true")
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--evaluation-manifest", type=Path, required=True)
    parser.add_argument("--scan-manifest", type=Path, required=True)
    parser.add_argument("--identity-snapshot", type=Path, required=True)
    parser.add_argument("--funding-snapshot", type=Path, required=True)
    parser.add_argument("--funding-settlements", type=Path, required=True)
    parser.add_argument("--candidate-table", type=Path, required=True)
    parser.add_argument("--cold-bars-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        capability = _authorize_formal_run(args.formal_run)
    except ValueError as exc:
        logging.error("%s", exc)
        sys.exit(2)
    FormalRunner(RealGitState()).run(
        capability,
        args.contract,
        args.evaluation_manifest,
        args.scan_manifest,
        args.identity_snapshot,
        args.funding_snapshot,
        args.funding_settlements,
        args.candidate_table,
        args.cold_bars_dir,
        args.output_dir,
    )


if __name__ == "__main__":
    main()
