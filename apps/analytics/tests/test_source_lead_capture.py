import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

from schurfer_analytics.source_lead_capture import (
    ClaimedCapture,
    SourceLeadCandidate,
    SourceLeadCaptureWorker,
    SourceObservation,
    TargetObservation,
    build_source_lead_candidates,
    capture_new_source_leads,
    capture_target_observation,
    prepare_source_lead_captures,
    summarize_order_book,
)
from schurfer_analytics.source_lead_contract import CAPTURE_VERSION, IDENTITY_REGISTRY_V3_START
from schurfer_analytics.source_lead_qualification import (
    IDENTITY_MATCH_METHOD_BASE_SYMBOL_V1,
    IDENTITY_MATCH_METHOD_REGISTRY_EXACT_V2,
    IDENTITY_MATCH_METHOD_REGISTRY_LOOKUP_V2,
    IdentityRegistry,
    QualificationResult,
    parse_identity_registry,
)

_EVIDENCE_URL = "https://example.test/evidence"
_EVIDENCE_SHA256 = "a" * 64


def _abc_registry(*, target_exchange: str = "binance") -> IdentityRegistry:
    """Registers _candidate()'s source ("gate:swap:ABC_USDT:1") against
    _Exchange()'s target market (id "ABCUSDT", default onboarded_at_ms
    1_700_000_000_000 -> identity_key "{target_exchange}:swap:ABCUSDT:
    1700000000000") under one canonical_asset_id."""
    return parse_identity_registry(
        {
            "schema_version": 1,
            "registry_version": "test_registry_v1",
            "links": [
                {
                    "canonical_asset_id": "asset:abc",
                    "exchange": "gate",
                    "instrument_identity_key": "gate:swap:ABC_USDT:1",
                    "evidence_url": _EVIDENCE_URL,
                    "evidence_sha256": _EVIDENCE_SHA256,
                },
                {
                    "canonical_asset_id": "asset:abc",
                    "exchange": target_exchange,
                    "instrument_identity_key": f"{target_exchange}:swap:ABCUSDT:1700000000000",
                    "evidence_url": _EVIDENCE_URL,
                    "evidence_sha256": _EVIDENCE_SHA256,
                },
            ],
        }
    )


def _abc_registry_both() -> IdentityRegistry:
    """Like _abc_registry, but registers both binance and bybit as targets
    -- for tests that exercise more than one target exchange client."""
    return parse_identity_registry(
        {
            "schema_version": 1,
            "registry_version": "test_registry_v1",
            "links": [
                {
                    "canonical_asset_id": "asset:abc",
                    "exchange": "gate",
                    "instrument_identity_key": "gate:swap:ABC_USDT:1",
                    "evidence_url": _EVIDENCE_URL,
                    "evidence_sha256": _EVIDENCE_SHA256,
                },
                *(
                    {
                        "canonical_asset_id": "asset:abc",
                        "exchange": exchange,
                        "instrument_identity_key": f"{exchange}:swap:ABCUSDT:1700000000000",
                        "evidence_url": _EVIDENCE_URL,
                        "evidence_sha256": _EVIDENCE_SHA256,
                    }
                    for exchange in ("binance", "bybit")
                ),
            ],
        }
    )


_EMPTY_REGISTRY = parse_identity_registry(
    {"schema_version": 1, "registry_version": "test_empty_registry_v1", "links": []}
)


def _source(
    exchange: str,
    *,
    event_id: int = 7,
    first_seen_at: datetime | None = None,
    conflict: bool = False,
) -> SourceObservation:
    observed = first_seen_at or datetime(2026, 8, 2, 0, 0, tzinfo=UTC)
    return SourceObservation(
        event_id=event_id,
        base="ABC",
        exchange=exchange,
        symbol="ABC_USDT",
        identity_key=f"{exchange}:swap:ABC_USDT:1",
        market_id="ABC_USDT",
        market_type="swap",
        quote_asset="USDT",
        settle_asset="USDT",
        identity_conflict=conflict,
        first_seen_at=observed,
        first_ticker_at=observed - timedelta(seconds=1),
        first_change_pct=25.0,
        first_price=1.25,
        first_volume_24h_usd=2_000_000.0,
    )


