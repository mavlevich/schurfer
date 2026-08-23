"""Tests for early_momentum_net_evidence_report.py -- CLI parsing,
orchestration (repository mocked), cohort-maturity validation, and
Markdown/JSON rendering."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from schurfer_analytics import early_momentum_net_evidence_report as report_mod
from schurfer_analytics.early_momentum_net_evidence import (
    ACCOUNTING_VERSION,
    EXPECTED_CONTRACT_SHA256_HEX,
    FORMAL_COHORT_START,
    EpisodeRow,
    LegacyContextRow,
    RawDataset,
    TradeRow,
)

CS = FORMAL_COHORT_START
EXPECTED_HASH = bytes.fromhex(EXPECTED_CONTRACT_SHA256_HEX)


def _episode() -> EpisodeRow:
    armed = CS + timedelta(minutes=5)
    return EpisodeRow(
        episode_id="e1",
        strategy_id=1,
        contract_sha256=EXPECTED_HASH,
        exchange="bybit",
        native_market_id="FOOUSDT",
        execution_symbol="FOO/USDT:USDT",
        source_exchange="bybit",
        source_native_id="FOOUSDT",
        execution_identity_key="ik1",
        source_identity_key="sik1",
        cluster_key="ck1",
        armed_at=armed,
        expires_at=armed + timedelta(hours=1),
        status="closed",
        terminal_reason=None,
        claimed_at=armed + timedelta(seconds=1),
        claim_expires_at=armed + timedelta(seconds=31),
        claim_attempts=1,
    )


def _trade() -> TradeRow:
    entry = CS + timedelta(minutes=5, seconds=2)
    return TradeRow(
        trade_id=1,
        episode_id="e1",
        strategy_id=1,
        symbol="FOO/USDT:USDT",
        exchange="bybit",
        side="long",
        size_usd=100.0,
        leverage=5.0,
        entry_price=1.0,
        entry_at=entry,
        exit_price=1.05,
        exit_at=entry + timedelta(hours=1),
        fees_usd=0.5,
        funding_usd=0.1,
        slippage_usd=0.2,
        gross_pnl_usd=25.0,
        gross_pnl_pct=25.0,
        net_pnl_usd=24.2,
        net_pnl_pct=24.2,
        accounting_version=ACCOUNTING_VERSION,
        accounting_status="complete",
        accounting_error=None,
        status="closed",
        notes="take_profit move=5.0%",
        entry_idempotency_key="e1:entry:base",
        is_paper=True,
        setup_context_strategy="early_momentum_v4",
        entry_ask_impact_bps=3.0,
        entry_bid_impact_bps=2.0,
    )


def _dataset(*, db_snapshot_at: datetime, cohort_end: datetime) -> RawDataset:
    return RawDataset(
        cohort_start=CS,
        cohort_end=cohort_end,
        db_snapshot_at=db_snapshot_at,
        episodes=(_episode(),),
        trades=(_trade(),),
        exit_liquidity=(),
    )


# --- CLI parsing ---


def test_build_parser_requires_cohort_end_and_code_revision() -> None:
    parser = report_mod.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--no-working-tree-dirty"])


def test_build_parser_requires_a_dirty_tree_flag() -> None:
    parser = report_mod.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--cohort-end", "2026-08-24T00:00:00Z", "--code-revision", "abc123"])


def test_build_parser_rejects_both_dirty_flags_together() -> None:
    parser = report_mod.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "--cohort-end",
                "2026-08-24T00:00:00Z",
                "--code-revision",
                "abc123",
                "--working-tree-dirty",
                "--no-working-tree-dirty",
            ]
        )


def test_build_parser_accepts_a_valid_invocation() -> None:
    parser = report_mod.build_parser()
    args = parser.parse_args(
        [
            "--cohort-end",
            "2026-08-24T00:00:00Z",
            "--code-revision",
            "abc123",
            "--no-working-tree-dirty",
            "--format",
            "json",
        ]
    )
    assert args.working_tree_dirty is False
    assert args.no_working_tree_dirty is True
    assert args.format == "json"


# --- generate_report orchestration (repository mocked) ---


async def test_generate_report_raises_when_cohort_not_mature() -> None:
    cohort_end = CS + timedelta(days=1)
    dataset = _dataset(
        db_snapshot_at=cohort_end + timedelta(hours=1),  # only 1h past cohort_end, needs 6h
        cohort_end=cohort_end,
    )
    fake_repo = AsyncMock()
    fake_repo.fetch = AsyncMock(return_value=dataset)
    fake_repo.fetch_legacy_context = AsyncMock(return_value=())
    fake_repo.close = AsyncMock()
    with (
        patch(
            "schurfer_analytics.early_momentum_net_evidence_repository."
            "EarlyMomentumNetEvidenceRepository.from_url",
            return_value=fake_repo,
        ),
        pytest.raises(report_mod.CohortNotMatureError),
    ):
        await report_mod.generate_report(
            db_url="postgresql://x",
            cohort_end=cohort_end,
            code_revision="abc123",
            working_tree_dirty=False,
        )


async def test_generate_report_succeeds_when_cohort_is_mature() -> None:
    cohort_end = CS + timedelta(days=1)
    dataset = _dataset(
        db_snapshot_at=cohort_end + timedelta(hours=7),
        cohort_end=cohort_end,
    )
    fake_repo = AsyncMock()
    fake_repo.fetch = AsyncMock(return_value=dataset)
    fake_repo.fetch_legacy_context = AsyncMock(return_value=())
    fake_repo.close = AsyncMock()
    with patch(
        "schurfer_analytics.early_momentum_net_evidence_repository."
        "EarlyMomentumNetEvidenceRepository.from_url",
        return_value=fake_repo,
    ):
        report = await report_mod.generate_report(
            db_url="postgresql://x",
            cohort_end=cohort_end,
            code_revision="abc123",
            working_tree_dirty=False,
        )
    assert report.formal_run is True
    assert report.economics.closed_trades == 1
    assert report.expected_contract_sha256 == EXPECTED_CONTRACT_SHA256_HEX
    fake_repo.close.assert_awaited_once()


async def test_generate_report_marks_dirty_tree_as_not_a_formal_run() -> None:
    cohort_end = CS + timedelta(days=1)
    dataset = _dataset(
        db_snapshot_at=cohort_end + timedelta(hours=7),
        cohort_end=cohort_end,
    )
    fake_repo = AsyncMock()
    fake_repo.fetch = AsyncMock(return_value=dataset)
    fake_repo.fetch_legacy_context = AsyncMock(return_value=())
    fake_repo.close = AsyncMock()
    with patch(
        "schurfer_analytics.early_momentum_net_evidence_repository."
        "EarlyMomentumNetEvidenceRepository.from_url",
        return_value=fake_repo,
    ):
        report = await report_mod.generate_report(
            db_url="postgresql://x",
            cohort_end=cohort_end,
            code_revision="abc123",
            working_tree_dirty=True,
        )
    assert report.formal_run is False
    assert report.working_tree_dirty is True


async def test_generate_report_rejects_empty_code_revision() -> None:
    cohort_end = CS + timedelta(days=1)
    dataset = _dataset(
        db_snapshot_at=cohort_end + timedelta(hours=7),
        cohort_end=cohort_end,
    )
    fake_repo = AsyncMock()
    fake_repo.fetch = AsyncMock(return_value=dataset)
    fake_repo.fetch_legacy_context = AsyncMock(return_value=())
    fake_repo.close = AsyncMock()
    with (
        patch(
            "schurfer_analytics.early_momentum_net_evidence_repository."
            "EarlyMomentumNetEvidenceRepository.from_url",
            return_value=fake_repo,
        ),
        pytest.raises(ValueError, match="code revision"),
    ):
        await report_mod.generate_report(
            db_url="postgresql://x",
            cohort_end=cohort_end,
            code_revision="   ",
            working_tree_dirty=False,
        )


# --- rendering ---


def _sample_report(*, working_tree_dirty: bool = False) -> report_mod.NetEvidenceReport:
    from schurfer_analytics.early_momentum_net_evidence import (
        build_funnel,
        compute_capacity,
        compute_concurrency,
        compute_economics,
        compute_robustness,
        dataset_fingerprint,
        evaluate_verdict,
    )

    cohort_end = CS + timedelta(days=1)
    dataset = _dataset(db_snapshot_at=cohort_end + timedelta(hours=7), cohort_end=cohort_end)
    funnel = build_funnel(dataset)
    economics = compute_economics(funnel.comparable)
    concurrency = compute_concurrency(funnel.comparable)
    robustness = compute_robustness(funnel.comparable)
    capacity = compute_capacity(funnel.comparable, {})
    verdict = evaluate_verdict(funnel=funnel, economics=economics, robustness=robustness)
    return report_mod.NetEvidenceReport(
        report_version="early_momentum_net_evidence_v1",
        generated_at=datetime.now(UTC),
        code_revision="abc123",
        working_tree_dirty=working_tree_dirty,
        formal_run=not working_tree_dirty,
        db_snapshot_at=dataset.db_snapshot_at,
        cohort_start=dataset.cohort_start,
        cohort_end=dataset.cohort_end,
        expected_contract_sha256=EXPECTED_CONTRACT_SHA256_HEX,
        observed_contract_sha256=(EXPECTED_CONTRACT_SHA256_HEX,),
        dataset_fingerprint=dataset_fingerprint(dataset),
        funnel=funnel,
        economics=economics,
        concurrency=concurrency,
        robustness=robustness,
        capacity=capacity,
        verdict=verdict,
        legacy_context=(
            LegacyContextRow(
                setup_context_strategy="early_momentum_v2",
                total_trades=3,
                closed_trades=2,
                cancelled_trades=0,
                open_trades=1,
                complete_accounting_closed_trades=1,
                total_net_pnl_usd_complete_only=12.5,
            ),
        ),
    )


def test_render_markdown_contains_verdict_and_key_sections() -> None:
    text = report_mod.render_markdown(_sample_report())
    assert "## Verdict" in text
    assert "## Evidence funnel" in text
    assert "## Economics" in text
    assert "## Concurrency and entry waves" in text
    assert "## Robustness" in text
    assert "## Capacity evidence" in text
    assert "## Legacy version context" in text
    assert "insufficient_data" in text  # this synthetic 1-trade dataset can't clear the floor


def test_render_markdown_flags_a_non_formal_run() -> None:
    text = report_mod.render_markdown(_sample_report(working_tree_dirty=True))
    assert "NOT A FORMAL RUN" in text


def test_render_json_round_trips_the_verdict() -> None:
    import json

    payload = json.loads(report_mod.render_json(_sample_report()))
    assert payload["verdict"]["verdict"] == "insufficient_data"
    assert payload["formal_run"] is True
    assert payload["expected_contract_sha256"] == EXPECTED_CONTRACT_SHA256_HEX


def test_render_json_excludes_raw_bootstrap_samples() -> None:
    """block_bootstrap must carry only the estimate, never the full
    10,000-sample draw array, or the JSON output balloons for no reason."""
    import json

    from schurfer_analytics.early_momentum_net_evidence import build_funnel, compute_robustness

    dataset = _dataset(db_snapshot_at=CS + timedelta(days=2), cohort_end=CS + timedelta(days=1))
    funnel = build_funnel(dataset)
    robustness = compute_robustness(funnel.comparable)
    assert not hasattr(robustness.block_bootstrap, "samples")
    payload = json.loads(report_mod.render_json(_sample_report()))
    if payload["robustness"]["block_bootstrap"] is not None:
        assert "samples" not in payload["robustness"]["block_bootstrap"]


# --- report is read-only ---


def test_repository_module_never_issues_a_write_statement() -> None:
    import schurfer_analytics.early_momentum_net_evidence_repository as repo_module

    source = repo_module.__file__
    assert source is not None
    content = Path(source).read_text(encoding="utf-8")
    for keyword in ("INSERT INTO", "UPDATE ", "DELETE FROM", "TRUNCATE"):
        assert keyword not in content
