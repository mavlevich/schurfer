from __future__ import annotations

from datetime import UTC, datetime, timedelta

from schurfer_analytics.pump_short_reentry_audit_report import (
    COOLDOWN_SECONDS,
    ComparableRow,
    OperationalRow,
    ReentryAuditFilters,
    _fingerprint,
    _JoinedRow,
    _max_drawdown_usd,
    build_comparable_rows,
    build_event_rollup,
    build_operational_rows,
    classify_transitions,
    compute_reentry_opportunity,
    first_open_per_event,
    normalize_symbol_base,
    orphan_trades_statement,
    reentry_decisions_statement,
    render_markdown,
    summarize_transitions,
)
from schurfer_analytics.pump_short_reentry_audit_report import (
    ReentryAuditManifest as _Manifest,
)
from schurfer_analytics.pump_short_reentry_audit_report import (
    ReentryAuditReport as _Report,
)
from schurfer_performance.accounting import PAPER_ACCOUNTING_VERSION
from sqlalchemy.dialects import postgresql

T0 = datetime(2026, 8, 1, tzinfo=UTC)


def _sql(statement: object) -> str:
    return str(
        statement.compile(  # type: ignore[attr-defined]
            dialect=postgresql.dialect(),  # type: ignore[no-untyped-call]
            compile_kwargs={"literal_binds": True},
        )
    )


# --- normalize_symbol_base ---------------------------------------------------


def test_normalize_symbol_base_splits_on_first_slash() -> None:
    assert normalize_symbol_base("TUT/USDT:USDT") == "TUT"
    assert normalize_symbol_base("BTC/USDT") == "BTC"


def test_normalize_symbol_base_handles_unicode_ticker() -> None:
    assert normalize_symbol_base("龙虾/USDT:USDT") == "龙虾"


def test_normalize_symbol_base_returns_whole_string_when_no_slash() -> None:
    # Malformed input should not raise — the identity-consistency check
    # downstream is what flags the mismatch.
    assert normalize_symbol_base("MALFORMED") == "MALFORMED"


# --- query builders (pure, no DB) -------------------------------------------


def test_reentry_decisions_statement_scopes_actions_window_and_strategy() -> None:
    filters = ReentryAuditFilters(
        since=T0, until=T0 + timedelta(days=1), strategy_versions=("pump_short_v1_market_quality",)
    )
    sql = _sql(reentry_decisions_statement(filters))
    assert "trade_decisions.action IN ('opened', 'opened_dry_run')" in sql
    assert "trade_decisions.strategy_version IN ('pump_short_v1_market_quality')" in sql
    assert "trade_decisions.ts >= '2026-08-01 00:00:00+00:00'" in sql
    assert "trade_decisions.ts < '2026-08-02 00:00:00+00:00'" in sql
    assert "LEFT OUTER JOIN app.trades" in sql
    assert "setup_context ->> 'decision_id'" in sql


def test_orphan_trades_statement_checks_entry_at_window_and_missing_link() -> None:
    filters = ReentryAuditFilters(
        since=T0, until=T0 + timedelta(days=1), strategy_versions=("pump_short_v1_market_quality",)
    )
    sql = _sql(orphan_trades_statement(filters))
    assert "trades.entry_at >= '2026-08-01 00:00:00+00:00'" in sql
    assert "trades.entry_at < '2026-08-02 00:00:00+00:00'" in sql
    assert "NOT (EXISTS" in sql
    assert "setup_context ->> 'decision_id'" in sql


def test_orphan_trades_statement_scopes_paper_and_strategy_version() -> None:
    filters = ReentryAuditFilters(
        since=T0, until=T0 + timedelta(days=1), strategy_versions=("pump_short_v1_market_quality",)
    )
    sql = _sql(orphan_trades_statement(filters))
    assert "setup_context ->> 'paper'" in sql
    assert "setup_context ->> 'strategy_version'" in sql
    assert "'pump_short_v1_market_quality'" in sql


# --- build_comparable_rows funnel -------------------------------------------


