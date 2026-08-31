from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
import schurfer_analytics.serial_pump_regimes_report as report_module
from schurfer_analytics.exchange_registry import EXCHANGE_FACTORIES
from schurfer_analytics.market_path_cache import MarketPathCacheCorruptError
from schurfer_analytics.momentum_universe_identity_repository import (
    MomentumUniverseIdentityRepository,
)
from schurfer_analytics.ohlcv import TIMEFRAME_MS, Candle
from schurfer_analytics.pump_recurrence_integrity_report import (
    Episode,
    Regime,
    SourceIdentityObservation,
)
from schurfer_analytics.pump_recurrence_integrity_repository import (
    PumpRecurrenceIntegrityRepository,
)
from schurfer_analytics.serial_pump_regimes import HorizonOutcome, RecurrenceSummary
from schurfer_analytics.serial_pump_regimes_report import (
    PopulationSummary,
    RegimeRow,
    ResolvedIdentity,
    SerialPumpRegimesFilters,
    SerialPumpRegimesReport,
    VenueExpansionEntry,
    _available_identity_episode_ids,
    _identity_overlap,
    _median_resolved,
    _pick_ohlcv_identity,
    _resolve_regime_identities,
    _summarize_horizons,
    _venue_ready,
    compute_input_fingerprint,
    render_json,
    render_markdown,
    run,
)
from schurfer_analytics.token_universe_coverage import AsOfCoverage

_T0 = datetime(2026, 8, 1, tzinfo=UTC)


def _episode(
    event_id: int,
    base: str = "JIMOTHY",
    *,
    minutes_after_t0: float = 0,
    peak_pct: float = 30.0,
) -> Episode:
    first = _T0 + timedelta(minutes=minutes_after_t0)
    return Episode(
        event_id=event_id,
        base=base,
        episode=1,
        first_seen_at=first,
        last_seen_at=first,
        peak_pct=peak_pct,
        closed_at=first,
    )


_UNSET = object()


def _observation(
    event_id: int,
    exchange: str,
    *,
    base: str = "JIMOTHY",
    identity_key: object = _UNSET,
    unified_symbol: object = _UNSET,
    identity_conflict: bool = False,
) -> SourceIdentityObservation:
    return SourceIdentityObservation(
        event_id=event_id,
        base=base,
        exchange=exchange,
        identity_key=identity_key if identity_key is not _UNSET else f"{exchange}:swap:{base}USDT",  # type: ignore[arg-type]
        unified_symbol=unified_symbol if unified_symbol is not _UNSET else f"{base}/USDT:USDT",  # type: ignore[arg-type]
        base_asset=base,
        identity_conflict=identity_conflict,
    )


def _resolved_horizon(
    label: str = "15m",
    *,
    forward_return_pct: float = 10.0,
    btc_adjusted_return_pct: float = 8.0,
    mfe_pct: float = 12.0,
    mae_pct: float = -3.0,
) -> HorizonOutcome:
    return HorizonOutcome(
        horizon_label=label,
        resolved=True,
        unresolved_reason=None,
        forward_return_pct=forward_return_pct,
        btc_adjusted_return_pct=btc_adjusted_return_pct,
        mfe_pct=mfe_pct,
        mae_pct=mae_pct,
        time_to_peak_minutes=1.0,
        retrace_magnitude_pct=2.0,
    )


def _unresolved_horizon(
    label: str = "15m", *, reason: str = "missing_decision_candle"
) -> HorizonOutcome:
    return HorizonOutcome(
        horizon_label=label,
        resolved=False,
        unresolved_reason=reason,
        forward_return_pct=None,
        btc_adjusted_return_pct=None,
        mfe_pct=None,
        mae_pct=None,
        time_to_peak_minutes=None,
        retrace_magnitude_pct=None,
    )


def _recurrence() -> RecurrenceSummary:
    return RecurrenceSummary(
        base="JIMOTHY", regime_index=0, regime_count_so_far=1, next_regime_gap_minutes=None
    )


def _row(
    horizons: tuple[HorizonOutcome, ...],
    *,
    ohlcv_exchange: str | None = "binance",
    ohlcv_symbol: str | None = "JIMOTHY/USDT:USDT",
    regime_mature: bool = True,
    next_regime_same_asset: bool | None = None,
    venue_expansion: tuple[VenueExpansionEntry, ...] = (),
    venue_expansion_unresolved_reason: str | None = None,
    delisted: bool | None = None,
) -> RegimeRow:
    return RegimeRow(
        base="JIMOTHY",
        episode_ids=(1,),
        first_seen_at=_T0,
        last_seen_at=_T0,
        max_peak_pct=30.0,
        decision_at=_T0,
        regime_mature=regime_mature,
        ohlcv_exchange=ohlcv_exchange,
        ohlcv_symbol=ohlcv_symbol,
        ohlcv_unresolved_reason=None,
        horizons=horizons,
        recurrence=_recurrence(),
        next_regime_same_asset=next_regime_same_asset,
        venue_expansion=venue_expansion,
        venue_expansion_unresolved_reason=venue_expansion_unresolved_reason,
        delisted=delisted,
    )


# -- SerialPumpRegimesFilters ------------------------------------------------


def test_filters_rejects_since_not_before_until() -> None:
    with pytest.raises(ValueError, match="--since must be earlier than --until"):
        SerialPumpRegimesFilters(since=_T0, until=_T0)


def test_filters_rejects_since_after_until() -> None:
    with pytest.raises(ValueError, match="--since must be earlier than --until"):
        SerialPumpRegimesFilters(since=_T0 + timedelta(hours=1), until=_T0)


def test_filters_allows_open_bounds() -> None:
    SerialPumpRegimesFilters(since=None, until=None)
    SerialPumpRegimesFilters(since=_T0, until=None)
    SerialPumpRegimesFilters(since=None, until=_T0)


# -- VenueExpansionEntry.expanded --------------------------------------------


def test_venue_expansion_entry_expanded_true_on_not_ready_to_ready() -> None:
    entry = VenueExpansionEntry(
        exchange="bybit",
        ready_before=False,
        ready_after=True,
        after_at_matured=True,
        match_basis="identity_key",
    )
    assert entry.expanded is True


