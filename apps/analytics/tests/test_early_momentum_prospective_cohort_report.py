"""Tests for early_momentum_prospective_cohort_report.py -- verdict
mapping, the fail-closed registration guard, and delegation to the shared
net-evidence builder rather than reimplementing any underlying economics."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from schurfer_analytics import early_momentum_prospective_cohort_report as prospective_mod
from schurfer_analytics.early_momentum_net_evidence import (
    VERDICT_FAIL,
    VERDICT_INSUFFICIENT_DATA,
    VERDICT_INVALID_INTEGRITY,
    VERDICT_PASS_LIVE_MICRO_CANDIDATE,
    RawDataset,
    Verdict,
)
from schurfer_analytics.early_momentum_prospective_cohort_report import (
    PROSPECTIVE_COHORT_KEY,
    PROSPECTIVE_VERDICT_BLOCKED,
    PROSPECTIVE_VERDICT_COLLECTING,
    PROSPECTIVE_VERDICT_ELIGIBLE,
    PROSPECTIVE_VERDICT_FAIL,
    CohortRegistration,
    ProspectiveCohortNotStartedError,
    generate_prospective_cohort_report,
    map_verdict_to_prospective,
)

if TYPE_CHECKING:
    from pathlib import Path

# --- map_verdict_to_prospective (pure) -------------------------------


def test_invalid_integrity_maps_to_blocked_not_economic_fail() -> None:
    """A broken pipeline/contract-hash mismatch must never be read as the
    strategy itself failing -- that would let a data bug wrongly kill a
    promising strategy."""
    verdict = Verdict(VERDICT_INVALID_INTEGRITY, ("unexpected_contract_hash",), False)
    prospective, reasons = map_verdict_to_prospective(verdict)
    assert prospective == PROSPECTIVE_VERDICT_BLOCKED
    assert "unexpected_contract_hash" in reasons
    assert "cannot_evaluate_integrity_violation" in reasons


def test_insufficient_data_maps_to_collecting() -> None:
    verdict = Verdict(VERDICT_INSUFFICIENT_DATA, ("closed_trades_5_below_100",), True)
    prospective, reasons = map_verdict_to_prospective(verdict)
    assert prospective == PROSPECTIVE_VERDICT_COLLECTING
    assert reasons == ("closed_trades_5_below_100",)


def test_fail_maps_to_fail() -> None:
    verdict = Verdict(VERDICT_FAIL, ("mean_net_return_not_positive",), False)
    prospective, reasons = map_verdict_to_prospective(verdict)
    assert prospective == PROSPECTIVE_VERDICT_FAIL
    assert reasons == ("mean_net_return_not_positive",)


def test_pass_live_micro_candidate_maps_to_eligible() -> None:
    verdict = Verdict(VERDICT_PASS_LIVE_MICRO_CANDIDATE, (), False)
    prospective, reasons = map_verdict_to_prospective(verdict)
    assert prospective == PROSPECTIVE_VERDICT_ELIGIBLE
    assert reasons == ()


def test_dirty_run_can_never_map_to_eligible() -> None:
    verdict = Verdict(VERDICT_PASS_LIVE_MICRO_CANDIDATE, (), False)
    prospective, reasons = map_verdict_to_prospective(verdict, formal_run=False)
    assert prospective == PROSPECTIVE_VERDICT_BLOCKED
    assert reasons == ("non_formal_run",)


def test_pass_without_immutable_artifact_remains_collecting() -> None:
    verdict = Verdict(VERDICT_PASS_LIVE_MICRO_CANDIDATE, (), False)
    prospective, reasons = map_verdict_to_prospective(verdict, immutable_artifact=False)
    assert prospective == PROSPECTIVE_VERDICT_COLLECTING
    assert reasons == ("immutable_artifact_required_for_eligibility",)


def test_unrecognized_verdict_state_fails_closed() -> None:
    verdict = Verdict("some_future_state", (), False)
    with pytest.raises(ValueError, match="unrecognized underlying verdict"):
        map_verdict_to_prospective(verdict)


# --- generate_prospective_cohort_report (orchestration) ---------------


async def test_raises_not_started_when_durable_registration_is_missing() -> None:
    with (
        patch.object(
            prospective_mod,
            "load_registration",
            AsyncMock(side_effect=ProspectiveCohortNotStartedError("not registered")),
        ),
        pytest.raises(ProspectiveCohortNotStartedError),
    ):
        await generate_prospective_cohort_report(
            db_url="postgresql://fake",
            cohort_end=datetime(2026, 9, 1, tzinfo=UTC),
            code_revision="abc123",
            working_tree_dirty=False,
        )


async def test_delegates_to_shared_builder_with_registered_cohort_start() -> None:
    prospective_start = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
    cohort_end = datetime(2026, 9, 1, tzinfo=UTC)
    fake_evidence = MagicMock()
    fake_evidence.verdict = Verdict(VERDICT_PASS_LIVE_MICRO_CANDIDATE, (), False)
    fake_evidence.formal_run = True
    registration = CohortRegistration(
        cohort_key=PROSPECTIVE_COHORT_KEY,
        strategy_name="early_momentum",
        strategy_version="4",
        contract_sha256=prospective_mod.EXPECTED_CONTRACT_SHA256_HEX,
        runtime_policy_sha256=prospective_mod.EXPECTED_RUNTIME_POLICY_SHA256_HEX,
        cohort_started_at=prospective_start,
    )
    dataset = MagicMock()
    legacy_context = ()

    with (
        patch.object(prospective_mod, "load_registration", AsyncMock(return_value=registration)),
        patch.object(
            prospective_mod,
            "fetch_report_inputs",
            AsyncMock(return_value=(dataset, legacy_context)),
        ) as mock_fetch,
        patch.object(prospective_mod, "build_report", return_value=fake_evidence) as mock_build,
    ):
        report = await generate_prospective_cohort_report(
            db_url="postgresql://fake",
            cohort_end=cohort_end,
            code_revision="abc123",
            working_tree_dirty=False,
        )

    mock_fetch.assert_awaited_once_with(
        db_url="postgresql://fake",
        cohort_start=prospective_start,
        cohort_end=cohort_end,
    )
    mock_build.assert_called_once_with(
        dataset=dataset,
        legacy_context=legacy_context,
        code_revision="abc123",
        working_tree_dirty=False,
    )
    assert report.evidence is fake_evidence
    assert report.prospective_verdict == PROSPECTIVE_VERDICT_COLLECTING
    assert report.prospective_cohort_started_at == prospective_start
    assert report.report_version == prospective_mod.PROSPECTIVE_REPORT_VERSION


# --- provenance sync (colleague review, 2026-08-25) --------------------


def test_expected_contract_sha256_matches_the_pinned_execution_side_literal() -> None:
    """`schurfer-analytics` does not depend on `schurfer-execution` (see
    apps/analytics/pyproject.toml), so this cannot be a real import-time
    check -- this test is the tripwire that catches the two literals
    drifting apart if only one side of a deliberate contract change is
    ever updated. If this test fails, apps/execution/schurfer_execution/
    early_momentum.py's own _EXPECTED_CONTRACT_SHA256_HEX and this
    module's EXPECTED_CONTRACT_SHA256_HEX must be updated together."""
    assert (
        prospective_mod.EXPECTED_CONTRACT_SHA256_HEX
        == "bdda6c6423b0cc69d8b6266269cda07c31e20f4d256b1793229ab47beb5cb1ac"
    )
    assert (
        prospective_mod.EXPECTED_RUNTIME_POLICY_SHA256_HEX
        == "720888b733bc097d53071b26edd5b85b4bb6dcc295a386fc1dc6590f9a2888d8"
    )


