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
    ParsedSettlement,
    coverage_status,
    parse_settlement,
    resolve_window,
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


# --- resolve_window orchestration (fake exchange + fake repo, no DB) ------------


class _FakeExchange:
    id = "bybit"

    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows
        self.calls = 0

    async def fetch_funding_rate_history(self, *args: object, **kwargs: object) -> list[Any]:
        self.calls += 1
        return list(self._rows) if self.calls == 1 else []


class _FakeRepo:
    def __init__(self) -> None:
        self.settlements: list[ParsedSettlement] = []
        self.runs: list[dict[str, Any]] = []

    async def write_settlements(
        self, route: Any, settlements: Any, *, now: Any, source_version: Any
    ) -> int:
        seen = {s.settlement_at for s in self.settlements}
        new = [s for s in settlements if s.settlement_at not in seen]
        self.settlements.extend(new)
        return len(new)

    async def write_coverage_run(self, route: Any, **kwargs: Any) -> None:
        self.runs.append(kwargs)


async def test_resolve_window_fetches_parses_and_records() -> None:
    base = int(_ENTRY.timestamp() * 1000)
    rows: list[dict[str, object]] = [
        {"timestamp": base + 4 * 3_600_000, "fundingRate": 0.0001},
        {"timestamp": base + 12 * 3_600_000, "fundingRate": -0.0002},
    ]
    repo = _FakeRepo()
    capture = await resolve_window(
        _FakeExchange(rows), repo, _ROUTE, since=_ENTRY, until=_EXIT, now=_ENTRY
    )
    assert capture.status == "complete"
    assert capture.settlements_written == 2
    assert len(repo.runs) == 1 and repo.runs[0]["status"] == "complete"
    # Idempotent: a retry re-writes nothing.
    again = await resolve_window(
        _FakeExchange(rows), repo, _ROUTE, since=_ENTRY, until=_EXIT, now=_ENTRY
    )
    assert again.settlements_written == 0


async def test_resolve_window_records_fetch_failure_without_complete() -> None:
    class _BrokenExchange:
        id = "bybit"

        async def fetch_funding_rate_history(self, *args: object, **kwargs: object) -> list[Any]:
            raise RuntimeError("venue down")

    repo = _FakeRepo()
    capture = await resolve_window(
        _BrokenExchange(), repo, _ROUTE, since=_ENTRY, until=_EXIT, now=_ENTRY
    )
    assert capture.status == "fetch_failed"
    assert capture.settlements_written == 0
    assert repo.runs[0]["status"] == "fetch_failed"