def test_venue_expansion_entry_expanded_false_when_already_ready() -> None:
    entry = VenueExpansionEntry(
        exchange="bybit",
        ready_before=True,
        ready_after=True,
        after_at_matured=True,
        match_basis="identity_key",
    )
    assert entry.expanded is False


def test_venue_expansion_entry_expanded_none_when_either_side_unknown() -> None:
    entry_no_before = VenueExpansionEntry(
        exchange="bybit",
        ready_before=None,
        ready_after=True,
        after_at_matured=True,
        match_basis="identity_key",
    )
    entry_no_after = VenueExpansionEntry(
        exchange="bybit",
        ready_before=True,
        ready_after=None,
        after_at_matured=True,
        match_basis="identity_key",
    )
    entry_neither = VenueExpansionEntry(
        exchange="bybit",
        ready_before=None,
        ready_after=None,
        after_at_matured=True,
        match_basis=None,
    )
    assert entry_no_before.expanded is None
    assert entry_no_after.expanded is None
    assert entry_neither.expanded is None


def test_venue_expansion_entry_expanded_none_when_after_at_not_matured() -> None:
    # ready_after is always None when the 30-day-forward point has not
    # actually occurred yet -- expanded must therefore also be None, never
    # guessed from ready_before alone.
    entry = VenueExpansionEntry(
        exchange="bybit",
        ready_before=False,
        ready_after=None,
        after_at_matured=False,
        match_basis="ticker_fallback",
    )
    assert entry.expanded is None


# -- _resolve_regime_identities -----------------------------------------------


def test_resolve_regime_identities_no_sources() -> None:
    assert _resolve_regime_identities((1,), {}) == {}


def test_resolve_regime_identities_single_consistent_observation() -> None:
    sources_by_event: dict[int, tuple[SourceIdentityObservation, ...]] = {
        1: (_observation(1, "binance", identity_key="k1", unified_symbol="X/USDT:USDT"),)
    }
    resolved = _resolve_regime_identities((1,), sources_by_event)
    assert resolved["binance"] == ResolvedIdentity(
        exchange="binance", identity_key="k1", unified_symbol="X/USDT:USDT"
    )


def test_resolve_regime_identities_agreeing_across_episodes_is_resolved() -> None:
    sources_by_event: dict[int, tuple[SourceIdentityObservation, ...]] = {
        1: (_observation(1, "binance", identity_key="k1", unified_symbol="X/USDT:USDT"),),
        2: (_observation(2, "binance", identity_key="k1", unified_symbol="X/USDT:USDT"),),
    }
    resolved = _resolve_regime_identities((1, 2), sources_by_event)
    assert resolved["binance"] is not None
    assert resolved["binance"].identity_key == "k1"


def test_resolve_regime_identities_conflicting_identity_key_is_ambiguous() -> None:
    # Two episodes of the SAME merged regime recorded different identity
    # keys on the same exchange (e.g. a relisting under the same ticker
    # within the cooldown window) -- must fail closed, never guess which
    # one is right.
    sources_by_event: dict[int, tuple[SourceIdentityObservation, ...]] = {
        1: (_observation(1, "binance", identity_key="k1", unified_symbol="X/USDT:USDT"),),
        2: (_observation(2, "binance", identity_key="k2", unified_symbol="X/USDT:USDT"),),
    }
    resolved = _resolve_regime_identities((1, 2), sources_by_event)
    assert resolved["binance"] is None


def test_resolve_regime_identities_identity_conflict_flag_is_ambiguous() -> None:
    sources_by_event: dict[int, tuple[SourceIdentityObservation, ...]] = {
        1: (_observation(1, "binance", identity_key="k1", identity_conflict=True),)
    }
    resolved = _resolve_regime_identities((1,), sources_by_event)
    assert resolved["binance"] is None


def test_resolve_regime_identities_missing_unified_symbol_is_ambiguous() -> None:
    sources_by_event: dict[int, tuple[SourceIdentityObservation, ...]] = {
        1: (_observation(1, "binance", unified_symbol=None),)
    }
    resolved = _resolve_regime_identities((1,), sources_by_event)
    assert resolved["binance"] is None


def test_resolve_regime_identities_unions_across_every_episode_in_the_regime() -> None:
    sources_by_event: dict[int, tuple[SourceIdentityObservation, ...]] = {
        1: (_observation(1, "bybit"),),
        2: (_observation(2, "binance"),),
    }
    resolved = _resolve_regime_identities((1, 2), sources_by_event)
    assert set(resolved.keys()) == {"bybit", "binance"}


def test_resolve_regime_identities_mixed_present_and_none_identity_key_is_ambiguous() -> None:
    # Regression guard (colleague review, 2026-09-01, round 2): a set
    # comprehension that filters out None before checking cardinality
    # would silently treat [identity_key='k1', identity_key=None] as a
    # single-element {"k1"} set -- i.e. "successfully resolved" -- when
    # the None entry is itself evidence of an incomplete observation that
    # should make the whole exchange ambiguous. Reusing identity_reason
    # (which flags the None-key observation as "missing_identity") closes
    # this before the set-building step is ever reached.
    sources_by_event: dict[int, tuple[SourceIdentityObservation, ...]] = {
        1: (_observation(1, "binance", identity_key="k1", unified_symbol="X/USDT:USDT"),),
        2: (_observation(2, "binance", identity_key=None, unified_symbol=None),),
    }
    resolved = _resolve_regime_identities((1, 2), sources_by_event)
    assert resolved["binance"] is None


def test_resolve_regime_identities_base_mismatch_is_ambiguous() -> None:
    # Regression guard (colleague review, 2026-09-01, round 2): the earlier
    # resolver never checked base_asset at all, even though
    # pump_recurrence_integrity_report.identity_reason already classifies
    # a base/base_asset mismatch as unresolved ("base_mismatch") --
    # exactly the alias-collision signal that check exists to catch.
    # Reusing identity_reason (rather than a second, narrower check) picks
    # this up automatically.
    sources_by_event: dict[int, tuple[SourceIdentityObservation, ...]] = {
        1: (
            SourceIdentityObservation(
                event_id=1,
                base="JIMOTHY",
                exchange="binance",
                identity_key="k1",
                unified_symbol="JIMOTHY/USDT:USDT",
                base_asset="SOMETHING_ELSE",
                identity_conflict=False,
            ),
        )
    }
    resolved = _resolve_regime_identities((1,), sources_by_event)
    assert resolved["binance"] is None


