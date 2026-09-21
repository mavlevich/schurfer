"""Outcome-blind funding-rate snapshot for the abnormal-flow freeze contract.

Builds the PRIMARY conservative funding proxy from a fresh, durable pull of actual
per-instrument funding-settlement history (CCXT ``fetchFundingRateHistory``) for the
EXACT eligible universe (Bybit + Binance linear), strictly over the calibration window,
PER VENUE. It reads funding rates only -- never a forward price or PnL. The baseline is
``P95`` of ``max(funding_rate, 0)`` (long-only conservative) per venue; ``P99`` is kept
for the mandatory sensitivity test. The captured DB tables (hold12h_funding_settlements,
funding_rate_snapshots) are NOT used here: neither is unconditional/full-universe (no
Binance capture at all), so they are stress/diagnostic only.

The snapshot fixes source, version, window, coverage, and a content hash so the freeze
run reproduces the same numbers. The CCXT edge is injectable so the pure percentile /
coverage logic is unit-tested without the network.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Iterable

FUNDING_SNAPSHOT_VERSION = "abnormal_flow_funding_snapshot_v1"
_PERCENTILES = (0.50, 0.90, 0.95, 0.99)


@dataclass(frozen=True)
class FundingSettlement:
    exchange: str
    native_market_id: str
    settlement_at: datetime
    funding_rate: float


class FundingClient(Protocol):
    """The venue edge. ``unified_symbol`` maps a native market id to the CCXT unified
    symbol (or ``None`` if it is not a resolvable linear instrument); ``funding_history``
    returns ``(settlement_ms, rate)`` for that symbol within ``[since_ms, until_ms)``."""

    def unified_symbol(self, native_market_id: str) -> str | None: ...

    def funding_history(
        self, unified_symbol: str, since_ms: int, until_ms: int
    ) -> list[tuple[int, float]]: ...


def _percentile(sorted_values: list[float], p: float) -> float | None:
    """Linear-interpolated p-quantile of a pre-sorted list. ``None`` when empty."""
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = p * (len(sorted_values) - 1)
    lo = int(rank)
    frac = rank - lo
    if lo + 1 >= len(sorted_values):
        return sorted_values[-1]
    return sorted_values[lo] + (sorted_values[lo + 1] - sorted_values[lo]) * frac


def funding_percentiles(settlements: Iterable[FundingSettlement]) -> dict[str, dict[str, Any]]:
    """Per-venue percentiles of ``max(funding_rate, 0)`` (long-only conservative). Returns
    per venue: settlement count, distinct instruments, and P50/P90/P95/P99."""
    by_venue: dict[str, list[float]] = {}
    instruments: dict[str, set[str]] = {}
    for s in settlements:
        by_venue.setdefault(s.exchange, []).append(max(s.funding_rate, 0.0))
        instruments.setdefault(s.exchange, set()).add(s.native_market_id)
    out: dict[str, dict[str, Any]] = {}
    for venue, values in by_venue.items():
        values.sort()
        out[venue] = {
            "settlements": len(values),
            "instruments": len(instruments[venue]),
            "max_rate_0_percentiles": {
                f"p{int(p * 100)}": _percentile(values, p) for p in _PERCENTILES
            },
        }
    return out


def _content_hash(settlements: list[FundingSettlement]) -> str:
    hasher = hashlib.sha256()
    for s in sorted(settlements, key=lambda s: (s.exchange, s.native_market_id, s.settlement_at)):
        hasher.update(
            f"{s.exchange}|{s.native_market_id}|{s.settlement_at.isoformat()}|{s.funding_rate!r}\n".encode()
        )
    return "sha256:" + hasher.hexdigest()


def fetch_settlements(
    client: FundingClient,
    exchange: str,
    native_market_ids: list[str],
    *,
    window_start: datetime,
    window_end: datetime,
) -> tuple[list[FundingSettlement], dict[str, Any]]:
    """Pull funding settlements for one venue's instruments over ``[window_start,
    window_end)``. Returns (settlements, coverage). An instrument with no resolvable
    unified symbol or no settlements is recorded, not silently dropped."""
    since_ms = int(window_start.timestamp() * 1000)
    until_ms = int(window_end.timestamp() * 1000)
    settlements: list[FundingSettlement] = []
    covered: set[str] = set()
    unresolved: list[str] = []
    for native in native_market_ids:
        symbol = client.unified_symbol(native)
        if symbol is None:
            unresolved.append(native)
            continue
        for ts_ms, rate in client.funding_history(symbol, since_ms, until_ms):
            if ts_ms < since_ms or ts_ms >= until_ms:
                continue
            settlements.append(
                FundingSettlement(
                    exchange=exchange,
                    native_market_id=native,
                    settlement_at=datetime.fromtimestamp(ts_ms / 1000, tz=UTC),
                    funding_rate=float(rate),
                )
            )
            covered.add(native)
    coverage = {
        "requested_instruments": len(native_market_ids),
        "covered_instruments": len(covered),
        "unresolved_symbols": len(unresolved),
        "no_settlement_instruments": sorted(set(native_market_ids) - covered - set(unresolved)),
    }
    return settlements, coverage


def build_snapshot(
    settlements: list[FundingSettlement],
    *,
    window_start: datetime,
    window_end: datetime,
    coverage_by_venue: dict[str, dict[str, Any]],
    source: dict[str, Any],
) -> dict[str, Any]:
    """Assemble the durable funding snapshot artifact (percentiles + coverage + source +
    content hash). P95 is the primary proxy; P99 is the sensitivity input."""
    return {
        "funding_snapshot_version": FUNDING_SNAPSHOT_VERSION,
        "source": source,
        "window": {"start": window_start.isoformat(), "end": window_end.isoformat()},
        "conservative_rule": "P95 of max(funding_rate,0) per venue (long); P99 sensitivity",
        "coverage": coverage_by_venue,
        "percentiles": funding_percentiles(settlements),
        "content_hash": _content_hash(settlements),
        "total_settlements": len(settlements),
    }


# --- CCXT edge (network; not unit-tested) ------------------------------------------


class CcxtFundingClient:
    """Wraps a CCXT exchange for one venue: maps native ids to unified linear symbols and
    pages funding history. Constructed via :meth:`create`."""

    def __init__(self, exchange: Any) -> None:
        self._ex = exchange
        self._ex.load_markets()

    @classmethod
    def create(cls, exchange_id: str) -> CcxtFundingClient:
        import ccxt

        klass = getattr(ccxt, exchange_id)
        return cls(klass({"enableRateLimit": True, "options": {"defaultType": "swap"}}))

    def unified_symbol(self, native_market_id: str) -> str | None:
        market = self._ex.markets_by_id.get(native_market_id)
        if not market:
            return None
        candidates = market if isinstance(market, list) else [market]
        for m in candidates:
            if m.get("swap") and m.get("linear") and m.get("settle") == "USDT":
                return str(m["symbol"])
        return None

    def funding_history(
        self, unified_symbol: str, since_ms: int, until_ms: int
    ) -> list[tuple[int, float]]:
        out: list[tuple[int, float]] = []
        cursor = since_ms
        while cursor < until_ms:
            batch = self._ex.fetch_funding_rate_history(unified_symbol, since=cursor, limit=200)
            if not batch:
                break
            for row in batch:
                ts = int(row["timestamp"])
                rate = row.get("fundingRate")
                if rate is not None:
                    out.append((ts, float(rate)))
            last = int(batch[-1]["timestamp"])
            if last <= cursor:
                break
            cursor = last + 1
            if last >= until_ms:
                break
        return out


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--universe-json", type=Path, required=True, help="{venue: [native_ids]}")
    parser.add_argument("--start-day", type=date.fromisoformat, required=True)
    parser.add_argument("--end-day", type=date.fromisoformat, required=True, help="exclusive")
    parser.add_argument("--out", type=Path, required=True, help="durable snapshot JSON")
    parser.add_argument(
        "--settlements-out", type=Path, default=None, help="optional raw settlements JSON"
    )
    return parser


def main() -> None:
    import ccxt

    args: Any = build_parser().parse_args()
    universe: dict[str, list[str]] = json.loads(args.universe_json.read_text())
    window_start = datetime(
        args.start_day.year, args.start_day.month, args.start_day.day, tzinfo=UTC
    )
    window_end = datetime(args.end_day.year, args.end_day.month, args.end_day.day, tzinfo=UTC)

    all_settlements: list[FundingSettlement] = []
    coverage_by_venue: dict[str, dict[str, Any]] = {}
    exchange_versions: dict[str, str] = {}
    for venue, natives in sorted(universe.items()):
        client = CcxtFundingClient.create(venue)
        exchange_versions[venue] = str(getattr(client._ex, "version", "unknown"))
        settlements, coverage = fetch_settlements(
            client, venue, natives, window_start=window_start, window_end=window_end
        )
        all_settlements.extend(settlements)
        coverage_by_venue[venue] = coverage
        sys.stderr.write(f"{venue}: {len(settlements)} settlements, coverage={coverage}\n")

    source = {
        "method": "ccxt.fetchFundingRateHistory",
        "ccxt_version": ccxt.__version__,
        "exchange_api_versions": exchange_versions,
        "universe_source": str(args.universe_json),
    }
    snapshot = build_snapshot(
        all_settlements,
        window_start=window_start,
        window_end=window_end,
        coverage_by_venue=coverage_by_venue,
        source=source,
    )
    args.out.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n")
    if args.settlements_out is not None:
        args.settlements_out.write_text(
            json.dumps(
                [
                    {
                        "exchange": s.exchange,
                        "native_market_id": s.native_market_id,
                        "settlement_at": s.settlement_at.isoformat(),
                        "funding_rate": s.funding_rate,
                    }
                    for s in all_settlements
                ]
            )
        )
    sys.stdout.write(f"{args.out}\n")


if __name__ == "__main__":
    main()
