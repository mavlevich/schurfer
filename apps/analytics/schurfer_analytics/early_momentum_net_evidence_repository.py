"""SQLAlchemy query layer for the early_momentum_v4 net-evidence report.

Everything runs inside a single REPEATABLE READ, read-only transaction
(`postgresql_readonly=True`) so the whole dataset -- episodes, linked
trades, orphan cross-check trades, exit-liquidity observations, and the
`SELECT now()` snapshot timestamp itself -- comes from one consistent
Postgres snapshot, never a mix of reads taken at different instants. The
snapshot timestamp is captured as the transaction's own FIRST statement (the
same NTP-drift fix already applied to early_momentum.py's scanner:
`evaluated_at`/`db_now` must come from the database's own clock, not the
report process's local clock, even indirectly).

`app.early_momentum_episodes` is the authoritative cohort-membership source
(see early_momentum_net_evidence.py's module docstring); the orphan
cross-check query below exists only to catch a trade that claims to be
early_momentum_v4 via `setup_context->>'strategy'` but was never reachable
through an episode at all -- itself an integrity violation, never a second
membership path.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any

from schurfer_journal.models import (
    EarlyMomentumEpisode,
    Strategy,
    Trade,
    TradeExitLiquidityObservation,
)
from sqlalchemy import Float, cast, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .early_momentum_net_evidence import (
    COHORT_MATURITY_BUFFER_SECONDS,
    EXPECTED_SETUP_CONTEXT_STRATEGY,
    STRATEGY_NAME,
    STRATEGY_VERSION,
    EpisodeRow,
    ExitLiquidityRow,
    LegacyContextRow,
    RawDataset,
    TradeRow,
)
from .outcome_repository import async_database_url

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.engine import RowMapping
    from sqlalchemy.ext.asyncio import AsyncConnection
    from sqlalchemy.sql import Select


def _episodes_statement(*, cohort_start: datetime, cohort_end: datetime) -> Select[Any]:
    episodes = EarlyMomentumEpisode.__table__
    strategies = Strategy.__table__
    return (
        select(
            episodes.c.episode_id,
            episodes.c.strategy_id,
            episodes.c.contract_sha256,
            episodes.c.exchange,
            episodes.c.native_market_id,
            episodes.c.execution_symbol,
            episodes.c.source_exchange,
            episodes.c.source_native_id,
            episodes.c.execution_identity_key,
            episodes.c.source_identity_key,
            episodes.c.cluster_key,
            episodes.c.armed_at,
            episodes.c.expires_at,
            episodes.c.status,
            episodes.c.terminal_reason,
            episodes.c.claimed_at,
            episodes.c.claim_expires_at,
            episodes.c.claim_attempts,
        )
        .select_from(episodes.join(strategies, strategies.c.id == episodes.c.strategy_id))
        .where(
            strategies.c.name == STRATEGY_NAME,
            strategies.c.version == STRATEGY_VERSION,
            episodes.c.armed_at >= cohort_start,
            episodes.c.armed_at < cohort_end,
        )
        .order_by(episodes.c.armed_at)
    )


def _json_float(column: Any, *path: str) -> Any:
    return cast(func.jsonb_extract_path_text(column, *path), Float)


def _trade_columns(trades: Any) -> list[Any]:
    paper = func.jsonb_extract_path_text(trades.c.setup_context, "paper") == "true"
    strategy_text = func.jsonb_extract_path_text(trades.c.setup_context, "strategy")
    # entry_vwap_impact_bps is the REAL entry-side impact at the actual
    # traded notional (early_momentum.py's own _SIZE_USD) -- setup_context.
    # market_quality.ask_impact_bps is a different number, measured at the
    # market-quality gate's larger safety-margin depth_target, never the
    # real trade size (colleague review: capacity evidence must read the
    # former, not the latter, see early_momentum.py's own setup_context
    # comment: "quality/market_quality reflects the gate's safety-margined
    # depth_target notional; these two fields [entry_vwap_impact_bps/
    # entry_vwap_filled_usd] are the actual entry-side reading at the real
    # trade size").
    ask_impact = _json_float(trades.c.setup_context, "entry_vwap_impact_bps")
    bid_impact = _json_float(trades.c.setup_context, "market_quality", "bid_impact_bps")
    return [
        trades.c.id.label("trade_id"),
        trades.c.episode_id,
        trades.c.strategy_id,
        trades.c.symbol,
        trades.c.exchange,
        trades.c.side,
        trades.c.size_usd,
        trades.c.leverage,
        trades.c.entry_price,
        trades.c.entry_at,
        trades.c.exit_price,
        trades.c.exit_at,
        trades.c.fees_usd,
        trades.c.funding_usd,
        trades.c.slippage_usd,
        trades.c.gross_pnl_usd,
        trades.c.gross_pnl_pct,
        trades.c.net_pnl_usd,
        trades.c.net_pnl_pct,
        trades.c.accounting_version,
        trades.c.accounting_status,
        trades.c.accounting_error,
        trades.c.status,
        trades.c.notes,
        trades.c.entry_idempotency_key,
        paper.label("is_paper"),
        strategy_text.label("setup_context_strategy"),
        ask_impact.label("entry_ask_impact_bps"),
        bid_impact.label("entry_bid_impact_bps"),
    ]


def _trades_by_episode_statement(episode_ids: tuple[str, ...]) -> Select[Any]:
    trades = Trade.__table__
    return select(*_trade_columns(trades)).where(trades.c.episode_id.in_(episode_ids))


def _orphan_trades_statement(*, cohort_start: datetime, cohort_end: datetime) -> Select[Any]:
    """v4-labeled trades with no episode_id at all. An episode armed just
    before cohort_end can still open its trade after cohort_end (episode
    TTL/trigger delay) -- if the episode_id link were ever lost, that
    orphan's entry_at would fall outside a naive `< cohort_end` bound and
    escape detection entirely (colleague review). The upper bound is
    widened by the same COHORT_MATURITY_BUFFER_SECONDS the report already
    requires cohort_end to be older than the DB clock by, which safely
    covers episode TTL + max_hold + operational buffer."""
    trades = Trade.__table__
    strategy_text = func.jsonb_extract_path_text(trades.c.setup_context, "strategy")
    orphan_upper_bound = cohort_end + timedelta(seconds=COHORT_MATURITY_BUFFER_SECONDS)
    return select(*_trade_columns(trades)).where(
        trades.c.episode_id.is_(None),
        strategy_text == EXPECTED_SETUP_CONTEXT_STRATEGY,
        trades.c.entry_at >= cohort_start,
        trades.c.entry_at < orphan_upper_bound,
    )


_LEGACY_STRATEGY_LABELS = (
    f"{STRATEGY_NAME}_v1",
    f"{STRATEGY_NAME}_v2",
    f"{STRATEGY_NAME}_v3",
)


def _legacy_context_statement(*, cohort_end: datetime) -> Select[Any]:
    trades = Trade.__table__
    strategy_text = func.jsonb_extract_path_text(trades.c.setup_context, "strategy")
    accounting_complete = trades.c.accounting_status == "complete"
    return (
        select(
            strategy_text.label("setup_context_strategy"),
            func.count().label("total_trades"),
            func.count().filter(trades.c.status == "closed").label("closed_trades"),
            func.count().filter(trades.c.status == "cancelled").label("cancelled_trades"),
            func.count().filter(trades.c.status == "open").label("open_trades"),
            func.count()
            .filter(trades.c.status == "closed", accounting_complete)
            .label("complete_accounting_closed_trades"),
            func.sum(trades.c.net_pnl_usd)
            .filter(trades.c.status == "closed", accounting_complete)
            .label("total_net_pnl_usd_complete_only"),
        )
        .where(
            strategy_text.in_(_LEGACY_STRATEGY_LABELS),
            trades.c.entry_at < cohort_end,
        )
        .group_by(strategy_text)
        .order_by(strategy_text)
    )


def _exit_liquidity_statement(trade_ids: tuple[int, ...]) -> Select[Any]:
    observations = TradeExitLiquidityObservation.__table__
    return select(
        observations.c.trade_id,
        observations.c.requested_notional_usd,
        observations.c.filled_notional_usd,
        observations.c.spread_bps,
        observations.c.ask_impact_bps,
        observations.c.bid_impact_bps,
        observations.c.latency_ms,
        observations.c.status,
        observations.c.error,
    ).where(observations.c.trade_id.in_(trade_ids))


def _episode_row(row: RowMapping) -> EpisodeRow:
    return EpisodeRow(
        episode_id=str(row["episode_id"]),
        strategy_id=int(row["strategy_id"]),
        contract_sha256=bytes(row["contract_sha256"]),
        exchange=str(row["exchange"]),
        native_market_id=str(row["native_market_id"]),
        execution_symbol=row["execution_symbol"],
        source_exchange=str(row["source_exchange"]),
        source_native_id=str(row["source_native_id"]),
        execution_identity_key=str(row["execution_identity_key"]),
        source_identity_key=str(row["source_identity_key"]),
        cluster_key=str(row["cluster_key"]),
        armed_at=row["armed_at"],
        expires_at=row["expires_at"],
        status=str(row["status"]),
        terminal_reason=row["terminal_reason"],
        claimed_at=row["claimed_at"],
        claim_expires_at=row["claim_expires_at"],
        claim_attempts=int(row["claim_attempts"]),
    )


def _trade_row(row: RowMapping) -> TradeRow:
    return TradeRow(
        trade_id=int(row["trade_id"]),
        episode_id=str(row["episode_id"]) if row["episode_id"] is not None else None,
        strategy_id=int(row["strategy_id"]),
        symbol=str(row["symbol"]),
        exchange=str(row["exchange"]),
        side=str(row["side"]),
        size_usd=float(row["size_usd"]),
        leverage=float(row["leverage"]),
        entry_price=float(row["entry_price"]),
        entry_at=row["entry_at"],
        exit_price=float(row["exit_price"]) if row["exit_price"] is not None else None,
        exit_at=row["exit_at"],
        fees_usd=float(row["fees_usd"]),
        funding_usd=float(row["funding_usd"]),
        slippage_usd=float(row["slippage_usd"]) if row["slippage_usd"] is not None else None,
        gross_pnl_usd=float(row["gross_pnl_usd"]) if row["gross_pnl_usd"] is not None else None,
        gross_pnl_pct=float(row["gross_pnl_pct"]) if row["gross_pnl_pct"] is not None else None,
        net_pnl_usd=float(row["net_pnl_usd"]) if row["net_pnl_usd"] is not None else None,
        net_pnl_pct=float(row["net_pnl_pct"]) if row["net_pnl_pct"] is not None else None,
        accounting_version=str(row["accounting_version"]),
        accounting_status=str(row["accounting_status"]),
        accounting_error=row["accounting_error"],
        status=str(row["status"]),
        notes=row["notes"],
        entry_idempotency_key=row["entry_idempotency_key"],
        is_paper=bool(row["is_paper"]),
        setup_context_strategy=row["setup_context_strategy"],
        entry_ask_impact_bps=(
            float(row["entry_ask_impact_bps"]) if row["entry_ask_impact_bps"] is not None else None
        ),
        entry_bid_impact_bps=(
            float(row["entry_bid_impact_bps"]) if row["entry_bid_impact_bps"] is not None else None
        ),
    )


def _exit_liquidity_row(row: RowMapping) -> ExitLiquidityRow:
    return ExitLiquidityRow(
        trade_id=int(row["trade_id"]),
        requested_notional_usd=float(row["requested_notional_usd"]),
        filled_notional_usd=(
            float(row["filled_notional_usd"]) if row["filled_notional_usd"] is not None else None
        ),
        spread_bps=float(row["spread_bps"]) if row["spread_bps"] is not None else None,
        ask_impact_bps=float(row["ask_impact_bps"]) if row["ask_impact_bps"] is not None else None,
        bid_impact_bps=float(row["bid_impact_bps"]) if row["bid_impact_bps"] is not None else None,
        latency_ms=int(row["latency_ms"]),
        status=str(row["status"]),
        error=row["error"],
    )


def _legacy_context_row(row: RowMapping) -> LegacyContextRow:
    return LegacyContextRow(
        setup_context_strategy=str(row["setup_context_strategy"]),
        total_trades=int(row["total_trades"]),
        closed_trades=int(row["closed_trades"]),
        cancelled_trades=int(row["cancelled_trades"]),
        open_trades=int(row["open_trades"]),
        complete_accounting_closed_trades=int(row["complete_accounting_closed_trades"]),
        total_net_pnl_usd_complete_only=(
            float(row["total_net_pnl_usd_complete_only"])
            if row["total_net_pnl_usd_complete_only"] is not None
            else None
        ),
    )


class EarlyMomentumNetEvidenceRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @classmethod
    def from_url(cls, db_url: str) -> EarlyMomentumNetEvidenceRepository:
        return cls(
            create_async_engine(
                async_database_url(db_url),
                pool_pre_ping=True,
                pool_size=1,
                max_overflow=0,
            )
        )

    async def fetch(self, *, cohort_start: datetime, cohort_end: datetime) -> RawDataset:
        async with self._engine.connect() as raw_connection:
            connection: AsyncConnection = await raw_connection.execution_options(
                isolation_level="REPEATABLE READ",
                postgresql_readonly=True,
            )
            async with connection.begin():
                db_now = (await connection.execute(select(func.now()))).scalar_one()

                episode_rows = (
                    (
                        await connection.execute(
                            _episodes_statement(cohort_start=cohort_start, cohort_end=cohort_end)
                        )
                    )
                    .mappings()
                    .all()
                )
                episodes = tuple(_episode_row(row) for row in episode_rows)
                episode_ids = tuple(e.episode_id for e in episodes)

                linked_trade_rows = (
                    (await connection.execute(_trades_by_episode_statement(episode_ids)))
                    .mappings()
                    .all()
                    if episode_ids
                    else []
                )
                orphan_trade_rows = (
                    (
                        await connection.execute(
                            _orphan_trades_statement(
                                cohort_start=cohort_start, cohort_end=cohort_end
                            )
                        )
                    )
                    .mappings()
                    .all()
                )
                trades = tuple(_trade_row(row) for row in (*linked_trade_rows, *orphan_trade_rows))
                trade_ids = tuple(t.trade_id for t in trades)

                exit_liquidity_rows = (
                    (await connection.execute(_exit_liquidity_statement(trade_ids)))
                    .mappings()
                    .all()
                    if trade_ids
                    else []
                )
                exit_liquidity = tuple(_exit_liquidity_row(row) for row in exit_liquidity_rows)

        return RawDataset(
            cohort_start=cohort_start,
            cohort_end=cohort_end,
            db_snapshot_at=db_now,
            episodes=episodes,
            trades=trades,
            exit_liquidity=exit_liquidity,
        )

    async def fetch_legacy_context(self, *, cohort_end: datetime) -> tuple[LegacyContextRow, ...]:
        """v1/v2/v3 descriptive context only -- never joined with or added
        to the v4 formal dataset above (see LegacyContextRow's docstring)."""
        async with self._engine.connect() as raw_connection:
            connection: AsyncConnection = await raw_connection.execution_options(
                isolation_level="REPEATABLE READ",
                postgresql_readonly=True,
            )
            async with connection.begin():
                rows = (
                    (await connection.execute(_legacy_context_statement(cohort_end=cohort_end)))
                    .mappings()
                    .all()
                )
        return tuple(_legacy_context_row(row) for row in rows)

    async def close(self) -> None:
        await self._engine.dispose()


__all__ = ["EarlyMomentumNetEvidenceRepository"]
