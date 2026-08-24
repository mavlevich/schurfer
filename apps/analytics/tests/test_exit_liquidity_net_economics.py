from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from schurfer_analytics.exit_liquidity_calibration_report import (
    EXIT_LIQUIDITY_COHORT_START,
    ExitLiquidityFilters,
)
from schurfer_analytics.exit_liquidity_net_economics import (
    DECISION_SAMPLE_SIZE,
    MINIMUM_CLUSTERS,
    MINIMUM_UTC_WEEKS,
    NetEconomicsRow,
    _run,
    build_coverage,
    build_net_economics_report,
    build_parser,
    compute_adjusted_net_pnl_usd,
    compute_exclusion_flags,
    normalize_exit_reason,
    render_json,
    render_markdown,
)
from schurfer_analytics.exit_liquidity_net_economics_dataset_artifact import (
    freeze as freeze_artifact,
)
from schurfer_analytics.research_dataset_artifact import iter_artifact_fingerprints
from schurfer_performance import PAPER_ACCOUNTING_VERSION


def _row(trade_id: int, **overrides: Any) -> NetEconomicsRow:
    exit_at = EXIT_LIQUIDITY_COHORT_START + timedelta(hours=trade_id)
    defaults: dict[str, Any] = {
        "trade_id": trade_id,
        "episode_id": None,
        "strategy_name": "pump_short",
        "strategy_version": "1",
        "symbol": "COTI/USDT:USDT",
        "exchange": "binance",
        "side": "short",
        "entry_at": exit_at - timedelta(hours=1),
        "exit_at": exit_at,
        "exit_reason": "max_hold age=60min",
        "size_usd": 50.0,
        "leverage": 5.0,
        "entry_price": 1.0,
        "exit_price": 0.99,
        "recorded_gross_pnl_usd": 0.5,
        "recorded_net_pnl_usd": 0.20,
        "fees_usd": 0.05,
        "funding_usd": 0.01,
        "entry_slippage_bps": 2.0,
        "modeled_exit_bps": 5.0,
        "accounting_version": PAPER_ACCOUNTING_VERSION,
        "accounting_status": "complete",
        "accounting_error": None,
        "observation_id": trade_id,
        "observed_at": exit_at - timedelta(seconds=1),
        "observation_exchange": "binance",
        "observation_symbol": "COTI/USDT:USDT",
        "observation_status": "sampled",
        "requested_notional_usd": 50.0,
        "filled_notional_usd": 50.0,
        "observed_mid": 0.995,
        "observed_spread_bps": 4.0,
        "observed_exit_bps": 6.0,
        "observed_ask_vwap": 0.996,
        "latency_ms": 100,
        "error": None,
    }
    defaults.update(overrides)
    return NetEconomicsRow(**defaults)


def _filters(days: int = 60) -> ExitLiquidityFilters:
    return ExitLiquidityFilters(
        since=EXIT_LIQUIDITY_COHORT_START, until=EXIT_LIQUIDITY_COHORT_START + timedelta(days=days)
    )


# adjusted_gross = 50 * (1.0 - 0.996) / 1.0 = 0.2
# entry_cost     = 50 * 2.0 / 10_000       = 0.01
# adjusted_net   = 0.2 - 0.01 - 0.05 - 0.01 = 0.13
_DEFAULT_ADJUSTED_NET_PNL_USD = 0.13


def test_formula_matches_hand_computed_primitives() -> None:
    assert compute_adjusted_net_pnl_usd(_row(1)) == pytest.approx(_DEFAULT_ADJUSTED_NET_PNL_USD)