def _joined(
    *,
    row_id: int = 1,
    decision_id: str | None = "dec-1",
    ts: datetime = T0,
    base: str = "TUT",
    exchange: str = "xt",
    pump_event_id: int | None = 100,
    trade_id: int | None = 10,
    trade_symbol: str | None = "TUT/USDT:USDT",
    trade_exchange: str | None = "xt",
    entry_at: datetime | None = T0,
    exit_at: datetime | None = T0 + timedelta(hours=1),
    trade_status: str | None = "closed",
    accounting_version: str | None = PAPER_ACCOUNTING_VERSION,
    accounting_status: str | None = "complete",
    net_pnl_usd: float | None = 1.0,
    net_pnl_pct: float | None = 2.0,
    event_closed_at: datetime | None = None,
) -> _JoinedRow:
    return _JoinedRow(
        decision_row_id=row_id,
        decision_id=decision_id,
        ts=ts,
        base=base,
        exchange=exchange,
        pump_event_id=pump_event_id,
        strategy_version="pump_short_v1_market_quality",
        trade_id=trade_id,
        trade_symbol=trade_symbol,
        trade_exchange=trade_exchange,
        entry_at=entry_at,
        exit_at=exit_at,
        trade_status=trade_status,
        accounting_version=accounting_version,
        accounting_status=accounting_status,
        net_pnl_usd=net_pnl_usd,
        net_pnl_pct=net_pnl_pct,
        event_closed_at=event_closed_at,
    )


def test_funnel_keeps_a_fully_valid_row_as_comparable() -> None:
    funnel, comparable = build_comparable_rows([_joined()])
    assert funnel[-1].name == "comparable"
    assert funnel[-1].count == 1
    assert len(comparable) == 1


def test_funnel_excludes_row_with_no_matching_trade() -> None:
    funnel, comparable = build_comparable_rows([_joined(trade_id=None, trade_symbol=None)])
    assert comparable == ()
    linked_step = next(step for step in funnel if step.name == "linked_to_trade")
    assert linked_step.count == 0
    assert ("no_matching_trade_row", 1) in linked_step.exclusion_reasons


def test_funnel_excludes_duplicate_trade_link() -> None:
    # Same decision_row_id matched by two different trade rows — the JSON
    # decision_id link has no DB-level uniqueness guarantee, so this must
    # fail closed (exclude both) rather than arbitrarily keep one.
    rows = [
        _joined(row_id=1, trade_id=10),
        _joined(row_id=1, trade_id=11),
    ]
    funnel, comparable = build_comparable_rows(rows)
    assert comparable == ()
    step = next(s for s in funnel if s.name == "unique_trade_link")
    assert step.count == 0
    assert ("duplicate_trade_link", 2) in step.exclusion_reasons


def test_funnel_excludes_row_with_no_pump_event_id() -> None:
    funnel, comparable = build_comparable_rows([_joined(pump_event_id=None)])
    assert comparable == ()
    step = next(s for s in funnel if s.name == "has_pump_event_id")
    assert step.count == 0
    assert ("decision_missing_pump_event_id", 1) in step.exclusion_reasons


def test_funnel_excludes_open_trade_as_right_censored() -> None:
    funnel, comparable = build_comparable_rows([_joined(trade_status="open")])
    assert comparable == ()
    step = next(s for s in funnel if s.name == "trade_closed")
    assert ("trade_status_open", 1) in step.exclusion_reasons


def test_funnel_excludes_legacy_accounting() -> None:
    funnel, comparable = build_comparable_rows(
        [_joined(accounting_version="legacy_price_only_v1", accounting_status="legacy")]
    )
    assert comparable == ()
    step = next(s for s in funnel if s.name == "accounting_complete")
    assert ("accounting_legacy", 1) in step.exclusion_reasons


def test_funnel_excludes_incomplete_paper_accounting() -> None:
    funnel, comparable = build_comparable_rows([_joined(accounting_status="incomplete")])
    assert comparable == ()
    step = next(s for s in funnel if s.name == "accounting_complete")
    assert ("accounting_incomplete", 1) in step.exclusion_reasons


def test_funnel_excludes_base_identity_mismatch() -> None:
    funnel, comparable = build_comparable_rows([_joined(trade_symbol="OTHER/USDT:USDT")])
    assert comparable == ()
    step = next(s for s in funnel if s.name == "identity_consistent")
    assert ("identity_mismatch_base_or_exchange", 1) in step.exclusion_reasons


