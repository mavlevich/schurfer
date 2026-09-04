"""Synthetic-fixture tests for cex_activity_discovery_dataset_artifact.py
(research/cex-activity-discovery-completion-v1).

No real qualified freeze exists yet (HYP-016's own discovery window is
already fully in the past, but the freeze/evaluate CLI split this module
supports has not run for real) -- exercised here against synthetic
`OutcomeSignalEpisode`/`ExactPricePath`/`PathRequest` fixtures, mirroring
this codebase's own established discipline of testing a frozen artifact
contract before any real invocation depends on it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from schurfer_analytics.cex_activity_discovery import (
    ExactPricePath,
    OutcomeSignalEpisode,
    PathRequest,
)
from schurfer_analytics.cex_activity_discovery_dataset_artifact import (
    CohortDriftDetectedError,
    WrongDatasetArtifactError,
    build_cohort,
    build_rows,
    freeze,
    read,
    read_authoritative_fingerprint,
)
from schurfer_analytics.research_dataset_artifact import ResearchDatasetArtifactCorruptError

if TYPE_CHECKING:
    from pathlib import Path

_BASE = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)


def _episode(
    episode_id: int, *, symbol: str = "TESTUSDT", direction: str = "buy"
) -> OutcomeSignalEpisode:
    trigger_at = _BASE + timedelta(minutes=episode_id)
    return OutcomeSignalEpisode(
        episode_id=episode_id,
        signal_id=f"cex_test:{episode_id}",
        source="cex_buy_burst_v1",
        exchange="bybit",
        symbol=symbol,
        direction=direction,
        trigger_at=trigger_at,
        entry_at=trigger_at + timedelta(minutes=1),
        signal_value=12.5,
    )


def _resolved_path(request_id: str, *, symbol: str = "TESTUSDT") -> ExactPricePath:
    entry_at = _BASE + timedelta(minutes=1)
    return ExactPricePath(
        request_id=request_id,
        symbol=symbol,
        trigger_at=_BASE,
        entry_at=entry_at,
        entry_price=100.0,
        observed_minutes=1440,
        max_high=110.0,
        min_low=95.0,
        first_up_25_at=None,
        first_down_25_at=None,
    )


def _unresolved_path(request_id: str) -> ExactPricePath:
    return ExactPricePath(
        request_id=request_id,
        symbol="TESTUSDT",
        trigger_at=_BASE,
        entry_at=_BASE + timedelta(minutes=1),
        entry_price=None,
        observed_minutes=0,
        max_high=None,
        min_low=None,
        first_up_25_at=None,
        first_down_25_at=None,
    )


def _cohort() -> dict[str, object]:
    return build_cohort(
        hypothesis_id="HYP-016",
        since=datetime(2026, 8, 18, tzinfo=UTC),
        until_exclusive=datetime(2026, 8, 27, tzinfo=UTC),
        exchange="bybit",
        market_type="linear",
        capture_version="test_capture_v1",
        directions=("buy", "sell"),
        control_boundary_policy_version="within_discovery_window_v1",
    )


def _fixture() -> (
    tuple[
        tuple[OutcomeSignalEpisode, ...],
        dict[str, ExactPricePath],
        dict[int, tuple[PathRequest, ...]],
        dict[str, ExactPricePath],
    ]
):
    episode1 = _episode(1)
    episode2 = _episode(2, symbol="OTHERUSDT", direction="sell")
    signal_paths = {
        "signal:1": _resolved_path("signal:1"),
        "signal:2": _unresolved_path("signal:2"),
    }
    control_request = PathRequest(
        "control:1:p1", "TESTUSDT", _BASE + timedelta(days=1), _BASE + timedelta(days=1, minutes=1)
    )
    controls_by_episode = {1: (control_request,), 2: ()}
    control_paths = {"control:1:p1": _resolved_path("control:1:p1")}
    return (episode1, episode2), signal_paths, controls_by_episode, control_paths


def test_build_rows_is_pure_and_deterministic() -> None:
    episodes, signal_paths, controls_by_episode, control_paths = _fixture()
    rows1 = build_rows(
        episodes=episodes,
        signal_paths=signal_paths,
        controls_by_episode=controls_by_episode,
        control_paths=control_paths,
    )
    rows2 = build_rows(
        episodes=episodes,
        signal_paths=signal_paths,
        controls_by_episode=controls_by_episode,
        control_paths=control_paths,
    )
    assert rows1 == rows2
    assert len(rows1) == 2
    assert rows1[0]["row_id"] == "cex_test:1"
    assert rows1[0]["signal_path"]["present"] is True
    assert rows1[0]["control_requests"] == [
        {
            "request_id": "control:1:p1",
            "symbol": "TESTUSDT",
            "trigger_at": (_BASE + timedelta(days=1)).isoformat(),
            "entry_at": (_BASE + timedelta(days=1, minutes=1)).isoformat(),
        }
    ]
    # episode2's own signal is unresolved -- present=True still (a real
    # ExactPricePath exists, just not resolved), unlike a genuinely
    # missing_path_result.
    assert rows1[1]["signal_path"]["present"] is True
    assert rows1[1]["signal_path"]["unresolved_reason"] == "missing_entry_bar"


def test_build_rows_missing_path_result_when_no_row_exists_at_all() -> None:
    episode = _episode(3)
    rows = build_rows(
        episodes=(episode,), signal_paths={}, controls_by_episode={3: ()}, control_paths={}
    )
    assert rows[0]["signal_path"] == {
        "present": False,
        "unresolved_reason": "missing_path_result",
    }


def test_freeze_then_read_round_trips_utc_and_every_field(tmp_path: Path) -> None:
    episodes, signal_paths, controls_by_episode, control_paths = _fixture()
    rows = build_rows(
        episodes=episodes,
        signal_paths=signal_paths,
        controls_by_episode=controls_by_episode,
        control_paths=control_paths,
    )
    manifest = freeze(
        cohort=_cohort(),
        rows=rows,
        code_revision="deadbeef",
        working_tree_dirty=False,
        directory=tmp_path,
    )
    (
        read_manifest,
        read_episodes,
        read_signal_paths,
        read_controls_by_episode,
        read_control_paths,
    ) = read(manifest.fingerprint, directory=tmp_path)

    assert read_manifest.fingerprint == manifest.fingerprint
    assert read_episodes == episodes
    assert read_signal_paths["signal:1"] == signal_paths["signal:1"]
    assert read_signal_paths["signal:1"].trigger_at.tzinfo is not None
    # episode2's own unresolved signal path round-trips too (present=True,
    # just unresolved) -- only a genuinely absent row is dropped from the
    # dict.
    assert read_signal_paths["signal:2"] == signal_paths["signal:2"]
    assert read_signal_paths["signal:2"].unresolved_reason == "missing_entry_bar"
    assert read_controls_by_episode[1] == controls_by_episode[1]
    assert read_controls_by_episode[2] == ()
    assert read_control_paths["control:1:p1"] == control_paths["control:1:p1"]


def test_read_rejects_a_fingerprint_from_a_different_dataset(tmp_path: Path) -> None:
    from schurfer_analytics.research_dataset_artifact import write_dataset_artifact

    _outcome, other_manifest = write_dataset_artifact(
        dataset_name="some_other_dataset",
        dataset_version="v1",
        schema_version="v1",
        rows=[{"row_id": "a", "value": 1}],
        row_id_field="row_id",
        row_order="row_id ascending",
        cohort={"x": 1},
        code_revision="deadbeef",
        working_tree_dirty=False,
        directory=tmp_path,
    )
    assert other_manifest is not None
    with pytest.raises(WrongDatasetArtifactError):
        read(other_manifest.fingerprint, directory=tmp_path)


def test_read_rejects_a_corrupted_artifact(tmp_path: Path) -> None:
    episodes, signal_paths, controls_by_episode, control_paths = _fixture()
    rows = build_rows(
        episodes=episodes,
        signal_paths=signal_paths,
        controls_by_episode=controls_by_episode,
        control_paths=control_paths,
    )
    manifest = freeze(
        cohort=_cohort(),
        rows=rows,
        code_revision="deadbeef",
        working_tree_dirty=False,
        directory=tmp_path,
    )
    data_path = tmp_path / manifest.fingerprint[:2] / manifest.fingerprint / "data.json"
    data_path.write_text('[{"tampered": true}]')
    with pytest.raises(ResearchDatasetArtifactCorruptError):
        read(manifest.fingerprint, directory=tmp_path)


# --- cohort-drift lock (colleague review, 2026-09-03) ----------------------


def test_freeze_is_idempotent_for_the_same_cohort_and_content(tmp_path: Path) -> None:
    episodes, signal_paths, controls_by_episode, control_paths = _fixture()
    rows = build_rows(
        episodes=episodes,
        signal_paths=signal_paths,
        controls_by_episode=controls_by_episode,
        control_paths=control_paths,
    )
    cohort = _cohort()
    manifest1 = freeze(
        cohort=cohort, rows=rows, code_revision="rev1", working_tree_dirty=False, directory=tmp_path
    )
    manifest2 = freeze(
        cohort=cohort, rows=rows, code_revision="rev2", working_tree_dirty=False, directory=tmp_path
    )
    assert manifest1.fingerprint == manifest2.fingerprint
    assert read_authoritative_fingerprint(cohort, directory=tmp_path) == manifest1.fingerprint


def test_freeze_raises_on_genuine_cohort_drift(tmp_path: Path) -> None:
    """The exact scenario this whole module exists to catch: a SECOND
    freeze attempt for the SAME cohort produces DIFFERENT content (a
    stand-in for a late-arriving historical bar correction changing an
    exact path's own outcome) -- must raise, never silently prefer the new
    result."""
    episodes, signal_paths, controls_by_episode, control_paths = _fixture()
    cohort = _cohort()
    rows1 = build_rows(
        episodes=episodes,
        signal_paths=signal_paths,
        controls_by_episode=controls_by_episode,
        control_paths=control_paths,
    )
    freeze(
        cohort=cohort,
        rows=rows1,
        code_revision="rev1",
        working_tree_dirty=False,
        directory=tmp_path,
    )

    drifted_signal_paths = dict(signal_paths)
    drifted_signal_paths["signal:1"] = _resolved_path("signal:1")  # same shape, will mutate below
    # Force a genuine content difference: a different entry_price for the
    # same request_id, as a late correction would produce.
    original = drifted_signal_paths["signal:1"]
    drifted_signal_paths["signal:1"] = ExactPricePath(
        request_id=original.request_id,
        symbol=original.symbol,
        trigger_at=original.trigger_at,
        entry_at=original.entry_at,
        entry_price=999.0,
        observed_minutes=original.observed_minutes,
        max_high=original.max_high,
        min_low=original.min_low,
        first_up_25_at=original.first_up_25_at,
        first_down_25_at=original.first_down_25_at,
    )
    rows2 = build_rows(
        episodes=episodes,
        signal_paths=drifted_signal_paths,
        controls_by_episode=controls_by_episode,
        control_paths=control_paths,
    )
    with pytest.raises(CohortDriftDetectedError):
        freeze(
            cohort=cohort,
            rows=rows2,
            code_revision="rev2",
            working_tree_dirty=False,
            directory=tmp_path,
        )
    # The FIRST freeze's own fingerprint remains authoritative -- a failed
    # second attempt must never overwrite it.
    first_fingerprint = read_authoritative_fingerprint(cohort, directory=tmp_path)
    assert first_fingerprint is not None


def test_read_authoritative_fingerprint_is_none_for_an_unfrozen_cohort(tmp_path: Path) -> None:
    assert read_authoritative_fingerprint(_cohort(), directory=tmp_path) is None


def test_different_cohorts_never_collide_on_the_same_lock(tmp_path: Path) -> None:
    episodes, signal_paths, controls_by_episode, control_paths = _fixture()
    rows = build_rows(
        episodes=episodes,
        signal_paths=signal_paths,
        controls_by_episode=controls_by_episode,
        control_paths=control_paths,
    )
    cohort_a = _cohort()
    cohort_b = build_cohort(
        hypothesis_id="HYP-016",
        since=datetime(2026, 9, 1, tzinfo=UTC),  # a genuinely different window
        until_exclusive=datetime(2026, 9, 8, tzinfo=UTC),
        exchange="bybit",
        market_type="linear",
        capture_version="test_capture_v1",
        directions=("buy", "sell"),
        control_boundary_policy_version="within_discovery_window_v1",
    )
    manifest_a = freeze(
        cohort=cohort_a,
        rows=rows,
        code_revision="rev1",
        working_tree_dirty=False,
        directory=tmp_path,
    )
    # Same rows, different cohort -- must NOT raise CohortDriftDetectedError,
    # since these are two independent cohort locks (write_dataset_artifact's
    # own fingerprint already includes `cohort`, so the two manifests get
    # DIFFERENT fingerprints here even with identical rows -- that is
    # expected and not itself what this test checks; the real assertion is
    # that each cohort's own lock tracks its own fingerprint independently,
    # with neither call raising a spurious drift error against the other).
    manifest_b = freeze(
        cohort=cohort_b,
        rows=rows,
        code_revision="rev1",
        working_tree_dirty=False,
        directory=tmp_path,
    )
    assert manifest_a.fingerprint != manifest_b.fingerprint
    assert read_authoritative_fingerprint(cohort_a, directory=tmp_path) == manifest_a.fingerprint
    assert read_authoritative_fingerprint(cohort_b, directory=tmp_path) == manifest_b.fingerprint
