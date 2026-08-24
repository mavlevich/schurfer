"""Read-only query layer for paper exit-liquidity calibration.

`modeled_exit_bps` is read from `Trade.setup_context->market_quality->
ask_impact_bps`, never from `Trade.exit_slippage_bps` -- a colleague review
(2026-08-24) of this report's own economics finding caught that the two are
NOT the same value and mixing them silently corrupts the comparison this
report exists to make. Confirmed directly against production data:
`Trade.exit_slippage_bps` genuinely equals `setup_context`'s decision-time
`ask_impact_bps` for OLDER paper trades (legacy accounting, closed before
paper.py's exit-time VWAP capture existed), but for NEWER trades where that
capture succeeded, `close_trade()` deliberately OVERWRITES the column to
`0.0` (see journal.py's own `close_trade` docstring: "never a second charge
on top of a price that already paid it" -- the captured VWAP fill price
already embeds the real cost, so the bps-adjustment column is zeroed to
avoid double-counting it in `calculate_performance`). That 0.0 means
"no additional charge on top of this fill", not "the model predicted zero
exit cost" -- reading it as the latter for this report's own comparison
falsely inflates the delta for exactly the rows where a real close-time
capture (the most trustworthy case) happened to succeed. `setup_context`'s
`market_quality.ask_impact_bps` is the one source that holds the true
decision-time model on every row regardless of which accounting path
closed it, since it is written once at open time and never touched again.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from schurfer_journal.models import Trade, TradeExitLiquidityObservation
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .exit_liquidity_calibration_report import ExitLiquidityFilters, ExitLiquidityRow
from .outcome_repository import async_database_url

if TYPE_CHECKING:
    from sqlalchemy.sql import Select


def exit_liquidity_statement(filters: ExitLiquidityFilters) -> Select[Any]:
    observation = TradeExitLiquidityObservation
    # 'ask_impact_bps' is hardcoded, not side-derived, because this
    # statement's own WHERE clause below is already hardcoded to
    # `Trade.side == "short"` -- a short's exit leg is the ask side (see
    # liquidity.book_side_for). If this report is ever extended to include
    # longs, this must become side-aware (bid for long) at the same time.
    modeled_exit_bps = func.jsonb_extract_path_text(
        Trade.setup_context, "market_quality", "ask_impact_bps"
    ).label("modeled_exit_bps")
    return (
        select(
            Trade.id.label("trade_id"),
            Trade.symbol,
            Trade.exchange,
            Trade.size_usd,
            Trade.entry_at,
            Trade.exit_at,
            Trade.notes.label("exit_reason"),
            modeled_exit_bps,
            observation.id.label("observation_id"),
            observation.observed_at,
            observation.exchange.label("observation_exchange"),
            observation.symbol.label("observation_symbol"),
            observation.status.label("observation_status"),
            observation.requested_notional_usd,
            observation.filled_notional_usd,
            observation.spread_bps.label("observed_spread_bps"),
            observation.ask_impact_bps.label("observed_exit_bps"),
            observation.latency_ms,
            observation.error,
        )
        .outerjoin(observation, observation.trade_id == Trade.id)
        .where(
            func.jsonb_extract_path_text(Trade.setup_context, "paper") == "true",
            Trade.side == "short",
            Trade.status == "closed",
            Trade.exit_at.is_not(None),
            Trade.exit_at >= filters.since,
            Trade.exit_at < filters.until,
        )
        .order_by(Trade.exit_at, Trade.id)
    )


def _parse_modeled_exit_bps(raw: Any) -> float | None:
    """`modeled_exit_bps` comes from `jsonb_extract_path_text`, i.e. raw JSON
    text pulled out of `setup_context` -- not a schema-validated numeric
    column like the other fields below. A malformed or non-numeric historical
    `ask_impact_bps` (bad setup_context, stray string) must not crash the
    whole report; it falls back to None, which `_finite_nonnegative` and the
    existing `missing_or_invalid_modeled_impact` exclusion already handle
    (colleague review, 2026-08-24)."""
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def map_exit_liquidity_row(row: dict[str, Any]) -> ExitLiquidityRow:
    return ExitLiquidityRow(
        trade_id=int(row["trade_id"]),
        symbol=str(row["symbol"]),
        exchange=str(row["exchange"]),
        size_usd=float(row["size_usd"]),
        entry_at=row["entry_at"],
        exit_at=row["exit_at"],
        exit_reason=row["exit_reason"],
        modeled_exit_bps=_parse_modeled_exit_bps(row["modeled_exit_bps"]),
        observation_id=(int(row["observation_id"]) if row["observation_id"] is not None else None),
        observed_at=row["observed_at"],
        observation_exchange=row["observation_exchange"],
        observation_symbol=row["observation_symbol"],
        observation_status=row["observation_status"],
        requested_notional_usd=(
            float(row["requested_notional_usd"])
            if row["requested_notional_usd"] is not None
            else None
        ),
        filled_notional_usd=(
            float(row["filled_notional_usd"]) if row["filled_notional_usd"] is not None else None
        ),
        observed_spread_bps=(
            float(row["observed_spread_bps"]) if row["observed_spread_bps"] is not None else None
        ),
        observed_exit_bps=(
            float(row["observed_exit_bps"]) if row["observed_exit_bps"] is not None else None
        ),
        latency_ms=int(row["latency_ms"]) if row["latency_ms"] is not None else None,
        error=row["error"],
    )


class ExitLiquidityCalibrationRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @classmethod
    def from_url(cls, db_url: str) -> ExitLiquidityCalibrationRepository:
        return cls(
            create_async_engine(
                async_database_url(db_url),
                pool_pre_ping=True,
                pool_size=1,
                max_overflow=0,
            )
        )

    async def load(self, filters: ExitLiquidityFilters) -> tuple[ExitLiquidityRow, ...]:
        async with self._engine.connect() as raw_connection:
            connection = await raw_connection.execution_options(
                isolation_level="REPEATABLE READ",
                postgresql_readonly=True,
            )
            async with connection.begin():
                result = await connection.execute(exit_liquidity_statement(filters))
                return tuple(map_exit_liquidity_row(dict(row)) for row in result.mappings())

    async def close(self) -> None:
        await self._engine.dispose()
