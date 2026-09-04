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

import pytest
from schurfer_analytics import cex_activity_discovery_dataset_artifact as dataset_artifact
from schurfer_analytics import cex_activity_discovery_report as report_module
from schurfer_analytics.cex_activity_discovery import (
    CONTROL_BOUNDARY_POLICY_VERSION,
    HYPOTHESIS_ID,
    OUTCOME_HORIZON_MINUTES,
    ExactPricePath,
    IncompatibleResearchContractError,
    OutcomeSignalEpisode,
    PathRequest,
    contract_fingerprint,
    signal_request,
)
from schurfer_analytics.cex_activity_discovery_report import (
    CANDIDATE_QUERY_VERSION,
    CexActivityDataset,
    build_report,
    load_dataset_from_artifact,
)
from schurfer_analytics.cex_activity_discovery_repository import (
    PATH_QUERY_VERSION,
    CexActivityDiscoveryRepository,
)
from schurfer_analytics.momentum_flow_bidirectional_burst_repository import (
    MomentumFlowBidirectionalBurstRepository,
)
from schurfer_analytics.momentum_flow_bidirectional_burst_study import DIRECTIONS

if TYPE_CHECKING:
    from pathlib import Path

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


_LIVE_CONTRACT_FINGERPRINT = contract_fingerprint(
    candidate_query_version=CANDIDATE_QUERY_VERSION,
    path_query_version=PATH_QUERY_VERSION,
    extreme_threshold_pct=10.0,
    refractory_minutes=60,
    min_volume_24h_usd=50_000.0,
)