# -- _pick_ohlcv_identity ------------------------------------------------------


def test_pick_ohlcv_identity_no_sources_at_all() -> None:
    identity, reason = _pick_ohlcv_identity({})
    assert identity is None
    assert reason == "no_identity_observation"


def test_pick_ohlcv_identity_picks_highest_priority_among_recorded_sources() -> None:
    # bybit is inserted first but binance outranks it in
    # EXCHANGE_OHLCV_PRIORITY -- priority order wins, not insertion order.
    regime_identities: dict[str, ResolvedIdentity | None] = {
        "bybit": ResolvedIdentity("bybit", "k-bybit", "X/USDT:USDT"),
        "binance": ResolvedIdentity("binance", "k-binance", "X/USDT:USDT"),
    }
    identity, reason = _pick_ohlcv_identity(regime_identities)
    assert identity is not None
    assert identity.exchange == "binance"
    assert reason is None


def test_pick_ohlcv_identity_unsupported_when_exchange_not_in_priority_or_factories() -> None:
    regime_identities: dict[str, ResolvedIdentity | None] = {
        "some_unlisted_exchange": ResolvedIdentity("some_unlisted_exchange", "k", "X")
    }
    identity, reason = _pick_ohlcv_identity(regime_identities)
    assert identity is None
    assert reason == "unsupported_exchange"


def test_pick_ohlcv_identity_ambiguous_when_supported_exchange_has_no_resolved_identity() -> None:
    # binance IS in EXCHANGE_OHLCV_PRIORITY/EXCHANGE_FACTORIES, but its own
    # identity for this regime was ambiguous (None) -- must be reported as
    # ambiguous_identity, distinct from "nothing here was ever supported".
    regime_identities: dict[str, ResolvedIdentity | None] = {"binance": None}
    identity, reason = _pick_ohlcv_identity(regime_identities)
    assert identity is None
    assert reason == "ambiguous_identity"


def test_pick_ohlcv_identity_falls_through_ambiguous_to_a_lower_priority_good_one() -> None:
    regime_identities: dict[str, ResolvedIdentity | None] = {
        "binance": None,  # ambiguous
        "bybit": ResolvedIdentity("bybit", "k-bybit", "X/USDT:USDT"),
    }
    identity, reason = _pick_ohlcv_identity(regime_identities)
    assert identity is not None
    assert identity.exchange == "bybit"
    assert reason is None


# -- _available_identity_episode_ids -----------------------------------------


def _regime(
    base: str = "JIMOTHY", *, episode_ids: tuple[int, ...] = (1,), first_seen_at: datetime = _T0
) -> Regime:
    return Regime(
        base=base,
        episode_ids=episode_ids,
        first_seen_at=first_seen_at,
        last_seen_at=first_seen_at,
        max_peak_pct=30.0,
    )


def test_available_identity_episode_ids_excludes_episode_after_boundary() -> None:
    # Regression guard (colleague review, 2026-09-01, round 2): the core
    # "future-known route selection" fix. Episode 2 starts 2 hours after
    # episode 1 -- well after the decision boundary (5 minutes past
    # episode 1's own first_seen_at) -- so it must not count as "known" at
    # decision time even though merge_episodes_into_regimes still merges
    # it into the same regime (within the 24h cooldown).
    regime = _regime(episode_ids=(1, 2), first_seen_at=_T0)
    boundary_ms = int((_T0 + timedelta(minutes=5)).timestamp() * 1000)
    episode_by_id = {
        1: _episode(1, minutes_after_t0=0),
        2: _episode(2, minutes_after_t0=120),
    }
    available = _available_identity_episode_ids(regime, boundary_ms, episode_by_id)
    assert available == (1,)


def test_available_identity_episode_ids_includes_episode_at_boundary() -> None:
    regime = _regime(episode_ids=(1, 2), first_seen_at=_T0)
    boundary_ms = int((_T0 + timedelta(minutes=5)).timestamp() * 1000)
    episode_by_id = {
        1: _episode(1, minutes_after_t0=0),
        2: _episode(2, minutes_after_t0=5),  # exactly at the boundary
    }
    available = _available_identity_episode_ids(regime, boundary_ms, episode_by_id)
    assert available == (1, 2)


# -- _identity_overlap ---------------------------------------------------------


def test_identity_overlap_true_when_shared_exchange_has_matching_identity_key() -> None:
    a: dict[str, ResolvedIdentity | None] = {
        "binance": ResolvedIdentity("binance", "k1", "X/USDT:USDT")
    }
    b: dict[str, ResolvedIdentity | None] = {
        "binance": ResolvedIdentity("binance", "k1", "X/USDT:USDT")
    }
    assert _identity_overlap(a, b) is True


def test_identity_overlap_false_when_shared_exchange_has_different_identity_key() -> None:
    # The exact relisted-ticker-collision case this exists to catch.
    a: dict[str, ResolvedIdentity | None] = {
        "binance": ResolvedIdentity("binance", "k1", "X/USDT:USDT")
    }
    b: dict[str, ResolvedIdentity | None] = {
        "binance": ResolvedIdentity("binance", "k2", "X/USDT:USDT")
    }
    assert _identity_overlap(a, b) is False


def test_identity_overlap_none_when_no_comparable_exchange() -> None:
    a: dict[str, ResolvedIdentity | None] = {
        "binance": ResolvedIdentity("binance", "k1", "X/USDT:USDT")
    }
    b: dict[str, ResolvedIdentity | None] = {
        "bybit": ResolvedIdentity("bybit", "k1", "X/USDT:USDT")
    }
    assert _identity_overlap(a, b) is None


