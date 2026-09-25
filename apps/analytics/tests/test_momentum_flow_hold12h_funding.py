"""Pure tests for the hold12h actual-funding capture + coverage rule."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from schurfer_analytics.derivatives_history import DerivativesHistoryFetch
from schurfer_analytics.momentum_flow_hold12h_funding import (
    ACTUAL_FUNDING_VERSION,
    CoverageRun,
    StoredFundingSource,
    coverage_for_interval,
)
from schurfer_analytics.momentum_flow_hold12h_funding_resolver import (
    FundingRateConflictError,
    ParsedSettlement,
    coverage_status,
    fetch_bybit_native_funding,
    parse_settlement,
    resolve_window,
    window_status,
)
from schurfer_analytics.momentum_flow_hold12h_verdict_report import (
    InstrumentRoute,
    SettlementEvent,
)

_V = ACTUAL_FUNDING_VERSION
_ENTRY = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
_EXIT = _ENTRY + timedelta(hours=12)
_ROUTE = InstrumentRoute("bybit", "linear", "FOOUSDT", "FOO/USDT:USDT")


def _run(
    status: str = "complete", *, since_off: int = -1, until_off: int = 13, version: str = _V
) -> CoverageRun:
    return CoverageRun(
        requested_since=_ENTRY + timedelta(hours=since_off),
        requested_until=_ENTRY + timedelta(hours=until_off),
        status=status,
        source_version=version,
    )


def _sev(hours: float, rate: float, version: str = _V) -> SettlementEvent:
    return SettlementEvent(_ENTRY + timedelta(hours=hours), rate, version)


# --- coverage_for_interval -----------------------------------------------------


def test_coverage_proven_returns_version_events() -> None:
    settlements = [_sev(4, 0.001), _sev(8, -0.002), _sev(4, 0.9, version="other")]
    cov = coverage_for_interval(settlements, [_run()], entry_at=_ENTRY, exit_at=_EXIT)
    assert cov is not None
    assert cov.proven_full_coverage
    assert [e.rate for e in cov.events] == [0.001, -0.002]  # other-version event excluded


def test_coverage_none_without_a_complete_spanning_run() -> None:
    one = [_sev(4, 0.001)]
    assert coverage_for_interval(one, [], entry_at=_ENTRY, exit_at=_EXIT) is None
    assert coverage_for_interval(one, [_run("incomplete")], entry_at=_ENTRY, exit_at=_EXIT) is None
    # A complete run that does not SPAN the interval does not prove coverage.
    assert (
        coverage_for_interval([_sev(4, 0.001)], [_run(since_off=1)], entry_at=_ENTRY, exit_at=_EXIT)
        is None
    )
    # Wrong source version.
    assert (
        coverage_for_interval(
            [_sev(4, 0.001)], [_run(version="other")], entry_at=_ENTRY, exit_at=_EXIT
        )
        is None
    )


def test_coverage_proven_zero_funding_is_not_assumed() -> None:
    # No settlements but a COMPLETE spanning run -> proven zero funding (empty events).
    cov = coverage_for_interval([], [_run()], entry_at=_ENTRY, exit_at=_EXIT)
    assert cov is not None
    assert cov.events == ()
    assert cov.proven_full_coverage


def test_coverage_blocked_by_overlapping_integrity_conflict() -> None:
    # A complete run PLUS an overlapping integrity-conflict run -> accounting_incomplete:
    # the compromised rate must not keep feeding the verdict until a human resolves it.
    settlements = [_sev(4, 0.001)]
    runs = [_run(), _run("integrity_conflict")]
    assert coverage_for_interval(settlements, runs, entry_at=_ENTRY, exit_at=_EXIT) is None
    # A conflict that does NOT overlap the interval does not block it.
    far = _run("integrity_conflict", since_off=48, until_off=60)
    cov = coverage_for_interval(settlements, [_run(), far], entry_at=_ENTRY, exit_at=_EXIT)
    assert cov is not None and cov.proven_full_coverage


# --- StoredFundingSource (exact-route only) ------------------------------------


def test_stored_source_keys_on_exact_route_not_ticker() -> None:
    settlements: dict[tuple[str, str], tuple[SettlementEvent, ...]] = {
        ("bybit", "FOOUSDT"): (_sev(4, 0.001),)
    }
    runs: dict[tuple[str, str], tuple[CoverageRun, ...]] = {("bybit", "FOOUSDT"): (_run(),)}
    source = StoredFundingSource(settlements, runs)
    assert source.coverage(_ROUTE, _ENTRY, _EXIT) is not None
    # A different native market id (same base ticker) must NOT match.
    other = InstrumentRoute("bybit", "linear", "FOO-PERP", "FOO/USDT:USDT")
    assert source.coverage(other, _ENTRY, _EXIT) is None


# --- parse_settlement (any cadence, invalid rejected) --------------------------


def test_parse_settlement_valid_and_variable_cadence() -> None:
    base = int(_ENTRY.timestamp() * 1000)
    a = parse_settlement({"timestamp": base, "fundingRate": 0.0001})
    b = parse_settlement({"timestamp": base + 3_600_000, "fundingRate": -0.0002})  # 1h later
    c = parse_settlement({"timestamp": base + 3_600_000 + 5 * 3_600_000, "fundingRate": 0.0})  # +5h
    assert a is not None and b is not None and c is not None
    assert a.funding_rate == 0.0001
    assert b.settlement_at < c.settlement_at  # irregular gaps accepted verbatim


def test_parse_settlement_rejects_invalid() -> None:
    base = int(_ENTRY.timestamp() * 1000)
    assert parse_settlement("not a dict") is None
    assert parse_settlement({"fundingRate": 0.1}) is None  # no timestamp
    assert parse_settlement({"timestamp": base, "fundingRate": None}) is None
    assert parse_settlement({"timestamp": base, "fundingRate": float("nan")}) is None
    assert parse_settlement({"timestamp": 123, "fundingRate": 0.1}) is None  # sub-2000 ms


# --- coverage_status -----------------------------------------------------------


def test_coverage_status_only_clean_fetch_is_complete() -> None:
    assert coverage_status(DerivativesHistoryFetch(rows=(), request_count=1)) == "complete"
    assert (
        coverage_status(DerivativesHistoryFetch((), 1, error_status="fetch_failed"))
        == "fetch_failed"
    )
    assert (
        coverage_status(DerivativesHistoryFetch((), 1, error_status="invalid_response"))
        == "invalid_response"
    )
    assert (
        coverage_status(DerivativesHistoryFetch((), 3, pagination_exhausted=True))
        == "pagination_exhausted"
    )


# --- window_status (bracket + parse-failure proof) -----------------------------


def _fetch(rows: tuple[Any, ...] = (), **kw: Any) -> DerivativesHistoryFetch:
    return DerivativesHistoryFetch(rows=rows, request_count=1, **kw)


def _ps(hours: float, rate: float) -> ParsedSettlement:
    return ParsedSettlement(_ENTRY + timedelta(hours=hours), rate, {"h": hours})


# A dense, gap-free 8h grid over the padded [entry-12h, exit+12h] window (entry=0, exit=12).
def _full_grid() -> list[ParsedSettlement]:
    return [_ps(h, 0.001) for h in (-8, 0, 8, 16, 24)]


def test_window_status_complete_on_dense_gap_free_grid() -> None:
    assert window_status(_fetch(), _full_grid(), 0, entry=_ENTRY, exit_at=_EXIT) == "complete"


def test_window_status_incomplete_two_edge_events_do_not_prove_interior() -> None:
    # One settlement 4h before entry and one 16h after exit, nothing inside the 12h
    # position: the 20h gap exceeds the 8h maximum interval and is an anomaly.
    two_edges = [_ps(-4, 0.001), _ps(16, 0.001)]
    assert window_status(_fetch(), two_edges, 0, entry=_ENTRY, exit_at=_EXIT) == "incomplete"


def test_window_status_incomplete_missing_interior_settlement() -> None:
    # 8h grid with the interior (hour 8, inside the position) dropped -> a 16h gap.
    missing = [_ps(h, 0.001) for h in (-8, 0, 16, 24)]
    assert window_status(_fetch(), missing, 0, entry=_ENTRY, exit_at=_EXIT) == "incomplete"


def test_window_status_incomplete_without_start_bracket() -> None:
    # Earliest settlement is after entry: could be the edge of available history.
    no_start = [_ps(4, 0.001), _ps(8, 0.001), _ps(20, 0.001)]
    assert window_status(_fetch(), no_start, 0, entry=_ENTRY, exit_at=_EXIT) == "incomplete"


def test_window_status_incomplete_without_end_bracket() -> None:
    no_end = [_ps(-8, 0.001), _ps(-4, 0.001), _ps(6, 0.001)]
    assert window_status(_fetch(), no_end, 0, entry=_ENTRY, exit_at=_EXIT) == "incomplete"


def test_window_status_incomplete_on_dropped_row() -> None:
    # A dense gap-free grid, but a fetched row failed to parse -> not complete.
    assert window_status(_fetch(), _full_grid(), 1, entry=_ENTRY, exit_at=_EXIT) == "incomplete"


def _hours(*values: float) -> list[ParsedSettlement]:
    return [_ps(h, 0.0001) for h in values]


def _utc(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def _real(*stamps: str) -> list[ParsedSettlement]:
    return [ParsedSettlement(_utc(stamp), 0.0001, {"t": stamp}) for stamp in stamps]


@pytest.mark.parametrize(
    ("entry", "exit_at", "settlements"),
    [
        # IOSTUSDT: hourly until 09-15 18:00, then back to the 00/08/16 grid.
        (
            "2026-09-15T23:32:48",
            "2026-09-16T11:32:58",
            (
                "2026-09-15T12:00",
                "2026-09-15T13:00",
                "2026-09-15T14:00",
                "2026-09-15T15:00",
                "2026-09-15T16:00",
                "2026-09-15T17:00",
                "2026-09-15T18:00",
                "2026-09-16T00:00",
                "2026-09-16T08:00",
                "2026-09-16T16:00",
            ),
        ),
        # MTLUSDT: one 4h step, then 8h.
        (
            "2026-09-18T23:43:56",
            "2026-09-19T11:43:59",
            (
                "2026-09-18T12:00",
                "2026-09-18T16:00",
                "2026-09-19T00:00",
                "2026-09-19T08:00",
                "2026-09-19T16:00",
            ),
        ),
        # B3USDT: hourly, then 4h.
        (
            "2026-09-18T16:56:36",
            "2026-09-19T04:56:48",
            (
                "2026-09-18T05:00",
                "2026-09-18T06:00",
                "2026-09-18T07:00",
                "2026-09-18T08:00",
                "2026-09-18T09:00",
                "2026-09-18T10:00",
                "2026-09-18T11:00",
                "2026-09-18T12:00",
                "2026-09-18T16:00",
                "2026-09-18T20:00",
                "2026-09-19T00:00",
                "2026-09-19T04:00",
                "2026-09-19T08:00",
                "2026-09-19T12:00",
                "2026-09-19T16:00",
            ),
        ),
    ],
    ids=["IOSTUSDT", "MTLUSDT", "B3USDT"],
)
def test_window_status_accepts_real_cadence_switches(
    entry: str, exit_at: str, settlements: tuple[str, ...]
) -> None:
    """Real Bybit v5 settlement timestamps (UTC, as the venue returned them) around real
    hold12h positions that v1 wrongly marked incomplete: a v1-style global minimum cadence
    called every legitimate cadence transition a hole."""
    status = window_status(
        _fetch(), _real(*settlements), 0, entry=_utc(entry), exit_at=_utc(exit_at)
    )
    assert status == "complete"


def test_window_status_flags_a_settlement_off_the_hour() -> None:
    """Bybit settles on the hour. An off-hour timestamp is an anomaly (unit or parsing
    fault), left incomplete; being on the hour is NOT a completeness proof."""
    off_hour = [_ps(h, 0.0001) for h in (-8, 0)] + [
        ParsedSettlement(_ENTRY + timedelta(hours=8, minutes=30), 0.0001, {}),
        _ps(16, 0.0001),
    ]
    assert window_status(_fetch(), off_hour, 0, entry=_ENTRY, exit_at=_EXIT) == "incomplete"


def test_window_status_cannot_detect_a_missing_event_inside_a_legal_gap() -> None:
    """Documents the residual risk of v2, not a guarantee: on a 4h schedule with the 04:00
    event missing, the remaining 00:00 -> 08:00 gap is a legal 8h interval, so the window
    is still complete. Completeness rests on the v5 history listing every settlement."""
    four_hourly_missing_one = _hours(-8, -4, 0, 8, 12, 16)
    assert (
        window_status(_fetch(), four_hourly_missing_one, 0, entry=_ENTRY, exit_at=_EXIT)
        == "complete"
    )


def test_window_status_propagates_fetch_error() -> None:
    assert (
        window_status(_fetch(error_status="fetch_failed"), [], 0, entry=_ENTRY, exit_at=_EXIT)
        == "fetch_failed"
    )
    assert (
        window_status(_fetch(pagination_exhausted=True), [], 0, entry=_ENTRY, exit_at=_EXIT)
        == "pagination_exhausted"
    )


# --- resolve_window orchestration (fake exchange + fake repo, no DB) ------------


def _v5(rows: list[dict[str, object]], symbol: str = "FOOUSDT") -> dict[str, Any]:
    """Bybit v5 envelope, newest first, with string fields as the venue sends them."""
    items = [
        {
            "symbol": symbol,
            "fundingRate": str(row["fundingRate"]),
            "fundingRateTimestamp": str(row["timestamp"]),
        }
        for row in sorted(rows, key=lambda row: int(str(row["timestamp"])), reverse=True)
    ]
    return {"retCode": 0, "retMsg": "OK", "result": {"category": "linear", "list": items}}


class _FakeExchange:
    """Answers ``public_get_v5_market_funding_history`` from a fixed row set, honoring
    ``startTime``/``endTime``/``limit`` like the venue (newest first)."""

    id = "bybit"

    def __init__(self, rows: list[dict[str, object]], *, symbol: str = "FOOUSDT") -> None:
        self._rows = rows
        self._symbol = symbol
        self.calls = 0
        self.params: list[dict[str, Any]] = []

    async def public_get_v5_market_funding_history(self, params: dict[str, Any]) -> Any:
        self.calls += 1
        self.params.append(dict(params))
        in_range = [
            row
            for row in self._rows
            if params["startTime"] <= int(str(row["timestamp"])) <= params["endTime"]
        ]
        newest_first = sorted(in_range, key=lambda row: int(str(row["timestamp"])), reverse=True)
        return _v5(newest_first[: params["limit"]], self._symbol)


class _FakeRepo:
    def __init__(self, *, conflict: bool = False) -> None:
        self.settlements: list[ParsedSettlement] = []
        self.runs: list[dict[str, Any]] = []
        self._conflict = conflict

    async def write_settlements(
        self, route: Any, settlements: Any, *, now: Any, source_version: Any
    ) -> int:
        if self._conflict:
            raise FundingRateConflictError("bybit:FOOUSDT funding rate changed on re-fetch")
        seen = {s.settlement_at for s in self.settlements}
        new = [s for s in settlements if s.settlement_at not in seen]
        self.settlements.extend(new)
        return len(new)

    async def write_coverage_run(self, route: Any, **kwargs: Any) -> None:
        self.runs.append(kwargs)


def _bracketed_rows() -> list[dict[str, object]]:
    # A dense, gap-free 8h grid over the padded window: brackets [entry, exit] and proves
    # the interior is complete (hours -8, 0, 8, 16, 24; entry=0, exit=12).
    base = int(_ENTRY.timestamp() * 1000)
    hour = 3_600_000
    rates = {-8: 0.0003, 0: 0.0001, 8: -0.0002, 16: 0.0004, 24: 0.0005}
    return [{"timestamp": base + h * hour, "fundingRate": r} for h, r in rates.items()]


async def test_resolve_window_fetches_parses_and_records() -> None:
    exchange = _FakeExchange(_bracketed_rows())
    repo = _FakeRepo()
    capture = await resolve_window(exchange, repo, _ROUTE, entry=_ENTRY, exit_at=_EXIT, now=_ENTRY)
    assert capture.status == "complete"
    assert capture.settlements_written == 5
    assert len(repo.runs) == 1 and repo.runs[0]["status"] == "complete"
    # The venue is queried by the exact native market id, never via a CCXT market lookup.
    assert exchange.params and all(p["symbol"] == _ROUTE.market_id for p in exchange.params)
    assert all(p["category"] == "linear" for p in exchange.params)
    # The recorded request window is padded on both sides of the target interval.
    assert repo.runs[0]["requested_since"] < _ENTRY
    assert repo.runs[0]["requested_until"] > _EXIT
    # Idempotent: a retry re-writes nothing.
    again = await resolve_window(
        _FakeExchange(_bracketed_rows()), repo, _ROUTE, entry=_ENTRY, exit_at=_EXIT, now=_ENTRY
    )
    assert again.settlements_written == 0


async def test_resolve_window_incomplete_when_history_does_not_bracket() -> None:
    base = int(_ENTRY.timestamp() * 1000)
    # Only in-window settlements: cannot prove the start boundary was not truncated.
    rows: list[dict[str, object]] = [
        {"timestamp": base + 4 * 3_600_000, "fundingRate": 0.0001},
        {"timestamp": base + 16 * 3_600_000, "fundingRate": 0.0004},
    ]
    repo = _FakeRepo()
    capture = await resolve_window(
        _FakeExchange(rows), repo, _ROUTE, entry=_ENTRY, exit_at=_EXIT, now=_ENTRY
    )
    assert capture.status == "incomplete"
    assert repo.runs[0]["status"] == "incomplete"


async def test_resolve_window_records_integrity_conflict_on_rate_conflict() -> None:
    repo = _FakeRepo(conflict=True)
    capture = await resolve_window(
        _FakeExchange(_bracketed_rows()), repo, _ROUTE, entry=_ENTRY, exit_at=_EXIT, now=_ENTRY
    )
    # A blocking status (not merely 'incomplete') so the reader invalidates any prior
    # 'complete' run for the window until the conflict is resolved.
    assert capture.status == "integrity_conflict"
    assert capture.integrity_conflict is True
    assert capture.settlements_written == 0
    assert repo.runs[0]["status"] == "integrity_conflict"
    assert "funding rate changed" in (repo.runs[0]["error"] or "")


async def test_resolve_window_records_fetch_failure_without_complete() -> None:
    class _BrokenExchange:
        id = "bybit"

        async def public_get_v5_market_funding_history(self, params: dict[str, Any]) -> Any:
            raise RuntimeError("venue down")

    repo = _FakeRepo()
    capture = await resolve_window(
        _BrokenExchange(), repo, _ROUTE, entry=_ENTRY, exit_at=_EXIT, now=_ENTRY
    )
    assert capture.status == "fetch_failed"
    assert capture.settlements_written == 0
    assert repo.runs[0]["status"] == "fetch_failed"


# --- native v5 fetch -----------------------------------------------------------


def _hourly_rows(count: int) -> list[dict[str, object]]:
    base = int(_ENTRY.timestamp() * 1000)
    return [{"timestamp": base + h * 3_600_000, "fundingRate": 0.0001} for h in range(count)]


async def test_native_fetch_pages_backwards_until_a_short_page() -> None:
    exchange = _FakeExchange(_hourly_rows(25))
    since = int(_ENTRY.timestamp() * 1000)
    fetch = await fetch_bybit_native_funding(
        exchange,
        "FOOUSDT",
        category="linear",
        since_ms=since,
        until_ms=since + 30 * 3_600_000,
        limit=10,
    )
    assert fetch.error_status is None and not fetch.pagination_exhausted
    assert exchange.calls == 3
    assert sorted(int(row["timestamp"]) for row in fetch.rows) == sorted(
        int(str(row["timestamp"])) for row in _hourly_rows(25)
    )
    # Raw venue item is kept for the stored native payload.
    assert fetch.rows[0]["info"]["symbol"] == "FOOUSDT"


async def test_native_fetch_reports_truncation_when_pages_run_out() -> None:
    exchange = _FakeExchange(_hourly_rows(25))
    since = int(_ENTRY.timestamp() * 1000)
    fetch = await fetch_bybit_native_funding(
        exchange,
        "FOOUSDT",
        category="linear",
        since_ms=since,
        until_ms=since + 30 * 3_600_000,
        limit=10,
        max_pages=2,
    )
    assert fetch.pagination_exhausted
    assert coverage_status(fetch) == "pagination_exhausted"


async def test_native_fetch_rejects_rows_for_another_symbol() -> None:
    exchange = _FakeExchange(_hourly_rows(3), symbol="BARUSDT")
    since = int(_ENTRY.timestamp() * 1000)
    fetch = await fetch_bybit_native_funding(
        exchange, "FOOUSDT", category="linear", since_ms=since, until_ms=since + 5 * 3_600_000
    )
    assert fetch.error_status == "invalid_response"


async def test_native_fetch_maps_a_venue_error_code_to_fetch_failed() -> None:
    class _ErrorExchange:
        async def public_get_v5_market_funding_history(self, params: dict[str, Any]) -> Any:
            return {"retCode": 10001, "retMsg": "params error", "result": {}}

    since = int(_ENTRY.timestamp() * 1000)
    fetch = await fetch_bybit_native_funding(
        _ErrorExchange(), "FOOUSDT", category="linear", since_ms=since, until_ms=since + 1
    )
    assert fetch.error_status == "fetch_failed"
    assert "params error" in (fetch.error or "")


async def test_delisted_instrument_is_captured_by_native_id() -> None:
    """ICXUSDT was delisted on 2026-09-18 and CCXT no longer lists it, but the v5 history
    still answers by native id. Real settlements around the 09-14 position."""
    icx = InstrumentRoute("bybit", "linear", "ICXUSDT", "ICX/USDT:USDT")
    entry = datetime(2026, 9, 13, 22, 27, tzinfo=UTC)
    exit_at = datetime(2026, 9, 14, 10, 27, tzinfo=UTC)
    settlements = (
        (datetime(2026, 9, 13, 16, tzinfo=UTC), 0.0001),
        (datetime(2026, 9, 14, 0, tzinfo=UTC), -0.0000397),
        (datetime(2026, 9, 14, 8, tzinfo=UTC), 0.0001),
        (datetime(2026, 9, 14, 16, tzinfo=UTC), 0.0001),
    )
    rows: list[dict[str, object]] = [
        {"timestamp": int(at.timestamp() * 1000), "fundingRate": rate} for at, rate in settlements
    ]
    exchange = _FakeExchange(rows, symbol="ICXUSDT")
    repo = _FakeRepo()
    capture = await resolve_window(exchange, repo, icx, entry=entry, exit_at=exit_at, now=exit_at)
    assert capture.status == "complete"
    assert {p["symbol"] for p in exchange.params} == {"ICXUSDT"}
    assert repo.runs[0]["source_version"] == ACTUAL_FUNDING_VERSION == "hold12h_actual_funding_v2"


async def test_unsupported_route_is_never_complete() -> None:
    route = InstrumentRoute("binance", "linear", "FOOUSDT", "FOO/USDT:USDT")
    repo = _FakeRepo()
    capture = await resolve_window(
        _FakeExchange(_bracketed_rows()), repo, route, entry=_ENTRY, exit_at=_EXIT, now=_ENTRY
    )
    assert capture.status == "fetch_failed"
    assert repo.runs[0]["status"] == "fetch_failed"