def _candidate(*, first_seen_at: datetime | None = None) -> SourceLeadCandidate:
    source = _source("gate", first_seen_at=first_seen_at)
    return SourceLeadCandidate(
        event_id=source.event_id,
        base=source.base,
        source=source,
        first_sources=("gate",),
        eligible=True,
        eligibility_reason="eligible",
    )


def test_unique_gate_first_is_eligible_without_future_confirmation() -> None:
    gate = _source("gate")
    later = _source("binance", first_seen_at=gate.first_seen_at + timedelta(seconds=20))

    candidates = build_source_lead_candidates((later, gate))

    assert len(candidates) == 1
    assert candidates[0].eligible is True
    assert candidates[0].first_sources == ("gate",)
    assert candidates[0].eligibility_reason == "eligible"


def test_timestamp_tie_is_preserved_and_excluded() -> None:
    observed = datetime(2026, 8, 2, tzinfo=UTC)

    candidate = build_source_lead_candidates(
        (_source("gate", first_seen_at=observed), _source("mexc", first_seen_at=observed))
    )[0]

    assert candidate.eligible is False
    assert candidate.first_sources == ("gate", "mexc")
    assert candidate.eligibility_reason == "gate_not_unique_first_source"


def test_source_identity_conflict_fails_closed() -> None:
    candidate = build_source_lead_candidates((_source("gate", conflict=True),))[0]

    assert candidate.eligible is False
    assert candidate.eligibility_reason == "source_identity_conflict"


async def test_prepare_does_not_truncate_the_durable_denominator(monkeypatch: Any) -> None:
    candidates = tuple(_candidate() for _ in range(9))
    load = AsyncMock(return_value=candidates)
    claim = AsyncMock(return_value=())
    monkeypatch.setattr(
        "schurfer_analytics.source_lead_capture.load_source_lead_candidates",
        load,
    )
    monkeypatch.setattr(
        "schurfer_analytics.source_lead_capture.claim_source_lead_captures",
        claim,
    )

    await prepare_source_lead_captures(
        "postgresql://test",
        set(range(9)),
        datetime(2026, 8, 2, tzinfo=UTC),
    )

    claim.assert_awaited_once()
    claim_call = claim.await_args
    assert claim_call is not None
    assert claim_call.args[1] == candidates


def test_order_book_summary_uses_executable_vwap_on_both_sides() -> None:
    summary = summarize_order_book(
        {
            "bids": [[99.0, 0.3], [98.0, 1.0]],
            "asks": [[101.0, 0.3], [102.0, 1.0]],
        },
        target_usd=50.0,
        contract_size=1.0,
    )

    assert summary["best_bid"] == 99.0
    assert summary["best_ask"] == 101.0
    assert summary["spread_bps"] == 200.0
    assert summary["bid_filled_notional_usd"] == 50.0
    assert summary["ask_filled_notional_usd"] == 50.0
    assert summary["bid_impact_bps"] is not None
    assert summary["ask_impact_bps"] is not None


class _Exchange:
    def __init__(self, *, onboarded_at_ms: int = 1_700_000_000_000) -> None:
        market = {
            "id": "ABCUSDT",
            "symbol": "ABC/USDT:USDT",
            "active": True,
            "swap": True,
            "linear": True,
            "contract": True,
            "contractSize": 1.0,
            "base": "ABC",
            "quote": "USDT",
            "settle": "USDT",
            "type": "swap",
            "info": {"onboardDate": onboarded_at_ms},
        }
        self.markets = {"ABC/USDT:USDT": market}
        # ccxt's own native-id index, built by load_markets() -- see
        # _resolve_registered_target_market's docstring for why this is
        # what target-market resolution now uses, not exchange.markets
        # keyed by a guessed unified symbol.
        self.markets_by_id: dict[str, Any] = {"ABCUSDT": market}
        self.fetch_ticker = AsyncMock(
            return_value={
                "last": 2.0,
                "percentage": 5.0,
                "quoteVolume": 3_000_000.0,
                "timestamp": 1_785_628_799_000,
            }
        )
        self.fetch_order_book = AsyncMock(
            return_value={"bids": [[1.99, 100]], "asks": [[2.01, 100]]}
        )
        self.load_markets = AsyncMock(return_value=self.markets)
        self.close = AsyncMock()