def test_identity_overlap_none_when_shared_exchange_but_one_side_ambiguous() -> None:
    a: dict[str, ResolvedIdentity | None] = {
        "binance": ResolvedIdentity("binance", "k1", "X/USDT:USDT")
    }
    b: dict[str, ResolvedIdentity | None] = {"binance": None}
    assert _identity_overlap(a, b) is None


def test_identity_overlap_none_for_empty_dicts() -> None:
    assert _identity_overlap({}, {}) is None


# -- _venue_ready ---------------------------------------------------------------


def _coverage(
    *, native_market_ids: frozenset[str] = frozenset(), identity_keys: frozenset[str] = frozenset()
) -> AsOfCoverage:
    return AsOfCoverage(
        exchange="binance",
        as_of=_T0,
        snapshot_captured_at=_T0,
        native_market_ids=native_market_ids,
        identity_keys=identity_keys,
    )


def test_venue_ready_prefers_identity_key_when_available() -> None:
    identity = ResolvedIdentity("binance", "k1", "JIMOTHY/USDT:USDT")
    coverage = _coverage(identity_keys=frozenset({"k1"}))
    ready, basis = _venue_ready(identity, "JIMOTHY", coverage)
    assert ready is True
    assert basis == "identity_key"


def test_venue_ready_falls_back_to_ticker_when_no_source_identity() -> None:
    # Regression guard (colleague review, 2026-09-01, round 2): the exact
    # "genuine venue expansion" case -- no source identity exists for this
    # exchange (this regime's own pump was never detected there), which is
    # precisely what "did it newly appear here" needs to detect. Requiring
    # identity_key alone would structurally exclude this case entirely.
    coverage = _coverage(native_market_ids=frozenset({"JIMOTHYUSDT"}))
    ready, basis = _venue_ready(None, "JIMOTHY", coverage)
    assert ready is True
    assert basis == "ticker_fallback"


def test_venue_ready_none_when_snapshot_not_usable() -> None:
    stale_coverage = AsOfCoverage(
        exchange="binance",
        as_of=_T0,
        snapshot_captured_at=None,
        native_market_ids=frozenset(),
        identity_keys=frozenset(),
    )
    ready, basis = _venue_ready(None, "JIMOTHY", stale_coverage)
    assert ready is None
    assert basis is None


# -- compute_input_fingerprint -------------------------------------------------


def test_compute_input_fingerprint_deterministic_regardless_of_input_order() -> None:
    a = _episode(1, minutes_after_t0=0)
    b = _episode(2, minutes_after_t0=5)
    obs_a = _observation(1, "binance")
    obs_b = _observation(2, "bybit")
    assert compute_input_fingerprint((a, b), (obs_a, obs_b)) == compute_input_fingerprint(
        (b, a), (obs_b, obs_a)
    )


def test_compute_input_fingerprint_changes_with_episode_content() -> None:
    a = _episode(1, peak_pct=30.0)
    b = _episode(1, peak_pct=31.0)
    assert compute_input_fingerprint((a,), ()) != compute_input_fingerprint((b,), ())


def test_compute_input_fingerprint_changes_with_identity_observation_content() -> None:
    # Regression guard (colleague review, 2026-09-01): the fingerprint must
    # also reflect the identity observations that determine exchange/
    # symbol choice -- an unchanged episode set with a changed identity_key
    # must not silently keep the same fingerprint.
    episodes = (_episode(1),)
    obs_a = _observation(1, "binance", identity_key="k1")
    obs_b = _observation(1, "binance", identity_key="k2")
    assert compute_input_fingerprint(episodes, (obs_a,)) != compute_input_fingerprint(
        episodes, (obs_b,)
    )


def test_compute_input_fingerprint_handles_none_closed_at() -> None:
    open_episode = Episode(
        event_id=1,
        base="JIMOTHY",
        episode=1,
        first_seen_at=_T0,
        last_seen_at=_T0,
        peak_pct=30.0,
        closed_at=None,
    )
    closed_episode = Episode(
        event_id=1,
        base="JIMOTHY",
        episode=1,
        first_seen_at=_T0,
        last_seen_at=_T0,
        peak_pct=30.0,
        closed_at=_T0,
    )
    fingerprint_open = compute_input_fingerprint((open_episode,), ())
    fingerprint_closed = compute_input_fingerprint((closed_episode,), ())
    assert fingerprint_open != fingerprint_closed


# -- _median_resolved -----------------------------------------------------------


def test_median_resolved_empty_list_is_none() -> None:
    assert _median_resolved([]) is None


def test_median_resolved_computes_median() -> None:
    assert _median_resolved([1.0, 2.0, 3.0]) == 2.0


def test_median_resolved_asserts_on_none() -> None:
    with pytest.raises(AssertionError):
        _median_resolved([1.0, None])


# -- _summarize_horizons --------------------------------------------------------


def test_summarize_horizons_counts_resolved_and_unresolved_separately() -> None:
    rows = (
        _row((_resolved_horizon("15m", forward_return_pct=10.0),)),
        _row((_unresolved_horizon("15m", reason="missing_decision_candle"),)),
        _row((_unresolved_horizon("15m", reason="missing_decision_candle"),)),
    )
    summaries = _summarize_horizons(rows)
    assert len(summaries) == 6
    summary_15m = next(s for s in summaries if s.horizon_label == "15m")
    assert summary_15m.resolved_count == 1
    assert summary_15m.unresolved_counts == {"missing_decision_candle": 2}
    assert summary_15m.median_forward_return_pct == pytest.approx(10.0)


def test_summarize_horizons_median_over_multiple_resolved_rows() -> None:
    rows = (
        _row((_resolved_horizon("15m", forward_return_pct=10.0),)),
        _row((_resolved_horizon("15m", forward_return_pct=20.0),)),
        _row((_resolved_horizon("15m", forward_return_pct=30.0),)),
    )
    summaries = _summarize_horizons(rows)
    summary_15m = next(s for s in summaries if s.horizon_label == "15m")
    assert summary_15m.median_forward_return_pct == pytest.approx(20.0)