def test_formula_reads_ask_vwap_directly_not_mid_plus_bps() -> None:
    """Regression (colleague review, 2026-08-25): mid + a flat notional-
    scaled ask_impact_bps charge is only equivalent to ask_vwap when
    mid == entry_price. On a losing short with mid far above entry, the
    old v1 formula understated exit cost by $0.25 on a $50/50%-move/100bps
    example -- reproduced here exactly."""
    row = _row(
        1,
        size_usd=50.0,
        entry_price=1.0,
        entry_slippage_bps=0.0,
        fees_usd=0.0,
        funding_usd=0.0,
        observed_mid=1.5,
        observed_exit_bps=100.0,
        observed_ask_vwap=1.515,  # mid * (1 + 100/10_000)
    )
    # True formula: gross = 50 * (1.0 - 1.515) / 1.0 = -25.75
    assert compute_adjusted_net_pnl_usd(row) == pytest.approx(-25.75)
    # The old (wrong) v1 formula would have given -25.5 -- a $0.25
    # overstatement of PnL. Confirm we are NOT reproducing that number.
    assert compute_adjusted_net_pnl_usd(row) != pytest.approx(-25.5)


def test_formula_correct_for_a_profitable_short_with_mid_below_entry() -> None:
    row = _row(
        1,
        size_usd=50.0,
        entry_price=1.0,
        entry_slippage_bps=0.0,
        fees_usd=0.0,
        funding_usd=0.0,
        observed_mid=0.8,
        observed_exit_bps=50.0,
        observed_ask_vwap=0.804,  # mid * (1 + 50/10_000)
    )
    # gross = 50 * (1.0 - 0.804) / 1.0 = 9.8
    assert compute_adjusted_net_pnl_usd(row) == pytest.approx(9.8)


def test_formula_correct_for_a_losing_short_with_mid_above_entry() -> None:
    row = _row(
        1,
        size_usd=50.0,
        entry_price=1.0,
        entry_slippage_bps=0.0,
        fees_usd=0.0,
        funding_usd=0.0,
        observed_mid=1.2,
        observed_exit_bps=50.0,
        observed_ask_vwap=1.206,  # mid * (1 + 50/10_000)
    )
    # gross = 50 * (1.0 - 1.206) / 1.0 = -10.3
    assert compute_adjusted_net_pnl_usd(row) == pytest.approx(-10.3)


def test_higher_observed_ask_vwap_lowers_adjusted_pnl() -> None:
    baseline = compute_adjusted_net_pnl_usd(_row(1, observed_ask_vwap=0.996))
    worse = compute_adjusted_net_pnl_usd(_row(1, observed_ask_vwap=1.02))
    assert worse < baseline


def test_lower_observed_ask_vwap_raises_adjusted_pnl() -> None:
    baseline = compute_adjusted_net_pnl_usd(_row(1, observed_ask_vwap=0.996))
    better = compute_adjusted_net_pnl_usd(_row(1, observed_ask_vwap=0.98))
    assert better > baseline


def test_long_side_is_not_supported_and_fails_closed() -> None:
    with pytest.raises(ValueError, match="only short is supported"):
        compute_adjusted_net_pnl_usd(_row(1, side="long"))


def test_leverage_does_not_multiply_the_dollar_pnl_a_second_time() -> None:
    """size_usd is already the full notional -- leverage must not scale
    adjusted_net_pnl_usd again on top of that."""
    five_x = compute_adjusted_net_pnl_usd(_row(1, leverage=5.0))
    ten_x = compute_adjusted_net_pnl_usd(_row(1, leverage=10.0))
    assert five_x == ten_x == pytest.approx(_DEFAULT_ADJUSTED_NET_PNL_USD)


def test_formula_never_reads_exit_price_or_recorded_pnl() -> None:
    naive_exit_price = compute_adjusted_net_pnl_usd(_row(1, exit_price=0.50))
    vwap_exit_price = compute_adjusted_net_pnl_usd(_row(1, exit_price=0.996))
    assert naive_exit_price == vwap_exit_price == pytest.approx(_DEFAULT_ADJUSTED_NET_PNL_USD)


def test_fees_and_funding_are_always_subtracted() -> None:
    base = compute_adjusted_net_pnl_usd(_row(1, fees_usd=0.0, funding_usd=0.0))
    with_costs = compute_adjusted_net_pnl_usd(_row(1, fees_usd=1.0, funding_usd=0.5))
    assert with_costs == pytest.approx(base - 1.5)