async def test_target_capture_resolves_via_registry_and_marks_identity_verified() -> None:
    """The exact behavior colleague review (2026-08-28) required: target
    market resolution goes through the identity registry's own
    instrument_identity_key, never a guessed symbol, and a successful
    resolution is recorded as genuinely identity_verified."""
    result = await capture_target_observation(
        "binance",
        _Exchange(),
        _candidate(),
        target_usd=50.0,
        timeout_seconds=1.0,
        registry=_abc_registry(),
    )

    assert result.status == "sampled"
    assert result.eligibility_reason == "identity_verified"
    assert result.identity_verified is True
    assert result.identity_match_method == IDENTITY_MATCH_METHOD_REGISTRY_EXACT_V2
    assert result.instrument["market_id"] == "ABCUSDT"
    assert result.ticker["last"] == 2.0
    assert result.liquidity["ask_filled_notional_usd"] == 50.0


async def test_target_capture_excluded_when_source_identity_unregistered() -> None:
    """A candidate whose own source identity has no registry link at all is
    excluded before any network call -- no guessed-symbol fallback."""
    exchange = _Exchange()

    result = await capture_target_observation(
        "binance",
        exchange,
        _candidate(),
        target_usd=50.0,
        timeout_seconds=1.0,
        registry=_EMPTY_REGISTRY,
    )

    assert result.status == "excluded"
    assert result.eligibility_reason == "source_identity_unregistered"
    assert result.identity_verified is False
    assert result.identity_match_method == IDENTITY_MATCH_METHOD_REGISTRY_LOOKUP_V2
    exchange.fetch_ticker.assert_not_awaited()
    exchange.fetch_order_book.assert_not_awaited()


async def test_target_capture_excluded_when_no_registered_target() -> None:
    """Source is registered, but this specific exchange has no registered
    link for that canonical asset -- excluded before any network call."""
    exchange = _Exchange()
    registry = _abc_registry(target_exchange="bybit")  # registered for bybit, not binance

    result = await capture_target_observation(
        "binance",
        exchange,
        _candidate(),
        target_usd=50.0,
        timeout_seconds=1.0,
        registry=registry,
    )

    assert result.status == "excluded"
    assert result.eligibility_reason == "no_registered_target"
    assert result.identity_verified is False
    assert result.identity_match_method == IDENTITY_MATCH_METHOD_REGISTRY_LOOKUP_V2
    exchange.fetch_ticker.assert_not_awaited()
    exchange.fetch_order_book.assert_not_awaited()


async def test_target_capture_excluded_when_registered_identity_not_found_in_markets() -> None:
    """The registry names an instrument_identity_key that this run's loaded
    markets do not actually contain (e.g. delisted since the link was
    registered, or a market_id genuinely absent) -- excluded, not guessed."""
    exchange = _Exchange()
    registry = parse_identity_registry(
        {
            "schema_version": 1,
            "registry_version": "test_registry_v1",
            "links": [
                {
                    "canonical_asset_id": "asset:abc",
                    "exchange": "gate",
                    "instrument_identity_key": "gate:swap:ABC_USDT:1",
                    "evidence_url": _EVIDENCE_URL,
                    "evidence_sha256": _EVIDENCE_SHA256,
                },
                {
                    "canonical_asset_id": "asset:abc",
                    "exchange": "binance",
                    "instrument_identity_key": "binance:swap:NONEXISTENT:1",
                    "evidence_url": _EVIDENCE_URL,
                    "evidence_sha256": _EVIDENCE_SHA256,
                },
            ],
        }
    )

    result = await capture_target_observation(
        "binance",
        exchange,
        _candidate(),
        target_usd=50.0,
        timeout_seconds=1.0,
        registry=registry,
    )

    assert result.status == "excluded"
    assert result.eligibility_reason == "target_registry_identity_not_found"
    assert result.identity_verified is False
    assert result.identity_match_method == IDENTITY_MATCH_METHOD_REGISTRY_LOOKUP_V2
    exchange.fetch_ticker.assert_not_awaited()
    exchange.fetch_order_book.assert_not_awaited()