def test_summarize_horizons_all_unresolved_medians_are_none() -> None:
    rows = (_row((_unresolved_horizon("15m"),)),)
    summaries = _summarize_horizons(rows)
    summary_15m = next(s for s in summaries if s.horizon_label == "15m")
    assert summary_15m.resolved_count == 0
    assert summary_15m.median_forward_return_pct is None
    assert summary_15m.median_btc_adjusted_return_pct is None
    assert summary_15m.median_mfe_pct is None
    assert summary_15m.median_mae_pct is None


# -- render_json / render_markdown -------------------------------------------


def _report(rows: tuple[RegimeRow, ...] = ()) -> SerialPumpRegimesReport:
    return SerialPumpRegimesReport(
        report_version="serial_pump_regimes_v1",
        generated_at=_T0,
        code_revision="deadbeef",
        working_tree_dirty=False,
        input_fingerprint="abc123",
        filters=SerialPumpRegimesFilters(since=None, until=None, bases=()),
        population=PopulationSummary(
            total_bases=1,
            total_regimes=len(rows),
            regimes_with_no_ohlcv_source=0,
            horizons=_summarize_horizons(rows),
        ),
        regimes=rows,
    )


def test_render_json_produces_real_nested_json_not_a_repr_string() -> None:
    # Regression guard: render_json once called json_ready(report) directly
    # without asdict() first -- json_ready only recurses into dict/list/
    # tuple, so a bare dataclass instance passed straight through
    # unchanged, and json.dumps(..., default=str) then stringified the
    # WHOLE report via repr() into a single JSON string literal instead of
    # real nested JSON. Caught by a live smoke run, fixed by adding
    # asdict() -- this test parses the output back and asserts it is an
    # actual object with real fields, not one giant string.
    report = _report((_row((_resolved_horizon("15m", forward_return_pct=10.0),)),))
    rendered = render_json(report)
    parsed = json.loads(rendered)
    assert isinstance(parsed, dict)
    assert parsed["report_version"] == "serial_pump_regimes_v1"
    assert parsed["code_revision"] == "deadbeef"
    assert isinstance(parsed["regimes"], list)
    assert parsed["regimes"][0]["base"] == "JIMOTHY"
    assert parsed["regimes"][0]["horizons"][0]["forward_return_pct"] == pytest.approx(10.0)


def test_render_json_serializes_datetimes_as_isoformat_strings() -> None:
    report = _report((_row((_resolved_horizon("15m"),)),))
    parsed = json.loads(render_json(report))
    assert parsed["generated_at"] == _T0.isoformat()
    assert parsed["regimes"][0]["decision_at"] == _T0.isoformat()


def test_render_markdown_includes_population_summary_and_horizon_table() -> None:
    report = _report((_row((_resolved_horizon("15m", forward_return_pct=10.0),)),))
    rendered = render_markdown(report)
    assert "serial_pump_regimes_v1" in rendered
    assert "Bases: 1" in rendered
    assert "15m" in rendered
    assert "10.00%" in rendered


def test_render_markdown_includes_gross_returns_disclaimer() -> None:
    # Regression guard (colleague review, 2026-09-01, round 2): the report
    # READER (not just source comments) must see the gross-returns/no-
    # verdict disclaimer -- it previously lived only in the module
    # docstring, invisible to anyone reading just the rendered output.
    report = _report((_row((_resolved_horizon("15m"),)),))
    rendered = render_markdown(report)
    assert "GROSS returns" in rendered


def test_render_markdown_includes_regimes_and_horizon_detail_sections() -> None:
    # Regression guard (colleague review, 2026-09-01, round 2): the
    # default markdown format used to surface only per-horizon population
    # medians -- omitting unresolved reason counts, recurrence, inter-
    # regime gaps, venue expansion, time-to-peak, retrace, and delisting,
    # almost everything else this report actually computes.
    row = _row(
        (
            _resolved_horizon("15m", forward_return_pct=10.0),
            _unresolved_horizon("1h", reason="insufficient_candle_history"),
        ),
        next_regime_same_asset=True,
        venue_expansion=(
            VenueExpansionEntry(
                exchange="bybit",
                ready_before=False,
                ready_after=True,
                after_at_matured=True,
                match_basis="ticker_fallback",
            ),
        ),
        delisted=False,
    )
    report = _report((row,))
    rendered = render_markdown(report)
    assert "## Regimes" in rendered
    assert "## Horizon detail" in rendered
    assert "insufficient_candle_history" in rendered  # unresolved reason count, per-horizon
    assert "ticker_fallback" in rendered  # venue expansion match basis
    assert "time-to-peak" in rendered
    assert "retrace magnitude" in rendered


# -- run() orchestration (fakes, no real DB/network) --------------------------


class _FakeRecurrenceRepository:
    def __init__(
        self, episodes: tuple[Episode, ...], observations: tuple[SourceIdentityObservation, ...]
    ):
        self._episodes = episodes
        self._observations = observations
        self.closed = False

    async def load(
        self, filters: object
    ) -> tuple[tuple[Episode, ...], tuple[SourceIdentityObservation, ...]]:
        return self._episodes, self._observations

    async def close(self) -> None:
        self.closed = True


class _FakeUniverseRepository:
    def __init__(
        self,
        *,
        coverage: AsOfCoverage | None = None,
        raise_on_instruments_as_of: Exception | None = None,
    ) -> None:
        self.closed = False
        self._coverage = coverage
        self._raise = raise_on_instruments_as_of
        self.calls: list[tuple[str, datetime]] = []

    async def instruments_as_of(self, exchange: str, as_of: datetime) -> AsOfCoverage:
        self.calls.append((exchange, as_of))
        if self._raise is not None:
            raise self._raise
        if self._coverage is not None:
            return self._coverage
        return AsOfCoverage(
            exchange=exchange,
            as_of=as_of,
            snapshot_captured_at=None,
            native_market_ids=frozenset(),
            identity_keys=frozenset(),
        )

    async def close(self) -> None:
        self.closed = True


class _FakeExchangeClient:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def _flat_candles(start_ms: int, count: int) -> list[Candle]:
    return [
        Candle(
            ts_ms=start_ms + i * TIMEFRAME_MS, open=1.0, high=1.0, low=1.0, close=1.0, volume=1.0
        )
        for i in range(count)
    ]


