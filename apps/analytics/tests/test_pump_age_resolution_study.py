"""Coverage for HYP-027, the surviving claim from HYP-023's withdrawal.

The tests that matter are the ones the withdrawal implies: that the cutoffs are
absolute and never derived from the data in front of them, that the groups
compared are ones production scores identically, that the floors bind on the
thinner side rather than the pool, and that the direction graded is the one
declared in advance.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from schurfer_analytics import pump_age_resolution_study as study_module
from schurfer_analytics.pump_age_resolution_study import (
    GROUP_A,
    GROUP_B,
    assess_readiness,
    assign_group,
    render_markdown,
    render_readiness,
    run_study,
    select_one_per_episode,
)
from schurfer_analytics.research_contract import ContractViolationError, ResearchContract
from schurfer_analytics.score_component_study import horizon_cost_pct, within_window

if TYPE_CHECKING:
    from pathlib import Path

_SINCE = datetime(2026, 8, 25, tzinfo=UTC)
_UNTIL = datetime(2026, 9, 30, tzinfo=UTC)


def _contract(**over: object) -> ResearchContract:
    defaults: dict[str, object] = {
        "hypothesis_id": "HYP-027",
        "contract_version": "v1",
        "window_since": _SINCE,
        "window_until": _UNTIL,
        "strategy_versions": ("pump_short_v1_market_quality",),
        "allow_fallback": False,
        "outcome_horizon_minutes": 60,
        "metric": "median",
        "comparison": "difference_of_metric",
        "baseline_policy": GROUP_B,
        "challenger_policies": (GROUP_A,),
        "cost_model_version": "conservative_costs_v1",
        "minimum_completed_trades": 3,
        "minimum_clusters": 2,
        "candidate_margin": 1.0,
        "rejection_margin": -1.0,
    }
    defaults.update(over)
    return ResearchContract(**defaults)  # type: ignore[arg-type]


def _row(
    episode: int,
    *,
    age_minutes: float,
    ret: float | None,
    minutes: int = 0,
    action: str = "skipped",
    base: str | None = None,
) -> dict[str, object]:
    return {
        "pump_event_id": episode,
        "base": base or f"TOK{episode}",
        "ts": _SINCE + timedelta(minutes=minutes),
        "action": action,
        "components": {
            "pump_age": {"value": age_minutes / 60, "points": 0, "max": 2, "note": ""},
        },
        "short_return_pct": ret,
        "mfe_pct": 1.0,
        "mae_pct": -1.0,
        "decision_id": f"decision-{episode}-{minutes}",
    }


# --- the cutoffs ------------------------------------------------------------


def test_the_group_boundary_is_absolute_and_inclusive_below() -> None:
    """Not a quantile. The withdrawn HYP-023 result came from boundaries derived
    from the data in front of them, which put a cut through 368 identical
    values; these are fixed by the contract and shared by every window."""
    assert assign_group(0.0) == GROUP_A
    assert assign_group(0.6) == GROUP_A
    assert assign_group(0.61) == GROUP_B
    assert assign_group(60.0) == GROUP_B


def test_an_episode_production_actually_scores_is_excluded_not_reported() -> None:
    """Above an hour production awards 1 point, above four hours 2. Those are the
    episodes the score does distinguish, and they are a different question with
    its own window. Reporting them descriptively here would spend their data
    before that contract exists."""
    assert assign_group(60.01) is None
    assert assign_group(600.0) is None


def test_a_negative_age_is_out_of_scope_rather_than_youngest() -> None:
    assert assign_group(-1.0) is None


def test_both_compared_groups_score_zero_in_production() -> None:
    """The premise of the whole hypothesis, asserted so a later edit to the
    cutoffs cannot quietly break it: production's first threshold is one hour,
    so every episode in either group contributes the same zero points."""
    production_zero_point_ceiling_minutes = 60.0
    for age in (0.0, 0.6, 0.61, 30.0, 59.9):
        assert assign_group(age) is not None
        assert age <= production_zero_point_ceiling_minutes


# --- the comparison ---------------------------------------------------------


def _cohort(*, young_return: float, older_return: float, count: int = 8) -> list[dict[str, object]]:
    rows = [
        _row(index, age_minutes=0.6, ret=young_return, base=f"YOUNG{index}")
        for index in range(1, count + 1)
    ]
    rows += [
        _row(count + index, age_minutes=5.0, ret=older_return, base=f"OLD{index}")
        for index in range(1, count + 1)
    ]
    return rows


def test_a_difference_in_the_declared_direction_confirms() -> None:
    result = run_study(
        select_one_per_episode(_cohort(young_return=3.0, older_return=0.0)), _contract()
    )
    assert result.difference_pct == pytest.approx(3.0)
    assert result.verdict == "candidate"


def test_a_difference_the_other_way_refutes_rather_than_being_reported_as_signal() -> None:
    """The direction was taken from discovery, so this test is one-sided. A large
    difference the wrong way is a refutation and must not come back as a finding
    with a sign attached."""
    result = run_study(
        select_one_per_episode(_cohort(young_return=0.0, older_return=3.0)), _contract()
    )
    assert result.difference_pct == pytest.approx(-3.0)
    assert result.verdict == "rejected"


def test_a_small_difference_is_inconclusive() -> None:
    result = run_study(
        select_one_per_episode(_cohort(young_return=1.0, older_return=0.7)), _contract()
    )
    assert result.verdict == "inconclusive"


def test_the_cost_model_is_applied_to_both_sides() -> None:
    result = run_study(
        select_one_per_episode(_cohort(young_return=2.0, older_return=1.0)), _contract()
    )
    assert result.groups[GROUP_A].median_net_pct == pytest.approx(2.0 - horizon_cost_pct(60))
    # A difference of medians cancels the cost, which is why the cost model
    # cannot flatter this comparison in either direction.
    assert result.difference_pct == pytest.approx(1.0)


# --- floors bind on the thinner side ----------------------------------------


def test_the_episode_floor_binds_on_the_thinner_group_not_the_pool() -> None:
    """The defect this forestalls: 254 young episodes and 119 older ones pool to
    373, which clears a 150 floor while the comparison rests on 119. A comparison
    is only as well evidenced as its thinner side."""
    rows = [_row(index, age_minutes=0.6, ret=3.0, base=f"YOUNG{index}") for index in range(1, 200)]
    rows += [
        _row(500 + index, age_minutes=5.0, ret=0.0, base=f"OLD{index}") for index in range(1, 40)
    ]
    result = run_study(
        select_one_per_episode(rows), _contract(minimum_completed_trades=150, minimum_clusters=30)
    )
    assert result.verdict == "inconclusive"
    assert "39 in the thinner group" in result.detail


def test_the_cluster_floor_binds_the_same_way() -> None:
    """Two hundred episodes of one token that pumped repeatedly is one
    observation about that token."""
    rows = [_row(index, age_minutes=0.6, ret=3.0, base="SAME") for index in range(1, 60)]
    rows += [
        _row(500 + index, age_minutes=5.0, ret=0.0, base=f"OLD{index}") for index in range(1, 60)
    ]
    result = run_study(
        select_one_per_episode(rows), _contract(minimum_completed_trades=10, minimum_clusters=30)
    )
    assert result.verdict == "inconclusive"
    assert "cluster floor" in result.detail


def test_an_empty_group_is_inconclusive_rather_than_a_one_sided_claim() -> None:
    rows = [_row(index, age_minutes=0.6, ret=3.0) for index in range(1, 10)]
    result = run_study(select_one_per_episode(rows), _contract())
    assert result.difference_pct is None
    assert result.verdict == "inconclusive"


# --- the shared family rules still apply ------------------------------------


def test_one_observation_per_episode() -> None:
    rows = [_row(1, age_minutes=0.6, ret=1.0, minutes=index) for index in range(200)]
    assert len(select_one_per_episode(rows)) == 1


def test_an_opened_decision_represents_its_episode() -> None:
    rows = [
        _row(1, age_minutes=0.6, ret=1.0, minutes=0, action="skipped"),
        _row(1, age_minutes=9.0, ret=2.0, minutes=5, action="opened"),
    ]
    selected = select_one_per_episode(rows)
    assert len(selected) == 1
    assert selected[0].age_minutes == pytest.approx(9.0)


def test_an_outcome_reaching_past_the_window_is_dropped() -> None:
    inside = int((_UNTIL - _SINCE).total_seconds() / 60) - 61
    straddling = int((_UNTIL - _SINCE).total_seconds() / 60) - 40
    observations = select_one_per_episode(
        [
            _row(1, age_minutes=0.6, ret=1.0, minutes=inside),
            _row(2, age_minutes=0.6, ret=1.0, minutes=straddling),
        ]
    )
    kept = within_window(observations, _contract())
    assert {item.pump_event_id for item in kept} == {1}


def test_an_episode_with_no_recorded_age_is_dropped_rather_than_called_young() -> None:
    """Absent and zero are different things, and defaulting a missing age to zero
    would put every unrecorded episode in the group under test."""
    rows = [_row(1, age_minutes=0.6, ret=1.0)]
    rows.append(
        {
            "pump_event_id": 2,
            "base": "TOK2",
            "ts": _SINCE,
            "action": "skipped",
            "components": {"pump_age": {"points": 0, "max": 2, "note": "no value"}},
            "short_return_pct": 1.0,
            "mfe_pct": None,
            "mae_pct": None,
        }
    )
    assert {item.pump_event_id for item in select_one_per_episode(rows)} == {1}


# --- nothing about the outcomes exists below the floors ----------------------


def _thin_cohort() -> list[dict[str, object]]:
    """Two episodes and two clusters per group, against floors of 150 and 30."""
    return [
        _row(1, age_minutes=0.6, ret=3.0, base="Y1"),
        _row(2, age_minutes=0.6, ret=2.5, base="Y2"),
        _row(3, age_minutes=5.0, ret=-0.2, base="O1"),
        _row(4, age_minutes=5.0, ret=-0.3, base="O2"),
    ]


def test_no_outcome_statistic_is_computed_below_the_floors() -> None:
    """The blocker a colleague found. run_study used to compute medians,
    quartiles and the difference and only then check sufficiency, and
    render_markdown printed them beside the word inconclusive. The contract says
    "nothing else is computed", and a verdict of inconclusive does not un-read a
    number that has already been shown."""
    contract = _contract(minimum_completed_trades=150, minimum_clusters=30)
    result = run_study(select_one_per_episode(_thin_cohort()), contract)

    assert result.verdict == "inconclusive"
    assert result.difference_pct is None
    assert result.groups == {}, "no group statistic exists at all, not merely unprinted"


def test_the_report_below_the_floors_contains_no_returns() -> None:
    contract = _contract(minimum_completed_trades=150, minimum_clusters=30)
    result = run_study(select_one_per_episode(_thin_cohort()), contract)
    rendered = render_markdown(
        result,
        contract,
        generated_at=datetime(2026, 9, 9, tzinfo=UTC),
        code_revision="abc123",
        working_tree_dirty=False,
        latest_decision_at=None,
        incomplete_outcome_episodes=0,
    )
    assert "inconclusive" in rendered
    assert "Nothing about the outcomes was computed" in rendered
    # The medians the old version leaked, and the difference between them.
    for leaked in ("2.79", "0.20", "+3.00", "Median net", "Median MFE"):
        assert leaked not in rendered, f"{leaked!r} must not reach a report below the floors"


def test_the_readiness_report_counts_and_nothing_more() -> None:
    contract = _contract(minimum_completed_trades=150, minimum_clusters=30)
    readiness = assess_readiness(select_one_per_episode(_thin_cohort()), contract)
    rendered = render_readiness(
        readiness,
        contract,
        generated_at=datetime(2026, 9, 9, tzinfo=UTC),
        code_revision="abc123",
        working_tree_dirty=False,
        incomplete_outcome_episodes=7,
    )
    assert "NOT READY" in rendered
    assert "2 |" in rendered
    for leaked in ("Median", "net", "MFE", "%"):
        assert leaked not in rendered, f"{leaked!r} must not reach a readiness report"


def test_readiness_is_computed_without_any_outcome() -> None:
    """Asserted by feeding it observations whose returns are absurd: if any of
    them reached the readiness numbers, this would show it."""
    contract = _contract(minimum_completed_trades=2, minimum_clusters=2)
    rows = _thin_cohort()
    poisoned = [{**row, "short_return_pct": 10_000.0} for row in rows]
    assert assess_readiness(select_one_per_episode(rows), contract) == assess_readiness(
        select_one_per_episode(poisoned), contract
    )


def test_the_provenance_records_a_dirty_working_tree() -> None:
    """Parsed and then dropped in the first version, so a report from a modified
    tree was indistinguishable from one from a clean commit."""
    contract = _contract()
    result = run_study(
        select_one_per_episode(_cohort(young_return=3.0, older_return=0.0)), contract
    )
    rendered = render_markdown(
        result,
        contract,
        generated_at=datetime(2026, 9, 9, tzinfo=UTC),
        code_revision="abc123",
        working_tree_dirty=True,
        latest_decision_at=None,
        incomplete_outcome_episodes=0,
    )
    assert "working tree dirty" in rendered


# --- the episode keeps its own decision --------------------------------------


def test_an_episode_whose_decision_has_no_outcome_is_dropped_not_substituted() -> None:
    """The same defect as HYP-023's, and it lands harder here: the substituted
    decision has a different age, so the episode changes group. On the discovery
    window 19 of 24 substitutions crossed this contract's own 0.6-minute
    boundary."""
    rows = [
        {**_row(1, age_minutes=0.6, ret=0.0, action="opened"), "short_return_pct": None},
        _row(1, age_minutes=6.0, ret=3.0, minutes=7),
    ]
    assert select_one_per_episode(rows) == ()


def test_the_decision_is_carried_so_the_sample_can_freeze_it() -> None:
    """Freezing episode ids alone would call two runs the same sample even when
    the same episodes were measured through different decisions."""
    result = run_study(
        select_one_per_episode(_cohort(young_return=3.0, older_return=0.0)), _contract()
    )
    assert len(result.decision_ids) == len(result.episode_ids)
    assert all(isinstance(key, str) for key in result.decision_ids)


# --- one read means one read, through the CLI --------------------------------


def _cli_rows(count: int, *, young_return: float, older_return: float) -> list[dict[str, object]]:
    rows = [
        _row(index, age_minutes=0.6, ret=young_return, base=f"Y{index}")
        for index in range(1, count + 1)
    ]
    rows += [
        _row(1000 + index, age_minutes=5.0, ret=older_return, base=f"O{index}")
        for index in range(1, count + 1)
    ]
    return rows


def _run_cli(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    contract_path: Path,
    manifest_path: Path,
    rows: list[dict[str, object]],
    mode: str = "read",
) -> str:
    async def _fake_load(_db_url: str, _contract: object) -> tuple[dict[str, object], ...]:
        return tuple(rows)

    monkeypatch.setattr(study_module, "load_observations", _fake_load)
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "pump-age-resolution-study",
            "--contract",
            str(contract_path),
            "--mode",
            mode,
            "--sample-manifest",
            str(manifest_path),
            "--code-revision",
            "abc123",
            "--no-working-tree-dirty",
        ],
    )
    study_module.main()
    return capsys.readouterr().out


@pytest.fixture
def registered_contract(tmp_path: Path) -> Path:
    path = tmp_path / "hyp-027.json"
    path.write_text(_contract(minimum_completed_trades=2, minimum_clusters=2).to_json())
    return path


def test_a_second_run_on_a_different_sample_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    registered_contract: Path,
    tmp_path: Path,
) -> None:
    """The blocker: --sample-manifest was optional and Make passed it only when
    asked, so the ordinary formal run was the unfrozen one. A colleague ran the
    real main twice under one contract, got candidate +3.00 and then candidate
    +5.00, and freeze_or_verify_sample was never called."""
    manifest = tmp_path / "sample.json"
    first = _run_cli(
        monkeypatch,
        capsys,
        registered_contract,
        manifest,
        _cli_rows(3, young_return=3.0, older_return=0.0),
    )
    assert "Verdict" in first
    assert manifest.exists(), "the formal path freezes without being asked to"

    with pytest.raises(ContractViolationError, match="different sample"):
        _run_cli(
            monkeypatch,
            capsys,
            registered_contract,
            manifest,
            _cli_rows(4, young_return=5.0, older_return=0.0),
        )


def test_the_same_sample_measured_through_other_decisions_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    registered_contract: Path,
    tmp_path: Path,
) -> None:
    """Identical episodes are not proof of an identical measurement. Which
    decision represents an episode used to change as outcomes resolved."""
    manifest = tmp_path / "sample.json"
    rows = _cli_rows(3, young_return=3.0, older_return=0.0)
    _run_cli(monkeypatch, capsys, registered_contract, manifest, rows)

    same_episodes_other_decisions = [
        {**row, "decision_id": f"other-{row['decision_id']}"} for row in rows
    ]
    with pytest.raises(ContractViolationError, match="measurements appeared"):
        _run_cli(monkeypatch, capsys, registered_contract, manifest, same_episodes_other_decisions)


def test_an_identical_rerun_is_allowed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    registered_contract: Path,
    tmp_path: Path,
) -> None:
    """Re-running the same measurement is reproduction, not a second experiment."""
    manifest = tmp_path / "sample.json"
    rows = _cli_rows(3, young_return=3.0, older_return=0.0)
    _run_cli(monkeypatch, capsys, registered_contract, manifest, rows)
    _run_cli(monkeypatch, capsys, registered_contract, manifest, rows)


def test_the_readiness_mode_never_freezes_anything(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    registered_contract: Path,
    tmp_path: Path,
) -> None:
    """It is meant to run daily. Freezing on it would burn the sample on the
    first check rather than on the read."""
    manifest = tmp_path / "sample.json"
    out = _run_cli(
        monkeypatch,
        capsys,
        registered_contract,
        manifest,
        _cli_rows(3, young_return=3.0, older_return=0.0),
        mode="readiness",
    )
    assert "READY" in out
    assert not manifest.exists()


def test_a_contract_for_another_hypothesis_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    path = tmp_path / "other.json"
    path.write_text(_contract(hypothesis_id="HYP-999").to_json())
    with pytest.raises(ContractViolationError):
        _run_cli(
            monkeypatch,
            capsys,
            path,
            tmp_path / "s.json",
            _cli_rows(3, young_return=1.0, older_return=0.0),
        )


def test_a_contract_asking_for_a_different_metric_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """A formally valid contract registering a mean, printed beside a difference
    of medians, is a result labelled with a rule it did not follow."""
    path = tmp_path / "mean.json"
    path.write_text(_contract(metric="mean").to_json())
    with pytest.raises(ContractViolationError, match="metric"):
        _run_cli(
            monkeypatch,
            capsys,
            path,
            tmp_path / "s.json",
            _cli_rows(3, young_return=1.0, older_return=0.0),
        )
