"""Tests for the freeze/evaluate CLI split in cex_activity_discovery_report.py
(research/cex-activity-discovery-completion-v1, colleague review 2026-09-03).

`build_report` is pure -- exercised directly against a synthetic
`CexActivityDataset`, no PostgreSQL and no filesystem. `load_dataset_from_
artifact` is exercised against a real (temp-directory) frozen artifact,
written via `cex_activity_discovery_dataset_artifact` directly -- bypassing
`freeze_dataset` itself, which is the only function allowed to touch
PostgreSQL and is out of scope for a unit test. Together these two cover
the same round trip `freeze_dataset` -> `load_dataset_from_artifact` ->
`build_report` takes in production, minus the one PostgreSQL-touching step.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from schurfer_analytics import cex_activity_discovery_dataset_artifact as dataset_artifact
from schurfer_analytics import cex_activity_discovery_report as report_module
from schurfer_analytics.cex_activity_discovery import (
    CONTROL_BOUNDARY_POLICY_VERSION,
    HYPOTHESIS_ID,
    OUTCOME_HORIZON_MINUTES,
    ExactPricePath,
    OutcomeSignalEpisode,
    PathRequest,
    signal_request,
)
from schurfer_analytics.cex_activity_discovery_report import (
    CexActivityDataset,
    build_report,
    load_dataset_from_artifact,
)
from schurfer_analytics.cex_activity_discovery_repository import CexActivityDiscoveryRepository
from schurfer_analytics.momentum_flow_bidirectional_burst_repository import (
    MomentumFlowBidirectionalBurstRepository,
)
from schurfer_analytics.momentum_flow_bidirectional_burst_study import DIRECTIONS

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

_BASE = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
_SINCE = datetime(2026, 8, 18, tzinfo=UTC)
_UNTIL = datetime(2026, 8, 27, tzinfo=UTC)


def _episode(
    episode_id: int, *, symbol: str = "TESTUSDT", direction: str = "buy"
) -> OutcomeSignalEpisode:
    trigger_at = _BASE + timedelta(minutes=episode_id)
    return OutcomeSignalEpisode(
        episode_id=episode_id,
        signal_id=f"cex_test:{episode_id}",
        source=f"cex_{direction}_burst_v1",
        exchange="bybit",
        symbol=symbol,
        direction=direction,
        trigger_at=trigger_at,
        entry_at=trigger_at + timedelta(minutes=1),
        signal_value=12.5,
    )


def _resolved_path(
    request_id: str, *, symbol: str, trigger_at: datetime, up_hit: bool = False
) -> ExactPricePath:
    entry_at = trigger_at + timedelta(minutes=1)
    return ExactPricePath(
        request_id=request_id,
        symbol=symbol,
        trigger_at=trigger_at,
        entry_at=entry_at,
        entry_price=100.0,
        observed_minutes=OUTCOME_HORIZON_MINUTES,
        max_high=130.0 if up_hit else 110.0,
        min_low=95.0,
        first_up_25_at=entry_at + timedelta(hours=3) if up_hit else None,
        first_down_25_at=None,
    )


def _dataset(
    *,
    episodes: tuple[OutcomeSignalEpisode, ...],
    signal_paths: dict[str, ExactPricePath],
    controls_by_episode: dict[int, tuple[PathRequest, ...]],
    control_paths: dict[str, ExactPricePath],
    generated_at: datetime = _BASE,
) -> CexActivityDataset:
    return CexActivityDataset(
        artifact_fingerprint="fake-fingerprint",
        manifest_generated_at=generated_at,
        database_snapshot_at=generated_at,
        since=_SINCE,
        until_exclusive=_UNTIL,
        exchange="bybit",
        market_type="linear",
        capture_version="v1",
        extreme_threshold_pct=10.0,
        refractory_minutes=60,
        min_volume_24h_usd=50_000.0,
        max_candidate_minutes=100_000,
        max_path_requests=200_000,
        candidate_extreme_minutes=7,
        episodes=episodes,
        signal_paths=signal_paths,
        controls_by_episode=controls_by_episode,
        control_paths=control_paths,
    )


# --- build_report (pure) ---------------------------------------------------


def test_build_report_reuses_the_datasets_own_generated_at_not_wall_clock() -> None:
    """The whole point of freezing generated_at/database_snapshot_at into
    the artifact's own extra metadata: two --from-artifact renders against
    the SAME fingerprint must be byte-identical, which is only true if
    build_report never reaches for a fresh wall-clock timestamp."""
    episode = _episode(1)
    dataset = _dataset(
        episodes=(episode,),
        signal_paths={},
        controls_by_episode={1: ()},
        control_paths={},
        generated_at=datetime(2026, 8, 28, 3, 0, tzinfo=UTC),
    )
    report = build_report(dataset, code_revision="deadbeef", working_tree_dirty=False)
    assert report.manifest.generated_at == datetime(2026, 8, 28, 3, 0, tzinfo=UTC)
    assert report.manifest.database_snapshot_at == datetime(2026, 8, 28, 3, 0, tzinfo=UTC)
    assert report.manifest.artifact_fingerprint == "fake-fingerprint"
    assert report.manifest.hypothesis_id == HYPOTHESIS_ID


def test_build_report_is_pure_and_deterministic() -> None:
    episode = _episode(1)
    signal_paths = {
        f"signal:{episode.episode_id}": _resolved_path(
            f"signal:{episode.episode_id}", symbol=episode.symbol, trigger_at=episode.trigger_at
        )
    }
    dataset = _dataset(
        episodes=(episode,),
        signal_paths=signal_paths,
        controls_by_episode={1: ()},
        control_paths={},
    )
    first = build_report(dataset, code_revision="deadbeef", working_tree_dirty=False)
    second = build_report(dataset, code_revision="deadbeef", working_tree_dirty=False)
    assert first == second


def test_build_report_funnel_counts_unmatched_resolved_signal_episodes() -> None:
    """An episode whose own signal resolved but has no control candidate at
    all must show up as resolved_signal_paths=1, matched_pairs=0,
    unmatched_resolved_signal_episodes=1 -- not silently vanish into the
    gap between the first two."""
    episode = _episode(1)
    request_id = signal_request(episode).request_id
    signal_paths = {
        request_id: ExactPricePath(
            request_id=request_id,
            symbol=episode.symbol,
            trigger_at=episode.trigger_at,
            entry_at=episode.entry_at,
            entry_price=100.0,
            observed_minutes=OUTCOME_HORIZON_MINUTES,
            max_high=110.0,
            min_low=95.0,
            first_up_25_at=None,
            first_down_25_at=None,
        )
    }
    dataset = _dataset(
        episodes=(episode,),
        signal_paths=signal_paths,
        controls_by_episode={1: ()},  # no control candidates at all
        control_paths={},
    )
    report = build_report(dataset, code_revision="deadbeef", working_tree_dirty=False)
    assert report.funnel.resolved_signal_paths == 1
    assert report.funnel.matched_pairs == 0
    assert report.funnel.unmatched_resolved_signal_episodes == 1


# --- load_dataset_from_artifact (real freeze, real read) -------------------


def _freeze_synthetic_cohort(directory: Path) -> tuple[str, OutcomeSignalEpisode]:
    episode = _episode(1)
    rows = dataset_artifact.build_rows(
        episodes=(episode,),
        signal_paths={},
        controls_by_episode={1: ()},
        control_paths={},
    )
    cohort = dataset_artifact.build_cohort(
        hypothesis_id=HYPOTHESIS_ID,
        since=_SINCE,
        until_exclusive=_UNTIL,
        exchange="bybit",
        market_type="linear",
        capture_version="v1",
        directions=DIRECTIONS,
        control_boundary_policy_version=CONTROL_BOUNDARY_POLICY_VERSION,
    )
    manifest = dataset_artifact.freeze(
        cohort=cohort,
        rows=rows,
        code_revision="deadbeef",
        working_tree_dirty=False,
        extra={
            "candidate_extreme_minutes": 3,
            "candidate_query_version": "test_v1",
            "path_query_version": "test_v1",
            "matching_policy_version": "test_v1",
            "extreme_threshold_pct": 10.0,
            "refractory_minutes": 60,
            "min_volume_24h_usd": 50_000.0,
            "max_candidate_minutes": 100_000,
            "max_path_requests": 200_000,
            "database_snapshot_at": _BASE.isoformat(),
        },
        directory=str(directory),
    )
    return manifest.fingerprint, episode


def test_load_dataset_from_artifact_round_trips_every_field(tmp_path: Path) -> None:
    fingerprint, episode = _freeze_synthetic_cohort(tmp_path)
    dataset = load_dataset_from_artifact(fingerprint, directory=str(tmp_path))
    assert dataset.artifact_fingerprint == fingerprint
    assert dataset.manifest_generated_at == _BASE
    assert dataset.database_snapshot_at == _BASE
    assert dataset.since == _SINCE
    assert dataset.until_exclusive == _UNTIL
    assert dataset.exchange == "bybit"
    assert dataset.market_type == "linear"
    assert dataset.capture_version == "v1"
    assert dataset.extreme_threshold_pct == 10.0
    assert dataset.refractory_minutes == 60
    assert dataset.min_volume_24h_usd == 50_000.0
    assert dataset.max_candidate_minutes == 100_000
    assert dataset.max_path_requests == 200_000
    assert dataset.candidate_extreme_minutes == 3
    assert dataset.episodes == (episode,)


def test_freeze_then_load_then_build_report_is_byte_identical_across_calls(
    tmp_path: Path,
) -> None:
    """The end-to-end contract this whole split exists for: two SEPARATE
    --from-artifact invocations against the same fingerprint must render
    the exact same report."""
    fingerprint, _episode = _freeze_synthetic_cohort(tmp_path)
    first_dataset = load_dataset_from_artifact(fingerprint, directory=str(tmp_path))
    second_dataset = load_dataset_from_artifact(fingerprint, directory=str(tmp_path))
    first_report = build_report(first_dataset, code_revision="deadbeef", working_tree_dirty=False)
    second_report = build_report(second_dataset, code_revision="deadbeef", working_tree_dirty=False)
    assert first_report == second_report


# --- main()'s --from-artifact path makes zero DB calls ---------------------


def test_main_from_artifact_never_touches_either_postgres_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The whole reason --freeze-artifact/--from-artifact are split: a
    --from-artifact render must be able to run with no DATABASE_URL at all
    and without ever constructing either PostgreSQL repository. Monkeypatch
    both repositories' from_url to raise if called at all -- if main()'s
    --from-artifact branch ever grows an "artifact missing -> fall back to
    a live query" path, this test catches it immediately."""
    fingerprint, _episode = _freeze_synthetic_cohort(tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    def _fail_from_url(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("--from-artifact must never construct a PostgreSQL repository")

    monkeypatch.setattr(CexActivityDiscoveryRepository, "from_url", staticmethod(_fail_from_url))
    monkeypatch.setattr(
        MomentumFlowBidirectionalBurstRepository, "from_url", staticmethod(_fail_from_url)
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "cex-activity-discovery-report",
            "--code-revision",
            "deadbeef",
            "--no-working-tree-dirty",
            "--from-artifact",
            fingerprint,
            "--artifact-directory",
            str(tmp_path),
            "--format",
            "json",
        ],
    )

    report_module.main()

    output = json.loads(capsys.readouterr().out)
    assert output["manifest"]["artifact_fingerprint"] == fingerprint