def test_missing_observation_blocks_adjusted_pnl_never_becomes_zero_cost() -> None:
    row = _row(
        1,
        observation_id=None,
        observed_at=None,
        observation_exchange=None,
        observation_symbol=None,
        observation_status=None,
        requested_notional_usd=None,
        filled_notional_usd=None,
        observed_mid=None,
        observed_spread_bps=None,
        observed_exit_bps=None,
        observed_ask_vwap=None,
        latency_ms=None,
    )
    flags = compute_exclusion_flags(row)
    assert flags.missing_observation is True
    assert flags.blocks_adjusted_pnl is True
    assert flags.primary_reason == "missing_observation"

    coverage = build_coverage((row,))
    assert coverage[0].adjusted_net_pnl_usd is None
    assert coverage[0].comparable is None


@pytest.mark.parametrize(
    ("overrides", "flag_name"),
    [
        ({"observation_status": "fetch_failed"}, "not_sampled"),
        ({"error": "timeout"}, "observation_error"),
        ({"observed_at": None}, "missing_observed_at"),
        ({"filled_notional_usd": None}, "missing_or_invalid_notional_fields"),
        ({"requested_notional_usd": None}, "missing_or_invalid_notional_fields"),
    ],
)
def test_incomplete_observation_fields_fail_closed_not_open(
    overrides: dict[str, object], flag_name: str
) -> None:
    """Regression (colleague review, 2026-08-25): these used to pass
    through the exclusion checks silently -- filled_notional_usd=None
    reached an AssertionError inside compute_adjusted_net_pnl_usd, and
    observed_at=None was treated as a valid fresh quote."""
    row = _row(1, **overrides)
    flags = compute_exclusion_flags(row)
    assert getattr(flags, flag_name) is True
    assert flags.blocks_adjusted_pnl is True

    # Must never reach compute_adjusted_net_pnl_usd (would assert/crash).
    coverage = build_coverage((row,))
    assert coverage[0].adjusted_net_pnl_usd is None


def test_incomplete_accounting_stays_unresolved_but_adjusted_pnl_is_still_computed() -> None:
    row = _row(1, accounting_status="incomplete", recorded_net_pnl_usd=None)
    flags = compute_exclusion_flags(row)
    assert flags.incomplete_accounting is True
    assert flags.blocks_adjusted_pnl is False

    coverage = build_coverage((row,))
    assert coverage[0].adjusted_net_pnl_usd == pytest.approx(_DEFAULT_ADJUSTED_NET_PNL_USD)
    assert coverage[0].comparable is None
    assert coverage[0].primary_reason == "incomplete_accounting"


def test_legacy_pre_263_malformed_identity_is_plain_identity_mismatch() -> None:
    row = _row(
        1,
        entry_at=datetime(2026, 8, 20, 16, 0, tzinfo=UTC),
        exit_at=datetime(2026, 8, 20, 17, 0, tzinfo=UTC),
        observation_symbol="WRONG/USDT:USDT",
    )
    flags = compute_exclusion_flags(row)
    assert flags.identity_mismatch is True
    assert flags.malformed_identity_post_263 is False
    assert flags.primary_reason == "identity_mismatch"


def test_post_263_malformed_identity_is_a_distinct_integrity_failure() -> None:
    row = _row(
        1,
        entry_at=datetime(2026, 8, 22, 0, 0, tzinfo=UTC),
        exit_at=datetime(2026, 8, 22, 1, 0, tzinfo=UTC),
        observation_symbol="WRONG/USDT:USDT",
    )
    flags = compute_exclusion_flags(row)
    assert flags.identity_mismatch is True
    assert flags.malformed_identity_post_263 is True
    assert flags.primary_reason == "malformed_identity_post_263"


@pytest.mark.parametrize(
    ("raw", "expected_reason", "expected_params"),
    [
        ("max_hold age=367min", "max_hold", "age=367min"),
        ("initial_sl move=-10.1%", "initial_sl", "move=-10.1%"),
        ("no_progress", "no_progress", None),
        ("trailing_stop trail=2%", "trailing_stop", "trail=2%"),
        ("something_unrecognized foo=1", "unknown", "something_unrecognized foo=1"),
        (None, "unknown", None),
        ("", "unknown", None),
    ],
)
def test_exit_reason_normalization(
    raw: str | None, expected_reason: str, expected_params: str | None
) -> None:
    reason, params = normalize_exit_reason(raw)
    assert reason == expected_reason
    assert params == expected_params