async def test_target_capture_picks_the_matching_market_among_id_collisions() -> None:
    """The realistic collision shape confirmed this session (TUT/agentT):
    the same native market_id key can map to more than one market in
    markets_by_id (ccxt returns a list when several market types share an
    id). Only the one whose recomputed identity_key matches the registered
    link exactly may be selected."""
    colliding_market = {
        "id": "ABCUSDT",
        "symbol": "ABC/USDT:USDT",
        "active": True,
        "swap": True,
        "linear": True,
        "contractSize": 1.0,
        "base": "ABC",
        "quote": "USDT",
        "settle": "USDT",
        "type": "swap",
        # A different onboarded_at -- a different identity_key, and
        # therefore a different, non-matching instrument.
        "info": {"onboardDate": 1_650_000_000_000},
    }
    exchange = _Exchange()
    real_market = exchange.markets["ABC/USDT:USDT"]
    exchange.markets_by_id = {"ABCUSDT": [colliding_market, real_market]}

    result = await capture_target_observation(
        "binance",
        exchange,
        _candidate(),
        target_usd=50.0,
        timeout_seconds=1.0,
        registry=_abc_registry(),
    )

    assert result.status == "sampled"
    assert result.identity_verified is True
    assert result.instrument["onboarded_at_ms"] == 1_700_000_000_000


async def test_target_listed_after_source_is_excluded_before_fetch() -> None:
    exchange = _Exchange(onboarded_at_ms=1_900_000_000_000)
    registry = parse_identity_registry(
        {
            "schema_version": 1,
            "registry_version": "test_registry_v1",
            "links": [
                {
                    "canonical_asset_id": "asset:abc",
                    "exchange": "gate",
                    "instrument_identity_key": "gate:swap:ABC_USDT:1",
                    "evidence_url": _EVIDENCE_URL,
                    "evidence_sha256": _EVIDENCE_SHA256,
                },
                {
                    "canonical_asset_id": "asset:abc",
                    "exchange": "binance",
                    "instrument_identity_key": "binance:swap:ABCUSDT:1900000000000",
                    "evidence_url": _EVIDENCE_URL,
                    "evidence_sha256": _EVIDENCE_SHA256,
                },
            ],
        }
    )

    result = await capture_target_observation(
        "binance",
        exchange,
        _candidate(),
        target_usd=50.0,
        timeout_seconds=1.0,
        registry=registry,
    )

    assert result.status == "excluded"
    assert result.eligibility_reason == "target_listed_after_source"
    # Identity WAS confirmed -- _resolve_registered_target_market already
    # matched this exact market before the onboarding-time check ran
    # (colleague review, 2026-08-28: a post-resolution eligibility failure
    # must not be tagged as if the route was never resolved at all).
    assert result.identity_verified is True
    assert result.identity_match_method == IDENTITY_MATCH_METHOD_REGISTRY_EXACT_V2
    exchange.fetch_ticker.assert_not_awaited()
    exchange.fetch_order_book.assert_not_awaited()


async def test_target_capture_marks_identity_verified_even_when_market_is_inactive() -> None:
    """target_inactive is a post-resolution eligibility failure, same class
    as target_listed_after_source: the market WAS matched exactly against
    the registry before this check ran, so identity is confirmed even
    though the row is excluded (colleague review, 2026-08-28)."""
    exchange = _Exchange()
    exchange.markets["ABC/USDT:USDT"]["active"] = False

    result = await capture_target_observation(
        "binance",
        exchange,
        _candidate(),
        target_usd=50.0,
        timeout_seconds=1.0,
        registry=_abc_registry(),
    )

    assert result.status == "excluded"
    assert result.eligibility_reason == "target_inactive"
    assert result.identity_verified is True
    assert result.identity_match_method == IDENTITY_MATCH_METHOD_REGISTRY_EXACT_V2