def test_funnel_excludes_exchange_identity_mismatch() -> None:
    funnel, comparable = build_comparable_rows([_joined(trade_exchange="binance")])
    assert comparable == ()
    step = next(s for s in funnel if s.name == "identity_consistent")
    assert ("identity_mismatch_base_or_exchange", 1) in step.exclusion_reasons


def test_funnel_excludes_missing_net_pnl() -> None:
    funnel, comparable = build_comparable_rows([_joined(net_pnl_usd=None)])
    assert comparable == ()
    step = next(s for s in funnel if s.name == "comparable")
    assert ("missing_net_pnl", 1) in step.exclusion_reasons


def test_funnel_excludes_missing_exit_at_with_its_own_reason() -> None:
    funnel, comparable = build_comparable_rows([_joined(exit_at=None)])
    assert comparable == ()
    step = next(s for s in funnel if s.name == "comparable")
    assert ("missing_exit_at", 1) in step.exclusion_reasons
    assert ("missing_net_pnl", 1) not in step.exclusion_reasons


# --- build_operational_rows ---------------------------------------------------


def test_build_operational_rows_includes_open_and_incomplete_trades() -> None:
    # An open position and an incomplete-accounting trade both survive into
    # the operational set even though neither would ever reach `comparable`.
    rows = [
        _joined(row_id=1, trade_status="open"),
        _joined(row_id=2, accounting_status="incomplete", pump_event_id=200),
    ]
    operational = build_operational_rows(rows)
    assert len(operational) == 2


def test_build_operational_rows_includes_decisions_with_no_trade_at_all() -> None:
    row = _joined(row_id=1, trade_id=None, trade_symbol=None, entry_at=None, exit_at=None)
    operational = build_operational_rows([row])
    assert len(operational) == 1
    assert operational[0].entry_at is None


def test_build_operational_rows_excludes_missing_pump_event_id() -> None:
    row = _joined(pump_event_id=None)
    assert build_operational_rows([row]) == ()


def test_build_operational_rows_dedupes_a_duplicate_trade_link() -> None:
    # The same decision_row_id fanned out across two trade rows (see the
    # unique_trade_link funnel test) must still contribute exactly one
    # operational row — duplication here would fabricate a spurious
    # near-zero-gap transition against itself.
    rows = [
        _joined(row_id=1, trade_id=10),
        _joined(row_id=1, trade_id=11),
    ]
    operational = build_operational_rows(rows)
    assert len(operational) == 1


# --- classify_transitions ----------------------------------------------------


def _comparable(
    *,
    row_id: int,
    ts: datetime,
    base: str = "TUT",
    pump_event_id: int,
    entry_at: datetime | None = None,
    exit_at: datetime | None = None,
    net_pnl_usd: float = 0.0,
    net_pnl_pct: float = 0.0,
    event_closed_at: datetime | None = None,
) -> ComparableRow:
    return ComparableRow(
        decision_row_id=row_id,
        decision_id=f"dec-{row_id}",
        ts=ts,
        base=base,
        exchange="xt",
        pump_event_id=pump_event_id,
        trade_id=row_id,
        entry_at=entry_at or ts,
        exit_at=exit_at or ts,
        net_pnl_usd=net_pnl_usd,
        net_pnl_pct=net_pnl_pct,
        event_closed_at=event_closed_at,
    )


def _operational(
    *,
    row_id: int,
    ts: datetime,
    base: str = "TUT",
    pump_event_id: int,
    entry_at: datetime | None = None,
    exit_at: datetime | None = None,
) -> OperationalRow:
    return OperationalRow(
        decision_row_id=row_id,
        base=base,
        pump_event_id=pump_event_id,
        ts=ts,
        entry_at=entry_at,
        exit_at=exit_at,
    )


def test_classify_transitions_same_event_after_24h_matches_tut() -> None:
    first = _operational(
        row_id=1,
        ts=T0,
        pump_event_id=3518,
        entry_at=T0,
        exit_at=T0 + timedelta(hours=1, minutes=32),
    )
    second = _operational(
        row_id=2,
        ts=T0 + timedelta(hours=24, minutes=6),
        pump_event_id=3518,
        entry_at=T0 + timedelta(hours=24, minutes=6),
    )
    transitions = classify_transitions([first, second])
    assert len(transitions) == 1
    row = transitions[0]
    assert row.transition_type == "same_event_after_24h"
    assert row.seconds_since_previous_decision >= COOLDOWN_SECONDS
    assert row.seconds_since_previous_exit is not None
    assert row.seconds_since_previous_exit < row.seconds_since_previous_decision