async def test_run_rejects_nonpositive_concurrency() -> None:
    with pytest.raises(ValueError, match="concurrency must be >= 1"):
        await run(
            database_url="postgresql://fake",
            filters=SerialPumpRegimesFilters(),
            code_revision="test",
            working_tree_dirty=False,
            concurrency=0,
        )


async def test_run_isolates_one_regimes_ohlcv_failure_from_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Regression guard (colleague review, 2026-09-01): one regime's own
    # fetch failure must not propagate through asyncio.gather and lose
    # every other already-completed regime in the same run.
    episodes = (_episode(1, base="OK"), _episode(2, base="FAIL"))
    observations = (
        _observation(1, "binance", base="OK", unified_symbol="OK/USDT:USDT"),
        _observation(2, "binance", base="FAIL", unified_symbol="FAIL/USDT:USDT"),
    )
    fake_recurrence_repo = _FakeRecurrenceRepository(episodes, observations)
    fake_universe_repo = _FakeUniverseRepository()
    fake_client = _FakeExchangeClient()

    monkeypatch.setattr(
        PumpRecurrenceIntegrityRepository,
        "from_url",
        classmethod(lambda cls, url: fake_recurrence_repo),
    )
    monkeypatch.setattr(
        MomentumUniverseIdentityRepository,
        "from_url",
        classmethod(lambda cls, url: fake_universe_repo),
    )
    monkeypatch.setitem(EXCHANGE_FACTORIES, "binance", lambda: fake_client)

    async def _fake_fetch_symbol_candles(
        exchange: object, symbol: str, start_ms: int, end_ms: int, **kwargs: object
    ) -> list[Candle]:
        if symbol == "FAIL/USDT:USDT":
            raise RuntimeError("simulated exchange failure")
        return _flat_candles(start_ms, 15)

    monkeypatch.setattr(report_module, "fetch_symbol_candles", _fake_fetch_symbol_candles)

    report = await run(
        database_url="postgresql://fake",
        filters=SerialPumpRegimesFilters(),
        code_revision="test",
        working_tree_dirty=False,
        concurrency=2,
        compute_venue_expansion=False,
        evaluation_at=_T0 + timedelta(days=40),
    )

    assert report.population.total_regimes == 2
    rows_by_base = {row.base: row for row in report.regimes}

    ok_row = rows_by_base["OK"]
    assert ok_row.ohlcv_unresolved_reason is None
    assert ok_row.ohlcv_exchange == "binance"
    ok_15m = next(h for h in ok_row.horizons if h.horizon_label == "15m")
    assert ok_15m.resolved is True

    fail_row = rows_by_base["FAIL"]
    assert fail_row.ohlcv_unresolved_reason == "ohlcv_fetch_failed"
    assert all(
        not h.resolved and h.unresolved_reason == "ohlcv_fetch_failed" for h in fail_row.horizons
    )

    # Cleanup still ran for everything, even though one task failed.
    assert fake_recurrence_repo.closed is True
    assert fake_universe_repo.closed is True
    assert fake_client.closed is True


async def test_run_uses_canonical_unified_symbol_not_a_reconstructed_ticker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Regression guard (colleague review, 2026-09-01): OHLCV must be
    # fetched via the exact recorded unified_symbol, never a ticker
    # rebuilt from the bare base -- a base whose own base != the leading
    # part of its unified_symbol (a stand-in for a relisted/collided
    # ticker) proves the fetch used the recorded symbol, not base.upper().
    episodes = (_episode(1, base="WRONG"),)
    observations = (_observation(1, "binance", base="WRONG", unified_symbol="RIGHT/USDT:USDT"),)
    fake_recurrence_repo = _FakeRecurrenceRepository(episodes, observations)
    fake_universe_repo = _FakeUniverseRepository()
    fake_client = _FakeExchangeClient()
    requested_symbols: list[str] = []

    monkeypatch.setattr(
        PumpRecurrenceIntegrityRepository,
        "from_url",
        classmethod(lambda cls, url: fake_recurrence_repo),
    )
    monkeypatch.setattr(
        MomentumUniverseIdentityRepository,
        "from_url",
        classmethod(lambda cls, url: fake_universe_repo),
    )
    monkeypatch.setitem(EXCHANGE_FACTORIES, "binance", lambda: fake_client)

    async def _fake_fetch_symbol_candles(
        exchange: object, symbol: str, start_ms: int, end_ms: int, **kwargs: object
    ) -> list[Candle]:
        requested_symbols.append(symbol)
        return _flat_candles(start_ms, 15)

    monkeypatch.setattr(report_module, "fetch_symbol_candles", _fake_fetch_symbol_candles)

    report = await run(
        database_url="postgresql://fake",
        filters=SerialPumpRegimesFilters(),
        code_revision="test",
        working_tree_dirty=False,
        concurrency=2,
        compute_venue_expansion=False,
        evaluation_at=_T0 + timedelta(days=40),
    )

    assert report.regimes[0].ohlcv_symbol == "RIGHT/USDT:USDT"
    assert "RIGHT/USDT:USDT" in requested_symbols
    assert "WRONG/USDT:USDT" not in requested_symbols