async def test_target_capture_marks_identity_verified_even_when_fetch_fails() -> None:
    """target_fetch_failed is reached only after _resolve_registered_target
    _market already matched the exact market -- the network call itself is
    what failed. identity_verified must stay True: the previous behavior
    (always False on any failure) claimed a confirmed route had never been
    resolved at all (colleague review, 2026-08-28)."""
    exchange = _Exchange()
    exchange.fetch_ticker = AsyncMock(side_effect=TimeoutError("no response"))

    result = await capture_target_observation(
        "binance",
        exchange,
        _candidate(),
        target_usd=50.0,
        timeout_seconds=1.0,
        registry=_abc_registry(),
    )

    assert result.status == "fetch_failed"
    assert result.eligibility_reason == "target_fetch_failed"
    assert result.identity_verified is True
    assert result.identity_match_method == IDENTITY_MATCH_METHOD_REGISTRY_EXACT_V2


async def test_capture_processes_target_clients_sequentially(monkeypatch: Any) -> None:
    # After IDENTITY_REGISTRY_V3_START so the "source_identity_unapproved"
    # assertion below actually exercises the empty-registry lookup this
    # test is about, rather than being short-circuited by the (unrelated)
    # pre-activation exclusion qualify_source_lead checks first.
    candidate = _candidate(first_seen_at=IDENTITY_REGISTRY_V3_START + timedelta(hours=1))
    claimed = (ClaimedCapture(capture_id=11, candidate=candidate),)
    active = 0
    maximum_active = 0

    class TrackedExchange:
        def __init__(self) -> None:
            template = _Exchange()
            self.markets = template.markets
            self.markets_by_id = template.markets_by_id

        async def load_markets(self) -> dict[str, Any]:
            nonlocal active, maximum_active
            active += 1
            maximum_active = max(maximum_active, active)
            return self.markets

        async def fetch_ticker(self, _symbol: str) -> dict[str, Any]:
            return {
                "last": 2.0,
                "percentage": 5.0,
                "quoteVolume": 3_000_000.0,
                "timestamp": 1_785_628_799_000,
            }

        async def fetch_order_book(self, _symbol: str, _limit: int) -> dict[str, Any]:
            return {"bids": [[1.99, 100]], "asks": [[2.01, 100]]}

        async def close(self, **_kwargs: Any) -> None:
            nonlocal active
            active -= 1

    persisted: dict[int, list[TargetObservation]] = {}
    qualifications: dict[int, QualificationResult] = {}

    async def persist(
        _db_url: str,
        results: dict[int, list[TargetObservation]],
        captured_qualifications: dict[int, QualificationResult],
        _registry_version: str,
        _registry_fingerprint: str,
    ) -> None:
        persisted.update(results)
        qualifications.update(captured_qualifications)

    monkeypatch.setattr(
        "schurfer_analytics.source_lead_capture.load_source_lead_candidates",
        AsyncMock(return_value=(candidate,)),
    )
    monkeypatch.setattr(
        "schurfer_analytics.source_lead_capture.claim_source_lead_captures",
        AsyncMock(return_value=claimed),
    )
    monkeypatch.setattr(
        "schurfer_analytics.source_lead_capture._persist_target_observations",
        persist,
    )

    await capture_new_source_leads(
        "postgresql://test",
        {7},
        datetime(2026, 8, 2, tzinfo=UTC),
        target_exchanges=("binance", "bybit"),
        target_usd=50.0,
        timeout_seconds=1.0,
        batch_size=8,
        factories={"binance": TrackedExchange, "bybit": TrackedExchange},
        # Both exchanges need a registered route -- otherwise
        # _exchange_has_registered_route (colleague review, 2026-08-28)
        # skips creating the client entirely, and there is nothing left for
        # this test to prove is sequential.
        identity_registry=_abc_registry_both(),
    )

    assert maximum_active == 1
    assert active == 0
    assert [row.target_exchange for row in persisted[11]] == ["binance", "bybit"]
    # ROUTE_EVIDENCE_INDEPENDENTLY_VERIFIED=True as of research/gate-source-
    # lead-registry-activation-v3 (PR 3 of 3): the pipeline runs identity
    # and liquidity checks to completion and now actually reaches
    # status='qualified' with a selected venue.
    assert qualifications[11].status == "qualified"
    assert qualifications[11].reason == "lowest_round_trip_impact"