def test_classify_transitions_same_event_under_24h_is_a_violation() -> None:
    first = _operational(row_id=1, ts=T0, pump_event_id=1)
    second = _operational(row_id=2, ts=T0 + timedelta(hours=2), pump_event_id=1)
    transitions = classify_transitions([first, second])
    assert transitions[0].transition_type == "same_event_under_24h"


def test_classify_transitions_cross_event_under_24h_is_a_violation() -> None:
    first = _operational(row_id=1, ts=T0, pump_event_id=1)
    second = _operational(row_id=2, ts=T0 + timedelta(hours=2), pump_event_id=2)
    transitions = classify_transitions([first, second])
    assert transitions[0].transition_type == "cross_event_under_24h"


def test_classify_transitions_cross_event_after_24h_is_independent() -> None:
    first = _operational(row_id=1, ts=T0, pump_event_id=1)
    second = _operational(row_id=2, ts=T0 + timedelta(days=3), pump_event_id=2)
    transitions = classify_transitions([first, second])
    assert transitions[0].transition_type == "cross_event_after_24h"


def test_classify_transitions_is_pairwise_across_a_base_with_three_opens() -> None:
    rows = [
        _operational(row_id=1, ts=T0, pump_event_id=1),
        _operational(row_id=2, ts=T0 + timedelta(hours=25), pump_event_id=1),
        _operational(row_id=3, ts=T0 + timedelta(hours=26), pump_event_id=2),
    ]
    transitions = classify_transitions(rows)
    assert [row.transition_type for row in transitions] == [
        "same_event_after_24h",
        "cross_event_under_24h",
    ]


def test_classify_transitions_never_compares_across_different_bases() -> None:
    rows = [
        _operational(row_id=1, ts=T0, base="TUT", pump_event_id=1),
        _operational(row_id=2, ts=T0 + timedelta(minutes=1), base="BLESS", pump_event_id=2),
    ]
    assert classify_transitions(rows) == ()


def test_classify_transitions_handles_missing_entry_and_exit_gracefully() -> None:
    # An operational row built from a decision with no linked trade at all
    # (entry_at/exit_at both None) must still classify on ts alone.
    first = _operational(row_id=1, ts=T0, pump_event_id=1, entry_at=None, exit_at=None)
    second = _operational(row_id=2, ts=T0 + timedelta(hours=2), pump_event_id=1)
    transitions = classify_transitions([first, second])
    assert transitions[0].transition_type == "same_event_under_24h"
    assert transitions[0].seconds_since_previous_entry is None
    assert transitions[0].seconds_since_previous_exit is None


def test_summarize_transitions_counts_violations() -> None:
    rows = [
        _operational(row_id=1, ts=T0, pump_event_id=1),
        # same_event_under_24h:
        _operational(row_id=2, ts=T0 + timedelta(hours=2), pump_event_id=1),
        # cross_event_under_24h:
        _operational(row_id=3, ts=T0 + timedelta(hours=4), pump_event_id=2),
    ]
    transitions = classify_transitions(rows)
    summary, violations = summarize_transitions(transitions)
    assert violations == 2
    assert sum(row.count for row in summary) == 2


# --- event rollup -------------------------------------------------------------


def test_build_event_rollup_separates_single_and_multiple_entry_events() -> None:
    rows = [
        _comparable(row_id=1, ts=T0, pump_event_id=1, net_pnl_usd=-5.93),
        _comparable(
            row_id=2, ts=T0 + timedelta(hours=24, minutes=6), pump_event_id=1, net_pnl_usd=6.24
        ),
        _comparable(row_id=3, ts=T0 + timedelta(days=2), pump_event_id=2, net_pnl_usd=1.0),
    ]
    summary, detail = build_event_rollup(rows)
    assert summary.total_events == 2
    assert summary.single_entry_events == 1
    assert summary.multiple_entry_events == 1
    assert len(detail) == 1
    row = detail[0]
    assert row.pump_event_id == 1
    assert row.num_opens == 2
    assert round(row.event_net_pnl_usd, 2) == 0.31
    assert row.first_open_only_net_pnl_usd == -5.93
    assert round(row.delta_usd, 2) == 6.24


# --- first_open_per_event / reentry opportunity ------------------------------