async def test_run_lets_cache_integrity_errors_propagate(monkeypatch: pytest.MonkeyPatch) -> None:
    # Regression guard (colleague review, 2026-09-01, round 2):
    # MarketPathCacheCorruptError/MarketPathCacheWriteError are NOT
    # ordinary per-regime fetch noise -- market_path_cache.py's own
    # docstrings require both to fail loudly (a systemic infra problem,
    # not something a single regime's "ohlcv_fetch_failed" label should
    # quietly absorb). Must propagate all the way out of run(), not be
    # caught by the generic per-regime except.
    episodes = (_episode(1, base="OK"),)
    observations = (_observation(1, "binance", base="OK", unified_symbol="OK/USDT:USDT"),)
    fake_recurrence_repo = _FakeRecurrenceRepository(episodes, observations)
    fake_universe_repo = _FakeUniverseRepository()
    fake_client = _FakeExchangeClient()

    monkeypatch.setattr(
        PumpRecurrenceIntegrityRepository,
        "from_url",
        classmethod(lambda cls, url: fake_recurrence_repo),
    )
    monkeypatch.setattr(
        MomentumUniverseIdentityRepository,
        "from_url",
        classmethod(lambda cls, url: fake_universe_repo),
    )
    monkeypatch.setitem(EXCHANGE_FACTORIES, "binance", lambda: fake_client)

    async def _fake_fetch_symbol_candles(
        exchange: object, symbol: str, start_ms: int, end_ms: int, **kwargs: object
    ) -> list[Candle]:
        raise MarketPathCacheCorruptError("simulated corrupt cache entry")

    monkeypatch.setattr(report_module, "fetch_symbol_candles", _fake_fetch_symbol_candles)

    with pytest.raises(MarketPathCacheCorruptError):
        await run(
            database_url="postgresql://fake",
            filters=SerialPumpRegimesFilters(),
            code_revision="test",
            working_tree_dirty=False,
            concurrency=2,
            compute_venue_expansion=False,
            evaluation_at=_T0 + timedelta(days=40),
        )

    # Cleanup still ran even though the exception propagated.
    assert fake_client.closed is True
    assert fake_universe_repo.closed is True


async def test_run_isolates_venue_expansion_failure_from_already_computed_horizons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Regression guard (colleague review, 2026-09-01, round 2): a
    # venue_expansion-only failure (unrelated to the OHLCV fetch, which
    # already succeeded) must not discard the horizon outcomes just
    # computed, and must not be mislabeled as ohlcv_fetch_failed.
    episodes = (_episode(1, base="OK"),)
    observations = (_observation(1, "binance", base="OK", unified_symbol="OK/USDT:USDT"),)
    fake_recurrence_repo = _FakeRecurrenceRepository(episodes, observations)
    fake_universe_repo = _FakeUniverseRepository(
        raise_on_instruments_as_of=RuntimeError("simulated DB hiccup")
    )
    fake_client = _FakeExchangeClient()

    monkeypatch.setattr(
        PumpRecurrenceIntegrityRepository,
        "from_url",
        classmethod(lambda cls, url: fake_recurrence_repo),
    )
    monkeypatch.setattr(
        MomentumUniverseIdentityRepository,
        "from_url",
        classmethod(lambda cls, url: fake_universe_repo),
    )
    monkeypatch.setitem(EXCHANGE_FACTORIES, "binance", lambda: fake_client)

    async def _fake_fetch_symbol_candles(
        exchange: object, symbol: str, start_ms: int, end_ms: int, **kwargs: object
    ) -> list[Candle]:
        return _flat_candles(start_ms, 15)

    monkeypatch.setattr(report_module, "fetch_symbol_candles", _fake_fetch_symbol_candles)

    report = await run(
        database_url="postgresql://fake",
        filters=SerialPumpRegimesFilters(),
        code_revision="test",
        working_tree_dirty=False,
        concurrency=2,
        compute_venue_expansion=True,
        evaluation_at=_T0 + timedelta(days=40),
    )

    row = report.regimes[0]
    assert row.ohlcv_unresolved_reason is None
    ok_15m = next(h for h in row.horizons if h.horizon_label == "15m")
    assert ok_15m.resolved is True
    assert row.venue_expansion_unresolved_reason == "venue_expansion_failed"
    assert row.venue_expansion == ()
    assert row.delisted is None


async def test_run_fingerprint_respects_base_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    # Regression guard (colleague review, 2026-09-01, round 2):
    # identity_observations used to stay unfiltered even after episodes
    # was narrowed by --base, so a bounded run's own fingerprint kept
    # changing whenever an UNRELATED base's identity observations changed.
    episodes = (_episode(1, base="KEEP"), _episode(2, base="DROP"))
    observations = (
        _observation(1, "binance", base="KEEP", unified_symbol="KEEP/USDT:USDT"),
        _observation(2, "binance", base="DROP", unified_symbol="DROP/USDT:USDT"),
    )
    fake_recurrence_repo = _FakeRecurrenceRepository(episodes, observations)
    fake_universe_repo = _FakeUniverseRepository()
    fake_client = _FakeExchangeClient()

    monkeypatch.setattr(
        PumpRecurrenceIntegrityRepository,
        "from_url",
        classmethod(lambda cls, url: fake_recurrence_repo),
    )
    monkeypatch.setattr(
        MomentumUniverseIdentityRepository,
        "from_url",
        classmethod(lambda cls, url: fake_universe_repo),
    )
    monkeypatch.setitem(EXCHANGE_FACTORIES, "binance", lambda: fake_client)

    async def _fake_fetch_symbol_candles(
        exchange: object, symbol: str, start_ms: int, end_ms: int, **kwargs: object
    ) -> list[Candle]:
        return _flat_candles(start_ms, 15)

    monkeypatch.setattr(report_module, "fetch_symbol_candles", _fake_fetch_symbol_candles)

    report = await run(
        database_url="postgresql://fake",
        filters=SerialPumpRegimesFilters(bases=("KEEP",)),
        code_revision="test",
        working_tree_dirty=False,
        concurrency=2,
        compute_venue_expansion=False,
        evaluation_at=_T0 + timedelta(days=40),
    )

    expected_fingerprint = compute_input_fingerprint((episodes[0],), (observations[0],))
    assert report.input_fingerprint == expected_fingerprint