async def test_capture_skips_exchange_client_entirely_when_no_route_is_registered(
    monkeypatch: Any,
) -> None:
    """Batch-level counterpart to _exchange_has_registered_route's own unit
    tests: with an empty registry, capture_new_source_leads must never
    create an exchange client or call load_markets at all (colleague
    review, 2026-08-28 -- AI_RULES.md requires gating before expensive work
    starts, not only at the final per-candidate check)."""
    candidate = _candidate(first_seen_at=IDENTITY_REGISTRY_V3_START + timedelta(hours=1))
    claimed = (ClaimedCapture(capture_id=21, candidate=candidate),)
    created = 0

    class CountingExchange:
        def __init__(self) -> None:
            nonlocal created
            created += 1

        async def load_markets(self) -> dict[str, Any]:
            raise AssertionError("load_markets must not be called with no registered route")

        async def close(self, **_kwargs: Any) -> None:
            pass

    persisted: dict[int, list[TargetObservation]] = {}
    qualifications: dict[int, QualificationResult] = {}

    async def persist(
        _db_url: str,
        results: dict[int, list[TargetObservation]],
        captured_qualifications: dict[int, QualificationResult],
        _registry_version: str,
        _registry_fingerprint: str,
    ) -> None:
        persisted.update(results)
        qualifications.update(captured_qualifications)

    monkeypatch.setattr(
        "schurfer_analytics.source_lead_capture.load_source_lead_candidates",
        AsyncMock(return_value=(candidate,)),
    )
    monkeypatch.setattr(
        "schurfer_analytics.source_lead_capture.claim_source_lead_captures",
        AsyncMock(return_value=claimed),
    )
    monkeypatch.setattr(
        "schurfer_analytics.source_lead_capture._persist_target_observations",
        persist,
    )

    await capture_new_source_leads(
        "postgresql://test",
        {7},
        datetime(2026, 8, 2, tzinfo=UTC),
        target_exchanges=("binance", "bybit"),
        target_usd=50.0,
        timeout_seconds=1.0,
        batch_size=8,
        factories={"binance": CountingExchange, "bybit": CountingExchange},
        identity_registry=_EMPTY_REGISTRY,
    )

    assert created == 0
    assert [row.eligibility_reason for row in persisted[21]] == [
        "source_identity_unregistered",
        "source_identity_unregistered",
    ]
    assert qualifications[21].reason == "source_identity_unapproved"


def test_capture_contract_is_versioned_and_identity_method_is_explicit() -> None:
    assert CAPTURE_VERSION == "source_lead_prospective_capture_v1"
    assert IDENTITY_MATCH_METHOD_BASE_SYMBOL_V1 == "base_symbol_v1"
    assert IDENTITY_MATCH_METHOD_REGISTRY_EXACT_V2 == "registry_exact_v2"