def _dataset(
    *,
    episodes: tuple[OutcomeSignalEpisode, ...],
    signal_paths: dict[str, ExactPricePath],
    controls_by_episode: dict[int, tuple[PathRequest, ...]],
    control_paths: dict[str, ExactPricePath],
    generated_at: datetime = _BASE,
    contract_fingerprint_override: str | None = None,
) -> CexActivityDataset:
    return CexActivityDataset(
        artifact_fingerprint="fake-fingerprint",
        artifact_code_revision="frozen-deadbeef",
        artifact_working_tree_dirty=False,
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
        candidate_query_version=CANDIDATE_QUERY_VERSION,
        path_query_version=PATH_QUERY_VERSION,
        contract_fingerprint=contract_fingerprint_override or _LIVE_CONTRACT_FINGERPRINT,
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


# --- contract_fingerprint gate (colleague review, 2026-09-04) --------------


def test_build_report_rejects_a_dataset_frozen_under_a_different_contract() -> None:
    """The core of finding [P1] "artifact does not pin the full research
    contract": a dataset whose own contract_fingerprint does not match
    what the CURRENT code's own constants would produce must never be
    silently evaluated with live matching/bootstrap/floor logic -- that
    would compute a real verdict from stale frozen data under a contract
    that no longer matches what the report claims to be running."""
    episode = _episode(1)
    dataset = _dataset(
        episodes=(episode,),
        signal_paths={},
        controls_by_episode={1: ()},
        control_paths={},
        contract_fingerprint_override="a" * 64,
    )
    with pytest.raises(IncompatibleResearchContractError, match="research contract"):
        build_report(dataset, code_revision="deadbeef", working_tree_dirty=False)


def test_build_report_surfaces_freeze_vs_render_code_revision_separately() -> None:
    episode = _episode(1)
    dataset = _dataset(
        episodes=(episode,), signal_paths={}, controls_by_episode={1: ()}, control_paths={}
    )
    report = build_report(dataset, code_revision="render-code", working_tree_dirty=True)
    assert report.manifest.code_revision == "render-code"
    assert report.manifest.working_tree_dirty is True
    assert report.manifest.artifact_code_revision == "frozen-deadbeef"
    assert report.manifest.artifact_working_tree_dirty is False
    assert report.manifest.contract_fingerprint == _LIVE_CONTRACT_FINGERPRINT


# --- unresolved-by-reason funnel breakdown (colleague review, 2026-09-04) --


def test_build_report_funnel_breaks_down_unresolved_paths_by_reason_and_side() -> None:
    """Missingness is classified (ExactPricePath.unresolved_reason) but was
    previously invisible in the formal report -- the funnel must show WHY
    and on WHICH SIDE observations were missing, and resolved + every
    reason must reconcile exactly to the total request count on each side
    (asserted inside build_report itself; re-checked here as the public
    contract this test is meant to lock in)."""
    signal_episode = _episode(1, symbol="AUSDT")
    control_only_episode = _episode(2, symbol="BUSDT")
    # signal_episode's own signal path is present but has no entry bar.
    signal_request_id = signal_request(signal_episode).request_id
    signal_paths = {
        signal_request_id: ExactPricePath(
            request_id=signal_request_id,
            symbol=signal_episode.symbol,
            trigger_at=signal_episode.trigger_at,
            entry_at=signal_episode.entry_at,
            entry_price=None,
            observed_minutes=0,
            max_high=None,
            min_low=None,
            first_up_25_at=None,
            first_down_25_at=None,
        )
        # control_only_episode's own signal request has NO row at all --
        # missing_path_result, not present in the dict.
    }
    control_request = PathRequest(
        "control:2:p1",
        control_only_episode.symbol,
        control_only_episode.trigger_at,
        control_only_episode.entry_at,
    )
    control_paths = {
        # control_request's own path is present but has an invalid extremum.
        control_request.request_id: ExactPricePath(
            request_id=control_request.request_id,
            symbol=control_only_episode.symbol,
            trigger_at=control_request.trigger_at,
            entry_at=control_request.entry_at,
            entry_price=100.0,
            observed_minutes=OUTCOME_HORIZON_MINUTES,
            max_high=-5.0,
            min_low=95.0,
            first_up_25_at=None,
            first_down_25_at=None,
        )
    }
    dataset = _dataset(
        episodes=(signal_episode, control_only_episode),
        signal_paths=signal_paths,
        controls_by_episode={1: (), 2: (control_request,)},
        control_paths=control_paths,
    )
    report = build_report(dataset, code_revision="deadbeef", working_tree_dirty=False)
    assert report.funnel.signal_unresolved_by_reason == {
        "missing_entry_bar": 1,
        "missing_path_result": 1,
    }
    assert report.funnel.control_unresolved_by_reason == {"invalid_extrema": 1}
    # Reconciliation: 2 signal requests total, 0 resolved, 2 unresolved (1+1).
    assert report.funnel.resolved_signal_paths == 0
    # 1 control request total, 0 resolved, 1 unresolved.
    assert report.funnel.resolved_control_paths == 0


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
        # The REAL live constants, not placeholder values -- these feed
        # _LIVE_CONTRACT_FINGERPRINT above, and build_report's own contract
        # check compares against the CURRENT code's live constants, so a
        # synthetic cohort meant to round-trip through build_report (not
        # just load_dataset_from_artifact) must actually match them.
        extreme_threshold_pct=10.0,
        refractory_minutes=60,
        min_volume_24h_usd=50_000.0,
        contract_fingerprint=_LIVE_CONTRACT_FINGERPRINT,
    )
    manifest = dataset_artifact.freeze(
        cohort=cohort,
        rows=rows,
        code_revision="deadbeef",
        working_tree_dirty=False,
        extra={
            "candidate_extreme_minutes": 3,
            "candidate_query_version": CANDIDATE_QUERY_VERSION,
            "path_query_version": PATH_QUERY_VERSION,
            "matching_policy_version": "test_v1",
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
    assert dataset.candidate_query_version == CANDIDATE_QUERY_VERSION
    assert dataset.path_query_version == PATH_QUERY_VERSION
    assert dataset.contract_fingerprint == _LIVE_CONTRACT_FINGERPRINT
    assert dataset.artifact_code_revision == "deadbeef"
    assert dataset.artifact_working_tree_dirty is False
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