async def test_run_does_not_use_future_episode_identity_for_ohlcv_pick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Regression guard (colleague review, 2026-09-01, round 2): the core
    # "future-known route selection" fix, exercised through the full
    # run() orchestration. Episode 1 (no identity) and episode 2 (identity
    # on binance, 2 hours later -- well after episode 1's own decision
    # boundary) merge into ONE regime. The regime's own OHLCV pick must
    # NOT use episode 2's identity, since it was not yet known at decision
    # time.
    episodes = (
        _episode(1, base="FUTR", minutes_after_t0=0),
        _episode(2, base="FUTR", minutes_after_t0=120),
    )
    observations = (_observation(2, "binance", base="FUTR", unified_symbol="FUTR/USDT:USDT"),)
    fake_recurrence_repo = _FakeRecurrenceRepository(episodes, observations)
    fake_universe_repo = _FakeUniverseRepository()
    fake_client = _FakeExchangeClient()

    monkeypatch.setattr(
        PumpRecurrenceIntegrityRepository,
        "from_url",
        classmethod(lambda cls, url: fake_recurrence_repo),
    )
    monkeypatch.setattr(
        MomentumUniverseIdentityRepository,
        "from_url",
        classmethod(lambda cls, url: fake_universe_repo),
    )
    monkeypatch.setitem(EXCHANGE_FACTORIES, "binance", lambda: fake_client)

    async def _fake_fetch_symbol_candles(
        exchange: object, symbol: str, start_ms: int, end_ms: int, **kwargs: object
    ) -> list[Candle]:
        return _flat_candles(start_ms, 15)

    monkeypatch.setattr(report_module, "fetch_symbol_candles", _fake_fetch_symbol_candles)

    report = await run(
        database_url="postgresql://fake",
        filters=SerialPumpRegimesFilters(),
        code_revision="test",
        working_tree_dirty=False,
        concurrency=2,
        compute_venue_expansion=False,
        evaluation_at=_T0 + timedelta(days=40),
    )

    assert len(report.regimes) == 1
    row = report.regimes[0]
    assert row.episode_ids == (1, 2)  # both episodes still belong to the regime
    assert row.ohlcv_exchange is None
    assert row.ohlcv_unresolved_reason == "no_identity_observation"


async def _run_two_regimes_for_recurrence(
    monkeypatch: pytest.MonkeyPatch, *, second_identity_key: str
) -> SerialPumpRegimesReport:
    episodes = (
        _episode(1, base="REC", minutes_after_t0=0),
        _episode(2, base="REC", minutes_after_t0=48 * 60),  # 48h later -- separate regime
    )
    observations = (
        _observation(1, "binance", base="REC", identity_key="k1", unified_symbol="REC/USDT:USDT"),
        _observation(
            2,
            "binance",
            base="REC",
            identity_key=second_identity_key,
            unified_symbol="REC/USDT:USDT",
        ),
    )
    fake_recurrence_repo = _FakeRecurrenceRepository(episodes, observations)
    fake_universe_repo = _FakeUniverseRepository()
    fake_client = _FakeExchangeClient()

    monkeypatch.setattr(
        PumpRecurrenceIntegrityRepository,
        "from_url",
        classmethod(lambda cls, url: fake_recurrence_repo),
    )
    monkeypatch.setattr(
        MomentumUniverseIdentityRepository,
        "from_url",
        classmethod(lambda cls, url: fake_universe_repo),
    )
    monkeypatch.setitem(EXCHANGE_FACTORIES, "binance", lambda: fake_client)

    async def _fake_fetch_symbol_candles(
        exchange: object, symbol: str, start_ms: int, end_ms: int, **kwargs: object
    ) -> list[Candle]:
        return _flat_candles(start_ms, 15)

    monkeypatch.setattr(report_module, "fetch_symbol_candles", _fake_fetch_symbol_candles)

    return await run(
        database_url="postgresql://fake",
        filters=SerialPumpRegimesFilters(),
        code_revision="test",
        working_tree_dirty=False,
        concurrency=2,
        compute_venue_expansion=False,
        evaluation_at=_T0 + timedelta(days=90),
    )


async def test_run_marks_next_regime_same_asset_true_when_identity_confirmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = await _run_two_regimes_for_recurrence(monkeypatch, second_identity_key="k1")
    assert len(report.regimes) == 2
    first_row = min(report.regimes, key=lambda r: r.first_seen_at)
    assert first_row.next_regime_same_asset is True


async def test_run_marks_next_regime_same_asset_false_on_identity_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The exact relisted-ticker-collision case: two "regimes" of the same
    # bare base, but genuinely different identity_key on their shared
    # exchange -- must be reported as NOT the same asset, not silently
    # counted as one asset's own recurrence.
    report = await _run_two_regimes_for_recurrence(monkeypatch, second_identity_key="k2")
    assert len(report.regimes) == 2
    first_row = min(report.regimes, key=lambda r: r.first_seen_at)
    assert first_row.next_regime_same_asset is False


async def test_run_computes_delisted(monkeypatch: pytest.MonkeyPatch) -> None:
    episodes = (_episode(1, base="GONE"),)
    observations = (_observation(1, "binance", base="GONE", identity_key="k1"),)
    fake_recurrence_repo = _FakeRecurrenceRepository(episodes, observations)
    # The identity_key this regime resolved to ("k1") is absent from the
    # coverage returned for evaluation_at -- delisted.
    fake_universe_repo = _FakeUniverseRepository(
        coverage=AsOfCoverage(
            exchange="binance",
            as_of=_T0,
            snapshot_captured_at=_T0,
            native_market_ids=frozenset(),
            identity_keys=frozenset({"some-other-key"}),
        )
    )
    fake_client = _FakeExchangeClient()

    monkeypatch.setattr(
        PumpRecurrenceIntegrityRepository,
        "from_url",
        classmethod(lambda cls, url: fake_recurrence_repo),
    )
    monkeypatch.setattr(
        MomentumUniverseIdentityRepository,
        "from_url",
        classmethod(lambda cls, url: fake_universe_repo),
    )
    monkeypatch.setitem(EXCHANGE_FACTORIES, "binance", lambda: fake_client)

    async def _fake_fetch_symbol_candles(
        exchange: object, symbol: str, start_ms: int, end_ms: int, **kwargs: object
    ) -> list[Candle]:
        return _flat_candles(start_ms, 15)

    monkeypatch.setattr(report_module, "fetch_symbol_candles", _fake_fetch_symbol_candles)

    report = await run(
        database_url="postgresql://fake",
        filters=SerialPumpRegimesFilters(),
        code_revision="test",
        working_tree_dirty=False,
        concurrency=2,
        compute_venue_expansion=True,
        evaluation_at=_T0 + timedelta(days=40),
    )

    assert report.regimes[0].delisted is True