async def test_only_clean_immutable_artifact_can_expose_eligible(tmp_path: Path) -> None:
    start = datetime(2026, 8, 25, tzinfo=UTC)
    end = start + timedelta(days=7)
    dataset = RawDataset(start, end, end + timedelta(hours=6), (), (), ())
    registration = CohortRegistration(
        cohort_key=PROSPECTIVE_COHORT_KEY,
        strategy_name="early_momentum",
        strategy_version="4",
        contract_sha256=prospective_mod.EXPECTED_CONTRACT_SHA256_HEX,
        runtime_policy_sha256=prospective_mod.EXPECTED_RUNTIME_POLICY_SHA256_HEX,
        cohort_started_at=start,
    )
    evidence = MagicMock(
        verdict=Verdict(VERDICT_PASS_LIVE_MICRO_CANDIDATE, (), False),
        formal_run=True,
    )
    with (
        patch.object(prospective_mod, "load_registration", AsyncMock(return_value=registration)),
        patch.object(prospective_mod, "fetch_report_inputs", AsyncMock(return_value=(dataset, ()))),
        patch.object(prospective_mod, "build_report", return_value=evidence),
    ):
        frozen = await generate_prospective_cohort_report(
            db_url="postgresql://fake",
            cohort_end=end,
            code_revision="abc123",
            working_tree_dirty=False,
            freeze_artifact=True,
            artifact_directory=str(tmp_path),
        )
    assert frozen.prospective_verdict == PROSPECTIVE_VERDICT_ELIGIBLE
    assert frozen.source_artifact is not None

    with patch.object(prospective_mod, "build_report", return_value=evidence):
        replayed = await generate_prospective_cohort_report(
            db_url=None,
            cohort_end=end,
            code_revision="abc123",
            working_tree_dirty=False,
            from_artifact=str(frozen.source_artifact["fingerprint"]),
            artifact_directory=str(tmp_path),
        )
    assert replayed.prospective_verdict == PROSPECTIVE_VERDICT_ELIGIBLE


async def test_dirty_run_cannot_publish_first_writer_wins_artifact(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="clean working tree"):
        await generate_prospective_cohort_report(
            db_url="postgresql://unused",
            cohort_end=datetime(2026, 9, 1, tzinfo=UTC),
            code_revision="abc123",
            working_tree_dirty=True,
            freeze_artifact=True,
            artifact_directory=str(tmp_path),
        )