def test_first_open_per_event_keeps_earliest_ts() -> None:
    rows = [
        _comparable(row_id=2, ts=T0 + timedelta(hours=1), pump_event_id=1),
        _comparable(row_id=1, ts=T0, pump_event_id=1),
    ]
    kept = first_open_per_event(rows)
    assert len(kept) == 1
    assert kept[0].decision_row_id == 1


def test_reentry_opportunity_remained_open_when_event_closes_late() -> None:
    row = _comparable(
        row_id=1,
        ts=T0,
        pump_event_id=1,
        event_closed_at=T0 + timedelta(hours=25),
    )
    stats = compute_reentry_opportunity([row], as_of=T0 + timedelta(days=10))
    assert stats.remained_observable_24h_after_first_open == 1
    assert stats.did_not_remain_observable_24h == 0
    assert stats.right_censored_still_open == 0


def test_reentry_opportunity_did_not_remain_open_when_event_closes_early() -> None:
    row = _comparable(
        row_id=1,
        ts=T0,
        pump_event_id=1,
        event_closed_at=T0 + timedelta(hours=2),
    )
    stats = compute_reentry_opportunity([row], as_of=T0 + timedelta(days=10))
    assert stats.did_not_remain_observable_24h == 1


def test_reentry_opportunity_right_censored_when_still_open_and_recent() -> None:
    row = _comparable(row_id=1, ts=T0, pump_event_id=1, event_closed_at=None)
    stats = compute_reentry_opportunity([row], as_of=T0 + timedelta(hours=1))
    assert stats.right_censored_still_open == 1


def test_reentry_opportunity_counts_as_remained_when_still_open_past_24h() -> None:
    # Never closed, but 24h have already passed as of the cutoff — that
    # alone already proves >=24h of structural observability as of `as_of`.
    row = _comparable(row_id=1, ts=T0, pump_event_id=1, event_closed_at=None)
    stats = compute_reentry_opportunity([row], as_of=T0 + timedelta(hours=25))
    assert stats.remained_observable_24h_after_first_open == 1
    assert stats.right_censored_still_open == 0


def test_reentry_opportunity_clips_closure_after_the_cutoff() -> None:
    # The event actually closed 30h after first open, but the report's own
    # `as_of` (filters.until) is only 10h after first open. Using the real
    # closed_at here would be peeking past the report's own window — this
    # must be treated exactly like "still open as of as_of" instead.
    row = _comparable(
        row_id=1,
        ts=T0,
        pump_event_id=1,
        event_closed_at=T0 + timedelta(hours=30),
    )
    stats = compute_reentry_opportunity([row], as_of=T0 + timedelta(hours=10))
    assert stats.right_censored_still_open == 1
    assert stats.did_not_remain_observable_24h == 0
    assert stats.remained_observable_24h_after_first_open == 0


def test_reentry_opportunity_future_closure_but_as_of_already_past_24h() -> None:
    # Same future-closure setup, but `as_of` itself is already >=24h past
    # first open — that alone proves structural observability regardless of
    # what happens to the event afterwards.
    row = _comparable(
        row_id=1,
        ts=T0,
        pump_event_id=1,
        event_closed_at=T0 + timedelta(hours=30),
    )
    stats = compute_reentry_opportunity([row], as_of=T0 + timedelta(hours=25))
    assert stats.remained_observable_24h_after_first_open == 1


# --- max drawdown -------------------------------------------------------------


def test_max_drawdown_tracks_the_largest_peak_to_trough_drop() -> None:
    rows = [
        _comparable(row_id=1, ts=T0, exit_at=T0, pump_event_id=1, net_pnl_usd=10.0),
        _comparable(
            row_id=2,
            ts=T0 + timedelta(hours=1),
            exit_at=T0 + timedelta(hours=1),
            pump_event_id=2,
            net_pnl_usd=-15.0,
        ),
        _comparable(
            row_id=3,
            ts=T0 + timedelta(hours=2),
            exit_at=T0 + timedelta(hours=2),
            pump_event_id=3,
            net_pnl_usd=2.0,
        ),
    ]
    # cumulative: 10, -5, -3 -> peak 10, trough -5 -> drawdown 15
    assert _max_drawdown_usd(rows) == 15.0