def test_duplicate_trade_id_is_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate trade"):
        build_coverage((_row(1), _row(1)))


def test_unsupported_accounting_version_is_excluded() -> None:
    row = _row(1, accounting_version="some_future_version")
    flags = compute_exclusion_flags(row)
    assert flags.unsupported_accounting_version is True
    assert flags.blocks_adjusted_pnl is True


def test_stale_quote_is_excluded() -> None:
    row = _row(1, observed_at=_row(1).exit_at - timedelta(seconds=200))
    flags = compute_exclusion_flags(row)
    assert flags.stale_quote is True
    assert flags.blocks_adjusted_pnl is True


def test_requested_notional_mismatch_is_excluded() -> None:
    row = _row(1, requested_notional_usd=40.0)
    flags = compute_exclusion_flags(row)
    assert flags.requested_notional_mismatch is True


def test_insufficient_visible_depth_is_excluded() -> None:
    row = _row(1, filled_notional_usd=20.0)
    flags = compute_exclusion_flags(row)
    assert flags.insufficient_visible_depth is True


def _cohort_rows(
    n: int, *, adjusted_bias: float = 0.0, clusters: int = 1, weeks: int = 1
) -> tuple[NetEconomicsRow, ...]:
    """`n` clean, comparable, complete rows spread across `clusters`
    symbols and `weeks` distinct UTC weeks. `adjusted_bias` is added
    directly to `observed_ask_vwap` (higher -> lower adjusted PnL, since
    this is a short) to push the mean positive/negative for verdict
    tests."""
    rows = []
    symbols = [f"SYM{i}/USDT:USDT" for i in range(max(clusters, 1))]
    for i in range(1, n + 1):
        # Minutes, not hours, for the intra-week offset: with n up to a few
        # hundred, `timedelta(hours=i)` could itself cross into the next
        # ISO week even when `weeks=1` was requested, silently breaking the
        # "exactly `weeks` distinct UTC weeks" contract this helper claims.
        week_offset = timedelta(weeks=(i % max(weeks, 1)))
        exit_at = EXIT_LIQUIDITY_COHORT_START + week_offset + timedelta(minutes=i)
        rows.append(
            _row(
                i,
                symbol=symbols[i % len(symbols)],
                observation_symbol=symbols[i % len(symbols)],
                entry_at=exit_at - timedelta(hours=1),
                exit_at=exit_at,
                observed_at=exit_at - timedelta(seconds=1),
                observed_ask_vwap=0.996 + adjusted_bias,
            )
        )
    return tuple(rows)


# --- P1 #4 hybrid-verdict test matrix (colleague review, 2026-08-25) -------


def test_matrix_1_small_n_negative_gives_insufficient_data_with_diagnostic() -> None:
    rows = _cohort_rows(5, adjusted_bias=5.0, clusters=1, weeks=1)  # higher ask_vwap -> negative
    report = build_net_economics_report(
        rows,
        _filters(),
        generated_at=datetime(2026, 8, 25, tzinfo=UTC),
        code_revision="abc123",
        working_tree_dirty=False,
    )
    assert report.metrics is not None
    assert report.metrics.mean_adjusted_net_pnl_usd < 0
    assert report.verdict == "insufficient_data"
    assert report.diagnostic == "negative_point_estimate"


def test_matrix_2_mature_n_negative_undiversified_gives_fail_with_diversity_recorded() -> None:
    rows = _cohort_rows(max(DECISION_SAMPLE_SIZE, 110), adjusted_bias=5.0, clusters=1, weeks=1)
    report = build_net_economics_report(
        rows,
        _filters(days=200),
        generated_at=datetime(2026, 8, 25, tzinfo=UTC),
        code_revision="abc123",
        working_tree_dirty=False,
    )
    assert report.metrics is not None
    assert report.metrics.mean_adjusted_net_pnl_usd < 0
    assert report.verdict == "fail"
    assert report.diagnostic is None
    # Diversity numbers still recorded, not hidden by the FAIL verdict.
    assert report.readiness["evidence_floor"]["clusters_actual"] == 1
    assert report.readiness["evidence_floor"]["utc_weeks_actual"] == 1


