"""Read-only query layer for `exit_liquidity_net_economics_v1`.

Deliberately a separate query/row shape from
`exit_liquidity_calibration_repository.py`: that report exists to compare
bps, this one exists to compare dollars, and bolting dollar-accounting
fields onto the bps row would couple two reports that should be free to
change independently (see the module docstring in
`exit_liquidity_net_economics.py` and `docs/research/exit-liquidity-
adjusted-net-economics-v1.md`).

Reuses `exit_liquidity_calibration_report.py`'s own cohort-start and
skew-tolerance constants rather than re-declaring them, since both reports
share the same underlying exit-liquidity-observation capture mechanism and
must never drift out of sync on what counts as "in cohort" or "stale".
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from schurfer_journal.models import Strategy, Trade, TradeExitLiquidityObservation
from sqlalchemy import func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .exit_liquidity_net_economics import ALLOWED_STRATEGY_IDENTITIES, NetEconomicsRow
from .outcome_repository import async_database_url

if TYPE_CHECKING:
    from sqlalchemy.sql import Select

    from .exit_liquidity_calibration_report import ExitLiquidityFilters


def net_economics_statement(filters: ExitLiquidityFilters) -> Select[Any]:
    observation = TradeExitLiquidityObservation
    # ask_impact_bps/ask_vwap hardcoded, not side-derived: a short's exit
    # leg is the ask side (see liquidity.book_side_for), and this
    # statement's own WHERE clause is already hardcoded to `Trade.side ==
    # "short"` -- extending to longs needs this to become side-aware (bid)
    # at the same time, exactly like exit_liquidity_calibration_
    # repository.py's own modeled_exit_bps.
    modeled_exit_bps = func.jsonb_extract_path_text(
        Trade.setup_context, "market_quality", "ask_impact_bps"
    ).label("modeled_exit_bps")
    return (
        select(
            Trade.id.label("trade_id"),
            Trade.episode_id,
            Strategy.name.label("strategy_name"),
            Strategy.version.label("strategy_version"),
            Trade.symbol,
            Trade.exchange,
            Trade.side,
            Trade.entry_at,
            Trade.exit_at,
            Trade.notes.label("exit_reason"),
            Trade.size_usd,
            Trade.leverage,
            Trade.entry_price,
            Trade.exit_price,
            Trade.gross_pnl_usd,
            Trade.net_pnl_usd.label("recorded_net_pnl_usd"),
            Trade.fees_usd,
            Trade.funding_usd,
            Trade.entry_slippage_bps,
            modeled_exit_bps,
            Trade.accounting_version,
            Trade.accounting_status,
            Trade.accounting_error,
            observation.id.label("observation_id"),
            observation.observed_at,
            observation.exchange.label("observation_exchange"),
            observation.symbol.label("observation_symbol"),
            observation.status.label("observation_status"),
            observation.requested_notional_usd,
            observation.filled_notional_usd,
            observation.mid.label("observed_mid"),
            observation.spread_bps.label("observed_spread_bps"),
            observation.ask_impact_bps.label("observed_exit_bps"),
            observation.ask_vwap.label("observed_ask_vwap"),
            observation.latency_ms,
            observation.error,
        )
        .select_from(Trade.__table__.join(Strategy, Strategy.id == Trade.strategy_id))
        .outerjoin(observation, observation.trade_id == Trade.id)
        .where(
            func.jsonb_extract_path_text(Trade.setup_context, "paper") == "true",
            Trade.side == "short",
            Trade.status == "closed",
            Trade.exit_at.is_not(None),
            Trade.exit_at >= filters.since,
            Trade.exit_at < filters.until,
            # Frozen contract scope: pump_short v1 ONLY -- see
            # ALLOWED_STRATEGY_IDENTITIES's own docstring in exit_liquidity_
            # net_economics.py. Without this, a distinct registered variant
            # like ("pump_short", "1_market_quality"), any historical
            # early_momentum short, or a future short strategy would all
            # silently blend into one aggregate verdict (colleague review,
            # 2026-08-25).
            tuple_(Strategy.name, Strategy.version).in_(ALLOWED_STRATEGY_IDENTITIES),
        )
        .order_by(Trade.exit_at, Trade.id)
    )


def _decimal_or_none(value: Any) -> float | None:
    return float(value) if value is not None else None


def map_net_economics_row(row: dict[str, Any]) -> NetEconomicsRow:
    return NetEconomicsRow(
        trade_id=int(row["trade_id"]),
        episode_id=str(row["episode_id"]) if row["episode_id"] is not None else None,
        strategy_name=str(row["strategy_name"]),
        strategy_version=str(row["strategy_version"]),
        symbol=str(row["symbol"]),
        exchange=str(row["exchange"]),
        side=str(row["side"]),
        entry_at=row["entry_at"],
        exit_at=row["exit_at"],
        exit_reason=row["exit_reason"],
        size_usd=float(row["size_usd"]),
        leverage=float(row["leverage"]),
        entry_price=float(row["entry_price"]),
        exit_price=_decimal_or_none(row["exit_price"]),
        recorded_gross_pnl_usd=_decimal_or_none(row["gross_pnl_usd"]),
        recorded_net_pnl_usd=_decimal_or_none(row["recorded_net_pnl_usd"]),
        fees_usd=float(row["fees_usd"]),
        funding_usd=float(row["funding_usd"]),
        entry_slippage_bps=_decimal_or_none(row["entry_slippage_bps"]),
        modeled_exit_bps=(
            float(row["modeled_exit_bps"]) if row["modeled_exit_bps"] is not None else None
        ),
        accounting_version=str(row["accounting_version"]),
        accounting_status=str(row["accounting_status"]),
        accounting_error=row["accounting_error"],
        observation_id=(int(row["observation_id"]) if row["observation_id"] is not None else None),
        observed_at=row["observed_at"],
        observation_exchange=row["observation_exchange"],
        observation_symbol=row["observation_symbol"],
        observation_status=row["observation_status"],
        requested_notional_usd=_decimal_or_none(row["requested_notional_usd"]),
        filled_notional_usd=_decimal_or_none(row["filled_notional_usd"]),
        observed_mid=_decimal_or_none(row["observed_mid"]),
        observed_spread_bps=_decimal_or_none(row["observed_spread_bps"]),
        observed_exit_bps=_decimal_or_none(row["observed_exit_bps"]),
        observed_ask_vwap=_decimal_or_none(row["observed_ask_vwap"]),
        latency_ms=int(row["latency_ms"]) if row["latency_ms"] is not None else None,
        error=row["error"],
    )


class ExitLiquidityNetEconomicsRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @classmethod
    def from_url(cls, db_url: str) -> ExitLiquidityNetEconomicsRepository:
        return cls(
            create_async_engine(
                async_database_url(db_url),
                pool_pre_ping=True,
                pool_size=1,
                max_overflow=0,
            )
        )

    async def load(self, filters: ExitLiquidityFilters) -> tuple[NetEconomicsRow, ...]:
        async with self._engine.connect() as raw_connection:
            connection = await raw_connection.execution_options(
                isolation_level="REPEATABLE READ",
                postgresql_readonly=True,
            )
            async with connection.begin():
                result = await connection.execute(net_economics_statement(filters))
                return tuple(map_net_economics_row(dict(row)) for row in result.mappings())

    async def close(self) -> None:
        await self._engine.dispose()


__all__ = [
    "ExitLiquidityNetEconomicsRepository",
    "map_net_economics_row",
    "net_economics_statement",
]