def test_max_drawdown_is_zero_for_a_monotonically_winning_series() -> None:
    rows = [
        _comparable(row_id=1, ts=T0, exit_at=T0, pump_event_id=1, net_pnl_usd=1.0),
        _comparable(
            row_id=2,
            ts=T0 + timedelta(hours=1),
            exit_at=T0 + timedelta(hours=1),
            pump_event_id=2,
            net_pnl_usd=2.0,
        ),
    ]
    assert _max_drawdown_usd(rows) == 0.0


def test_max_drawdown_orders_by_exit_at_not_decision_time() -> None:
    # Opened in an order (ts) that would show a peak-then-crash if sorted by
    # decision time, but the actual realization order (exit_at) is reversed
    # — the loss is realized FIRST, so there should be no drawdown at all
    # once ordered correctly (cumulative only ever goes up).
    rows = [
        _comparable(
            row_id=1,
            ts=T0,
            entry_at=T0,
            exit_at=T0 + timedelta(hours=5),  # realized last
            pump_event_id=1,
            net_pnl_usd=10.0,
        ),
        _comparable(
            row_id=2,
            ts=T0 + timedelta(minutes=1),
            entry_at=T0 + timedelta(minutes=1),
            exit_at=T0 + timedelta(hours=1),  # realized first
            pump_event_id=2,
            net_pnl_usd=-3.0,
        ),
    ]
    # By exit_at: -3 realized first (cumulative -3, peak 0, drawdown 3), then
    # +10 (cumulative 7, new peak). Max drawdown is 3, not 0 (which a
    # ts-ordered walk of [10, -3] would wrongly report as 13).
    assert _max_drawdown_usd(rows) == 3.0


# --- fingerprint ---------------------------------------------------------


def test_fingerprint_is_stable_for_identical_inputs() -> None:
    rows = [_joined()]
    assert _fingerprint(rows, (1, 2, 3)) == _fingerprint(rows, (1, 2, 3))


def test_fingerprint_changes_when_orphan_ids_differ() -> None:
    rows = [_joined()]
    assert _fingerprint(rows, (1, 2, 3)) != _fingerprint(rows, (1, 2, 4))


def test_fingerprint_changes_when_a_row_excluded_from_comparable_differs() -> None:
    # Two datasets whose `comparable` output could coincidentally match (a
    # single valid row in both) must still fingerprint differently if the
    # raw joined rows differ — e.g. a second, funnel-excluded decision only
    # present in one of them.
    base_rows = [_joined(row_id=1)]
    extra_rows = [_joined(row_id=1), _joined(row_id=2, trade_id=None, trade_symbol=None)]
    assert _fingerprint(base_rows, ()) != _fingerprint(extra_rows, ())


# --- render smoke test ---------------------------------------------------


def test_render_markdown_smoke() -> None:
    from schurfer_analytics.pump_short_reentry_audit_report import (
        EconomicsSummary,
        EventRollupSummary,
        FunnelStep,
        OrphanTradesDiagnostic,
        ReentryOpportunityStats,
    )

    report = _Report(
        manifest=_Manifest(
            code_revision="abc123",
            working_tree_dirty=False,
            generated_at=T0,
            dataset_since=T0,
            dataset_until_exclusive=T0 + timedelta(days=1),
            input_fingerprint="deadbeef",
        ),
        funnel=(
            FunnelStep(
                name="all_open_decisions", count=1, share_of_previous_pct=None, exclusion_reasons=()
            ),
        ),
        orphan_trades=OrphanTradesDiagnostic(0, None, None, ()),
        transition_summary=(),
        base_24h_invariant_violations=0,
        event_rollup=EventRollupSummary(
            total_events=1, single_entry_events=1, multiple_entry_events=0
        ),
        multiple_entry_events=(),
        economics_all_actual_trades=EconomicsSummary(
            "all_actual_trades", 1, 1.0, 1.0, 100.0, 2.0, 0.0
        ),
        economics_actual_first_open_per_event=EconomicsSummary(
            "actual_first_open_per_event", 1, 1.0, 1.0, 100.0, 2.0, 0.0
        ),
        reentry_opportunity=ReentryOpportunityStats(1, 1, 0, 0),
    )
    text = render_markdown(report)
    assert "Pump-Short Re-entry Audit" in text
    assert "Measurement-only" in text
    assert "Future fix options" in text
    assert "abc123" in text