def test_matrix_3_mature_n_positive_undiversified_gives_insufficient_data() -> None:
    rows = _cohort_rows(max(DECISION_SAMPLE_SIZE, 110), adjusted_bias=-0.05, clusters=1, weeks=1)
    report = build_net_economics_report(
        rows,
        _filters(days=200),
        generated_at=datetime(2026, 8, 25, tzinfo=UTC),
        code_revision="abc123",
        working_tree_dirty=False,
    )
    assert report.metrics is not None
    assert report.metrics.mean_adjusted_net_pnl_usd > 0
    assert report.verdict == "insufficient_data"
    assert report.diagnostic is None


def test_matrix_4_mature_diversified_negative_gives_fail() -> None:
    rows = _cohort_rows(
        max(DECISION_SAMPLE_SIZE, 120),
        adjusted_bias=5.0,
        clusters=MINIMUM_CLUSTERS + 5,
        weeks=MINIMUM_UTC_WEEKS + 2,
    )
    report = build_net_economics_report(
        rows,
        _filters(days=200),
        generated_at=datetime(2026, 8, 25, tzinfo=UTC),
        code_revision="abc123",
        working_tree_dirty=False,
    )
    assert report.metrics is not None
    assert report.metrics.mean_adjusted_net_pnl_usd < 0
    assert report.verdict == "fail"


def test_matrix_5_and_6_mature_diversified_positive_reaches_a_positive_verdict() -> None:
    rows = _cohort_rows(
        max(DECISION_SAMPLE_SIZE, 120),
        adjusted_bias=-0.05,
        clusters=MINIMUM_CLUSTERS + 5,
        weeks=MINIMUM_UTC_WEEKS + 2,
    )
    report = build_net_economics_report(
        rows,
        _filters(days=200),
        generated_at=datetime(2026, 8, 25, tzinfo=UTC),
        code_revision="abc123",
        working_tree_dirty=False,
    )
    assert report.metrics is not None
    assert report.metrics.mean_adjusted_net_pnl_usd > 0
    # Whether this lands as fragile or robust depends on the deterministic
    # bootstrap/leave-one-out over this synthetic data -- either is a valid
    # positive outcome; what matters is it is NOT insufficient_data/fail.
    assert report.verdict in (
        "historical_positive_requires_forward_confirmation",
        "fragile_positive",
    )


def test_bootstrap_and_fingerprint_are_deterministic() -> None:
    rows = _cohort_rows(50, clusters=10, weeks=5)
    filters = _filters(days=90)
    report1 = build_net_economics_report(
        rows,
        filters,
        generated_at=datetime(2026, 8, 25, tzinfo=UTC),
        code_revision="abc123",
        working_tree_dirty=False,
    )
    report2 = build_net_economics_report(
        rows,
        filters,
        generated_at=datetime(2026, 8, 25, tzinfo=UTC),
        code_revision="abc123",
        working_tree_dirty=False,
    )
    assert report1.manifest["input_fingerprint"] == report2.manifest["input_fingerprint"]
    assert report1.robustness is not None
    assert report2.robustness is not None
    r1, r2 = report1.robustness, report2.robustness
    assert r1.bootstrap_point_estimate == r2.bootstrap_point_estimate
    assert r1.bootstrap_lower_bound == r2.bootstrap_lower_bound
    assert r1.bootstrap_upper_bound == r2.bootstrap_upper_bound

    changed = list(rows)
    original_ask_vwap = changed[0].observed_ask_vwap
    assert original_ask_vwap is not None
    changed[0] = replace(changed[0], observed_ask_vwap=original_ask_vwap + 1.0)
    report3 = build_net_economics_report(
        tuple(changed),
        filters,
        generated_at=datetime(2026, 8, 25, tzinfo=UTC),
        code_revision="abc123",
        working_tree_dirty=False,
    )
    assert report3.manifest["input_fingerprint"] != report1.manifest["input_fingerprint"]


