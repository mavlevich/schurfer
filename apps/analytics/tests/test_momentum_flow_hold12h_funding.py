"""Pure tests for the hold12h actual-funding capture + coverage rule."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

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
    # The reviewer's case: one settlement 4h before entry and one 16h after exit, nothing
    # inside the 12h position. Brackets alone must NOT count as proven coverage.
    two_edges = [_ps(-4, 0.001), _ps(16, 0.001)]
    assert window_status(_fetch(), two_edges, 0, entry=_ENTRY, exit_at=_EXIT) == "incomplete"


def test_window_status_incomplete_missing_interior_settlement() -> None:
    # 8h grid with the interior (hour 8, inside the position) dropped -> a doubled gap.
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


class _FakeExchange:
    id = "bybit"

    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows
        self.calls = 0
        self.symbols: list[object] = []

    async def fetch_funding_rate_history(self, *args: object, **kwargs: object) -> list[Any]:
        self.calls += 1
        self.symbols.append(args[0] if args else kwargs.get("symbol"))
        return list(self._rows) if self.calls == 1 else []


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
    # Blocker 2: the resolved unified symbol is what CCXT is queried with, not the ticker.
    assert exchange.symbols and all(s == _ROUTE.unified_symbol for s in exchange.symbols)
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

        async def fetch_funding_rate_history(self, *args: object, **kwargs: object) -> list[Any]:
            raise RuntimeError("venue down")

    repo = _FakeRepo()
    capture = await resolve_window(
        _BrokenExchange(), repo, _ROUTE, entry=_ENTRY, exit_at=_EXIT, now=_ENTRY
    )
    assert capture.status == "fetch_failed"
    assert capture.settlements_written == 0
    assert repo.runs[0]["status"] == "fetch_failed"