async def test_worker_keeps_slow_network_capture_single_and_off_caller_path(
    monkeypatch: Any,
) -> None:
    first = (ClaimedCapture(capture_id=11, candidate=_candidate()),)
    second = (ClaimedCapture(capture_id=12, candidate=_candidate()),)
    started = asyncio.Event()
    release = asyncio.Event()
    active = 0
    maximum_active = 0
    processed: list[int] = []

    async def capture(
        _db_url: str,
        claimed: tuple[ClaimedCapture, ...],
        **_kwargs: Any,
    ) -> None:
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        started.set()
        await release.wait()
        processed.append(claimed[0].capture_id)
        active -= 1

    abandon = AsyncMock()
    recover = AsyncMock(return_value=0)
    monkeypatch.setattr(
        "schurfer_analytics.source_lead_capture.capture_claimed_source_leads",
        capture,
    )
    monkeypatch.setattr(
        "schurfer_analytics.source_lead_capture.abandon_source_lead_captures",
        abandon,
    )
    monkeypatch.setattr(
        "schurfer_analytics.source_lead_capture.abandon_previous_process_captures",
        recover,
    )
    worker = SourceLeadCaptureWorker(
        "postgresql://test",
        target_exchanges=("binance", "bybit"),
        target_usd=50.0,
        timeout_seconds=5.0,
        queue_size=4,
        shutdown_timeout_seconds=1.0,
    )
    worker.start()

    await worker.submit(first)
    await started.wait()
    # Submission returns while the first network call is deliberately blocked.
    await worker.submit(second)
    assert worker.pending_batches == 1

    release.set()
    await worker.close()

    assert processed == [11, 12]
    assert maximum_active == 1
    abandon.assert_not_awaited()
    recover.assert_awaited_once()


async def test_worker_queue_overflow_is_durable_abandonment(monkeypatch: Any) -> None:
    first = (ClaimedCapture(capture_id=11, candidate=_candidate()),)
    second = (ClaimedCapture(capture_id=12, candidate=_candidate()),)
    abandon = AsyncMock()
    monkeypatch.setattr(
        "schurfer_analytics.source_lead_capture.abandon_source_lead_captures",
        abandon,
    )
    worker = SourceLeadCaptureWorker(
        "postgresql://test",
        target_exchanges=("binance", "bybit"),
        target_usd=50.0,
        timeout_seconds=5.0,
        queue_size=1,
        shutdown_timeout_seconds=1.0,
    )
    # Freeze consumption to exercise the bounded queue deterministically.
    monkeypatch.setattr(worker, "start", lambda: None)

    await worker.submit(first)
    await worker.submit(second)

    assert worker.pending_batches == 1
    assert worker.queue_dropped_total == 1
    abandon.assert_awaited_once_with("postgresql://test", second, "capture_queue_full")


async def test_worker_failure_is_abandoned_without_stopping_next_batch(monkeypatch: Any) -> None:
    first = (ClaimedCapture(capture_id=11, candidate=_candidate()),)
    second = (ClaimedCapture(capture_id=12, candidate=_candidate()),)
    processed: list[int] = []

    async def capture(
        _db_url: str,
        claimed: tuple[ClaimedCapture, ...],
        **_kwargs: Any,
    ) -> None:
        capture_id = claimed[0].capture_id
        processed.append(capture_id)
        if capture_id == 11:
            raise RuntimeError("exchange failed")

    abandon = AsyncMock()
    monkeypatch.setattr(
        "schurfer_analytics.source_lead_capture.capture_claimed_source_leads",
        capture,
    )
    monkeypatch.setattr(
        "schurfer_analytics.source_lead_capture.abandon_source_lead_captures",
        abandon,
    )
    monkeypatch.setattr(
        "schurfer_analytics.source_lead_capture.abandon_previous_process_captures",
        AsyncMock(return_value=0),
    )
    worker = SourceLeadCaptureWorker(
        "postgresql://test",
        target_exchanges=("binance", "bybit"),
        target_usd=50.0,
        timeout_seconds=5.0,
        queue_size=4,
        shutdown_timeout_seconds=1.0,
    )

    await worker.submit(first)
    await worker.submit(second)
    await worker.close()

    assert processed == [11, 12]
    abandon.assert_awaited_once()
    abandon_call = abandon.await_args
    assert abandon_call is not None
    assert abandon_call.args[1] == first
    assert abandon_call.args[2].startswith("capture_worker_failed: RuntimeError")