def test_markdown_and_json_render_without_error_and_state_funding_caveat() -> None:
    rows = _cohort_rows(10, clusters=3, weeks=2)
    report = build_net_economics_report(
        rows,
        _filters(days=30),
        generated_at=datetime(2026, 8, 25, tzinfo=UTC),
        code_revision="abc123",
        working_tree_dirty=False,
    )
    markdown = render_markdown(report)
    assert "funding_usd is a fixed conservative-rate model" in markdown
    assert "Verdict" in markdown

    payload = json.loads(render_json(report))
    assert payload["manifest"]["funding_is_modeled_not_observed"]
    assert payload["verdict"] == report.verdict
    assert payload["diagnostic"] == report.diagnostic


def test_manifest_records_formula_cost_model_and_bootstrap_provenance() -> None:
    rows = _cohort_rows(5, clusters=2, weeks=1)
    report = build_net_economics_report(
        rows,
        _filters(days=30),
        generated_at=datetime(2026, 8, 25, tzinfo=UTC),
        code_revision="abc123",
        working_tree_dirty=False,
    )
    manifest = report.manifest
    assert manifest["formula_version"] == "ask_vwap_primitives_v2"
    assert manifest["cost_model_version"]
    assert manifest["allowed_strategy_identities"] == [["pump_short", "1"]]
    assert manifest["bootstrap"]["seed"]
    assert manifest["bootstrap"]["iterations"]
    assert manifest["bootstrap"]["confidence_level"]
    assert manifest["bootstrap"]["version"]


async def test_run_from_artifact_reads_frozen_rows_without_touching_the_database(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    rows = _cohort_rows(5, clusters=2, weeks=1)
    filters = _filters(days=30)
    _outcome, manifest = freeze_artifact(
        rows, filters, code_revision="abc123", working_tree_dirty=False, directory=str(tmp_path)
    )
    assert manifest is not None

    args = build_parser().parse_args(
        [
            "--code-revision",
            "abc123",
            "--no-working-tree-dirty",
            "--from-artifact",
            manifest.fingerprint,
            "--artifact-directory",
            str(tmp_path),
            "--format",
            "json",
        ]
    )
    output = await _run(args)
    payload = json.loads(output)
    assert payload["readiness"]["closed_short_paper_trades"] == 5
    assert payload["manifest"]["source_artifact"]["fingerprint"] == manifest.fingerprint


async def test_run_freeze_artifact_persists_rows_and_embeds_source_artifact_in_same_run(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Regression (colleague review, 2026-08-25): manifest.source_artifact
    used to stay null for the SAME run that just froze the artifact --
    only a later --from-artifact run would see it."""
    rows = _cohort_rows(3, clusters=1, weeks=1)

    class _FakeRepository:
        @classmethod
        def from_url(cls, db_url: str) -> _FakeRepository:
            return cls()

        async def load(self, filters: ExitLiquidityFilters) -> tuple[NetEconomicsRow, ...]:
            return rows

        async def close(self) -> None:
            return None

    monkeypatch.setenv("DATABASE_URL", "postgresql://fake")
    monkeypatch.setattr(
        "schurfer_analytics.exit_liquidity_net_economics_repository.ExitLiquidityNetEconomicsRepository",
        _FakeRepository,
    )
    args = build_parser().parse_args(
        [
            "--code-revision",
            "abc123",
            "--no-working-tree-dirty",
            "--freeze-artifact",
            "--artifact-directory",
            str(tmp_path),
            "--format",
            "json",
        ]
    )
    output = await _run(args)
    payload = json.loads(output)

    captured = capsys.readouterr()
    assert "[research-dataset-artifact] created fingerprint=" in captured.err
    fingerprints = iter_artifact_fingerprints(directory=str(tmp_path))
    assert len(fingerprints) == 1
    assert payload["manifest"]["source_artifact"]["fingerprint"] == fingerprints[0]
