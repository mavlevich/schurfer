"""Build or reuse an outcome-blind abnormal-flow replay snapshot.

This command never imports or calls the returns reader. It verifies daily cold-bar
artifacts, assembles point-in-time decisions, forms episodes, selects matched controls,
and publishes the result under a content-derived fingerprint. A cache hit verifies the
published bundle and returns without scanning Parquet rows again.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from datetime import time as datetime_time
from pathlib import Path
from typing import Any, Final

from .abnormal_flow_input_audit import verified_input
from .abnormal_flow_replay import (
    DecisionFeatures,
    _decision_eligible,
    assemble_decisions,
    control_band_key,
    form_episodes,
    iter_instrument_bars,
    oi_freshness_limit_for,
    primary_cell_fires,
)
from .abnormal_flow_scan import load_identity_resolver
from .abnormal_flow_screen import AbnormalFlowContract
from .abnormal_flow_snapshots import (
    ColdBarInput,
    SnapshotIdentity,
    SnapshotPublishOutcome,
    SnapshotReader,
    SnapshotWriter,
    decision_id,
    snapshot_directory,
)

PIPELINE_VERSION: Final = "abnormal_flow_outcome_blind_snapshot_pipeline_v1"


@dataclass(frozen=True)
class SnapshotBounds:
    feature_start: datetime
    evaluation_start: datetime
    evaluation_end: datetime
    first_day: date
    day_end_exclusive: date


@dataclass(frozen=True)
class SnapshotBuildResult:
    fingerprint: str
    snapshot_dir: str
    cache_hit: bool
    decisions: int
    episodes: int
    controls: int
    cold_bar_rows: int
    verify_seconds: float
    scan_seconds: float
    controls_seconds: float
    publish_seconds: float
    total_seconds: float

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True) + "\n"


def snapshot_bounds(contract: AbnormalFlowContract) -> SnapshotBounds:
    if not contract.window_start_utc or not contract.window_end_utc:
        raise ValueError("contract evaluation bounds are required")
    if contract.scan_lag_minutes is None:
        raise ValueError("contract scan lag is required")
    evaluation_start = datetime.fromisoformat(contract.window_start_utc.replace("Z", "+00:00"))
    evaluation_end = datetime.fromisoformat(contract.window_end_utc.replace("Z", "+00:00"))
    feature_start = evaluation_start - timedelta(
        minutes=contract.lookback_minutes + 1 + contract.scan_lag_minutes
    )
    if evaluation_end.timetz().replace(tzinfo=None) == datetime_time.min:
        day_end_exclusive = evaluation_end.date()
    else:
        day_end_exclusive = evaluation_end.date() + timedelta(days=1)
    return SnapshotBounds(
        feature_start=feature_start,
        evaluation_start=evaluation_start,
        evaluation_end=evaluation_end,
        first_day=feature_start.date(),
        day_end_exclusive=day_end_exclusive,
    )


def _load_contract(path: Path) -> tuple[AbnormalFlowContract, str]:
    raw_bytes = path.read_bytes()
    parsed = json.loads(raw_bytes)
    if not isinstance(parsed, dict):
        raise ValueError("contract must be a JSON object")
    data = dict(parsed)
    embedded_hash = data.pop("contract_hash", None)
    contract = AbnormalFlowContract(**data)
    contract.require_frozen()
    computed_hash = contract.compute_hash()
    if embedded_hash is not None and embedded_hash != computed_hash:
        raise ValueError(
            f"contract hash mismatch: embedded {embedded_hash}, computed {computed_hash}"
        )
    return contract, computed_hash


def _verified_inputs(
    cold_bars_dir: Path, bounds: SnapshotBounds
) -> tuple[list[str], tuple[ColdBarInput, ...]]:
    paths: list[str] = []
    inputs: list[ColdBarInput] = []
    day = bounds.first_day
    while day < bounds.day_end_exclusive:
        path, manifest = verified_input(cold_bars_dir, day)
        paths.append(str(path.resolve()))
        inputs.append(
            ColdBarInput(
                day=manifest.day,
                file_name=manifest.file_name,
                row_count=manifest.row_count,
                sha256=manifest.sha256,
                source_fingerprint=manifest.source_fingerprint or "",
            )
        )
        day += timedelta(days=1)
    return paths, tuple(inputs)


def _identity(
    contract: AbnormalFlowContract,
    contract_hash: str,
    identity_hash: str,
    bounds: SnapshotBounds,
    cold_bars: tuple[ColdBarInput, ...],
) -> SnapshotIdentity:
    return SnapshotIdentity(
        pipeline_version=PIPELINE_VERSION,
        contract_hash=contract_hash,
        contract_version=contract.contract_version,
        identity_snapshot_hash=identity_hash,
        dependency_start=bounds.feature_start.isoformat(),
        evaluation_start=bounds.evaluation_start.isoformat(),
        evaluation_end_exclusive=bounds.evaluation_end.isoformat(),
        cold_bars=cold_bars,
    )


def _select_controls(
    contract: AbnormalFlowContract,
    episodes: list[DecisionFeatures],
    decisions: Any,
    *,
    evaluation_start: datetime,
    evaluation_end: datetime,
) -> dict[str, list[DecisionFeatures]]:
    episodes_by_band: dict[tuple[str, str, int, int], list[DecisionFeatures]] = defaultdict(list)
    selected: dict[str, list[DecisionFeatures]] = {decision_id(episode): [] for episode in episodes}
    for episode in episodes:
        band = control_band_key(episode)
        if band is not None:
            episodes_by_band[band].append(episode)

    max_controls = contract.controls_per_episode or 0
    for candidate in decisions:
        if not (evaluation_start <= candidate.decision_at < evaluation_end):
            continue
        if not _decision_eligible(contract, candidate) or primary_cell_fires(contract, candidate):
            continue
        band = control_band_key(candidate)
        if band is None:
            continue
        for episode in episodes_by_band.get(band, ()):
            if candidate.decision_at == episode.decision_at and candidate.symbol == episode.symbol:
                continue
            candidates = selected[decision_id(episode)]
            candidates.append(candidate)
            candidates.sort(
                key=lambda item: (
                    abs((item.decision_at - episode.decision_at).total_seconds()),
                    item.symbol,
                )
            )
            del candidates[max_controls:]
    return selected


def _git_state() -> tuple[str, bool]:
    git = shutil.which("git")
    if git is None:
        return "unknown", True
    revision = subprocess.check_output(  # noqa: S603 - resolved executable, fixed argv
        [git, "rev-parse", "HEAD"], text=True
    ).strip()
    dirty = bool(
        subprocess.check_output(  # noqa: S603 - resolved executable, fixed argv
            [git, "status", "--porcelain"], text=True
        ).strip()
    )
    return revision, dirty


def build_or_reuse_snapshot(
    *,
    contract_path: Path,
    identity_snapshot_path: Path,
    cold_bars_dir: Path,
    artifact_root: Path,
    code_revision: str,
    working_tree_dirty: bool,
    memory_limit: str = "512MB",
    threads: int = 2,
) -> SnapshotBuildResult:
    total_started = time.perf_counter()
    verify_started = time.perf_counter()
    contract, contract_hash = _load_contract(contract_path)
    resolver, identity_hash = load_identity_resolver(identity_snapshot_path)
    bounds = snapshot_bounds(contract)
    paths, cold_bars = _verified_inputs(cold_bars_dir, bounds)
    identity = _identity(contract, contract_hash, identity_hash, bounds, cold_bars)
    fingerprint = identity.fingerprint()
    verify_seconds = time.perf_counter() - verify_started

    final_dir = snapshot_directory(artifact_root, fingerprint)
    if final_dir.exists():
        reader = SnapshotReader(final_dir, fingerprint)
        manifest = reader.manifest
        return SnapshotBuildResult(
            fingerprint=fingerprint,
            snapshot_dir=str(final_dir),
            cache_hit=True,
            decisions=manifest.artifacts["decisions"].row_count,
            episodes=manifest.artifacts["episodes"].row_count,
            controls=manifest.artifacts["controls"].row_count,
            cold_bar_rows=sum(item.row_count for item in cold_bars),
            verify_seconds=verify_seconds,
            scan_seconds=0.0,
            controls_seconds=0.0,
            publish_seconds=0.0,
            total_seconds=time.perf_counter() - total_started,
        )

    with SnapshotWriter(
        artifact_root,
        identity,
        code_revision=code_revision,
        working_tree_dirty=working_tree_dirty,
        memory_limit=memory_limit,
        threads=threads,
    ) as writer:
        scan_started = time.perf_counter()
        primary_fires: list[DecisionFeatures] = []
        for bars in iter_instrument_bars(
            paths,
            window_start=bounds.feature_start,
            window_end=bounds.evaluation_end,
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
            writer.append_decisions(decisions)
            primary_fires.extend(
                decision
                for decision in decisions
                if bounds.evaluation_start <= decision.decision_at < bounds.evaluation_end
                and _decision_eligible(contract, decision)
                and primary_cell_fires(contract, decision)
            )
        episodes = form_episodes(primary_fires, contract.cooldown_minutes)
        writer.append_episodes(episodes)
        scan_seconds = time.perf_counter() - scan_started

        controls_started = time.perf_counter()
        controls_by_episode = _select_controls(
            contract,
            episodes,
            writer.iter_decisions(),
            evaluation_start=bounds.evaluation_start,
            evaluation_end=bounds.evaluation_end,
        )
        for episode in episodes:
            writer.append_controls(episode, controls_by_episode[decision_id(episode)])
        controls_seconds = time.perf_counter() - controls_started

        publish_started = time.perf_counter()
        published = writer.publish()
        publish_seconds = time.perf_counter() - publish_started

    manifest = published.manifest
    return SnapshotBuildResult(
        fingerprint=fingerprint,
        snapshot_dir=str(published.directory),
        cache_hit=published.outcome is SnapshotPublishOutcome.ALREADY_EXISTS,
        decisions=manifest.artifacts["decisions"].row_count,
        episodes=manifest.artifacts["episodes"].row_count,
        controls=manifest.artifacts["controls"].row_count,
        cold_bar_rows=sum(item.row_count for item in cold_bars),
        verify_seconds=verify_seconds,
        scan_seconds=scan_seconds,
        controls_seconds=controls_seconds,
        publish_seconds=publish_seconds,
        total_seconds=time.perf_counter() - total_started,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--identity-snapshot", type=Path, required=True)
    parser.add_argument("--cold-bars-dir", type=Path, required=True)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--benchmark-out", type=Path)
    parser.add_argument("--memory-limit", default="512MB")
    parser.add_argument("--threads", type=int, default=2)
    return parser


def main() -> None:
    args: Any = build_parser().parse_args()
    revision, dirty = _git_state()
    result = build_or_reuse_snapshot(
        contract_path=args.contract,
        identity_snapshot_path=args.identity_snapshot,
        cold_bars_dir=args.cold_bars_dir,
        artifact_root=args.artifact_root,
        code_revision=revision,
        working_tree_dirty=dirty,
        memory_limit=args.memory_limit,
        threads=args.threads,
    )
    output = result.to_json()
    if args.benchmark_out:
        args.benchmark_out.write_text(output)
    sys.stdout.write(output)


if __name__ == "__main__":
    main()
