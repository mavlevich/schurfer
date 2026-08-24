"""Tests for execution_intent.py: TradingMode/resolve_mode's safety ladder,
ExecutionIntent/ExecutionResult validation, PaperBroker's pass-through
dispatch to paper.py, and ShadowBroker's decision-evidence writes. LiveBroker
does not exist in this build -- see the module docstring for why."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from schurfer_execution.config import Config
from schurfer_execution.execution_intent import (
    STRATEGY_EARLY_MOMENTUM,
    STRATEGY_LIQUIDATION_CASCADE,
    STRATEGY_PUMP_SHORT,
    DisabledBroker,
    EpisodeClaim,
    ExecutionIntent,
    ExecutionResult,
    ExecutionStatus,
    PaperBroker,
    ShadowBroker,
    StrategyIdentity,
    TradingMode,
    build_broker,
    parse_mode,
    resolve_mode,
)
from schurfer_execution.journal import OpenTradeOutcome
from schurfer_execution.symbols import ExecutionInstrument


def _cfg(
    *,
    dry_run: bool = False,
    auto_trade: bool = False,
    pump_short_mode: str | None = None,
    early_momentum_mode: str | None = None,
    liquidation_cascade_mode: str | None = None,
    db_url: str | None = "postgresql://x",
) -> Config:
    cfg = object.__new__(Config)
    cfg.dry_run = dry_run
    cfg.auto_trade = auto_trade
    cfg.pump_short_mode = pump_short_mode
    cfg.early_momentum_mode = early_momentum_mode
    cfg.liquidation_cascade_mode = liquidation_cascade_mode
    cfg.db_url = db_url
    return cfg


def _instrument() -> ExecutionInstrument:
    return ExecutionInstrument(
        exchange="bybit",
        symbol="BEAT/USDT:USDT",
        native_market_id="BEATUSDT",
        base="BEAT",
        quote="USDT",
        settle="USDT",
        market_type="swap",
    )


def _intent(**overrides: Any) -> ExecutionIntent:
    fields: dict[str, Any] = {
        "strategy": StrategyIdentity(name="pump_short", version="1"),
        "instrument": _instrument(),
        "side": "short",
        "size_usd": 50.0,
        "leverage": 3,
        "score": 8,
        "setup_context": {"pump_pct": 45.0},
        "idempotency_key": "decision-1",
        "price": 1.5,
    }
    fields.update(overrides)
    return ExecutionIntent(**fields)


# ---- resolve_mode: the safety ladder ----


@pytest.mark.parametrize(
    "strategy",
    [STRATEGY_PUMP_SHORT, STRATEGY_EARLY_MOMENTUM, STRATEGY_LIQUIDATION_CASCADE],
)
def test_resolve_mode_auto_trade_with_no_override_is_paper_not_live_micro(
    strategy: str,
) -> None:
    """The exact regression the colleague review's blocker #1 was about:
    AUTO_TRADE=true alone must never promote an unset per-strategy override
    into the AUTO_TRADE-derived LIVE_MICRO ceiling."""
    cfg = _cfg(auto_trade=True)
    assert resolve_mode(cfg, strategy) == TradingMode.PAPER


@pytest.mark.parametrize(
    "strategy",
    [STRATEGY_PUMP_SHORT, STRATEGY_EARLY_MOMENTUM, STRATEGY_LIQUIDATION_CASCADE],
)
def test_resolve_mode_dry_run_with_no_override_is_paper(strategy: str) -> None:
    cfg = _cfg(dry_run=True)
    assert resolve_mode(cfg, strategy) == TradingMode.PAPER


@pytest.mark.parametrize(
    "strategy",
    [STRATEGY_PUMP_SHORT, STRATEGY_EARLY_MOMENTUM, STRATEGY_LIQUIDATION_CASCADE],
)
def test_resolve_mode_neither_flag_with_no_override_is_disabled(strategy: str) -> None:
    cfg = _cfg(dry_run=False, auto_trade=False)
    assert resolve_mode(cfg, strategy) == TradingMode.DISABLED


def test_resolve_mode_explicit_paper_override_under_dry_run_is_accepted() -> None:
    cfg = _cfg(dry_run=True, pump_short_mode="paper")
    assert resolve_mode(cfg, STRATEGY_PUMP_SHORT) == TradingMode.PAPER


def test_resolve_mode_explicit_disabled_override_is_a_legal_demotion() -> None:
    """DISABLED is always <= any ceiling -- an operator can turn one
    strategy off without touching the global dry_run/auto_trade switches
    that gate every other strategy's task too."""
    cfg = _cfg(auto_trade=True, pump_short_mode="disabled")
    assert resolve_mode(cfg, STRATEGY_PUMP_SHORT) == TradingMode.DISABLED


def test_resolve_mode_override_above_dry_run_ceiling_raises() -> None:
    cfg = _cfg(dry_run=True, early_momentum_mode="live_micro")
    with pytest.raises(ValueError, match="exceeds"):
        resolve_mode(cfg, STRATEGY_EARLY_MOMENTUM)


def test_resolve_mode_override_above_disabled_ceiling_raises() -> None:
    cfg = _cfg(dry_run=False, auto_trade=False, pump_short_mode="paper")
    with pytest.raises(ValueError, match="exceeds"):
        resolve_mode(cfg, STRATEGY_PUMP_SHORT)


@pytest.mark.parametrize("mode", ["live_probe", "live_micro"])
def test_resolve_mode_unimplemented_modes_always_raise(mode: str) -> None:
    """Even when the mode fits under the ceiling (AUTO_TRADE's ceiling is
    LIVE_MICRO, which covers both), no broker implements them in this
    build -- must fail loud, never silently fall back to PAPER."""
    cfg = _cfg(auto_trade=True, liquidation_cascade_mode=mode)
    with pytest.raises(ValueError, match="no implemented broker"):
        resolve_mode(cfg, STRATEGY_LIQUIDATION_CASCADE)


def test_resolve_mode_shadow_override_is_legal_for_liquidation_cascade() -> None:
    cfg = _cfg(dry_run=True, liquidation_cascade_mode="shadow")
    assert resolve_mode(cfg, STRATEGY_LIQUIDATION_CASCADE) == TradingMode.SHADOW


def test_resolve_mode_shadow_override_is_legal_for_early_momentum() -> None:
    cfg = _cfg(auto_trade=True, early_momentum_mode="shadow")
    assert resolve_mode(cfg, STRATEGY_EARLY_MOMENTUM) == TradingMode.SHADOW


def test_resolve_mode_shadow_override_always_raises_for_pump_short() -> None:
    """pump_short already writes its own decision evidence unconditionally
    in trader.py -- SHADOW must stay forbidden for it specifically, even
    though the ceiling would otherwise allow it."""
    cfg = _cfg(auto_trade=True, pump_short_mode="shadow")
    with pytest.raises(ValueError, match="not usable by this strategy"):
        resolve_mode(cfg, STRATEGY_PUMP_SHORT)


def test_resolve_mode_unknown_strategy_raises() -> None:
    cfg = _cfg(dry_run=True)
    with pytest.raises(ValueError, match="unknown strategy"):
        resolve_mode(cfg, "not_a_real_strategy")


def test_parse_mode_rejects_unknown_string() -> None:
    with pytest.raises(ValueError):
        parse_mode("not_a_real_mode")


def test_parse_mode_accepts_every_declared_value() -> None:
    for mode in TradingMode:
        assert parse_mode(mode.value) is mode


# ---- ExecutionIntent / ExecutionResult ----


def test_execution_intent_accepts_a_valid_construction() -> None:
    intent = _intent()
    assert intent.side == "short"


@pytest.mark.parametrize("side", ["buy", "sell", "", "LONG"])
def test_execution_intent_rejects_bad_side(side: str) -> None:
    with pytest.raises(ValueError, match="side"):
        _intent(side=side)


@pytest.mark.parametrize("size_usd", [0.0, -1.0])
def test_execution_intent_rejects_non_positive_size(size_usd: float) -> None:
    with pytest.raises(ValueError, match="size_usd"):
        _intent(size_usd=size_usd)


@pytest.mark.parametrize("size_usd", [float("nan"), float("inf"), float("-inf")])
def test_execution_intent_rejects_non_finite_size(size_usd: float) -> None:
    """nan <= 0 and inf <= 0 are both False in Python -- a plain positivity
    check alone silently lets both straight through (colleague review)."""
    with pytest.raises(ValueError, match="size_usd"):
        _intent(size_usd=size_usd)


@pytest.mark.parametrize("price", [float("nan"), float("inf"), float("-inf")])
def test_execution_intent_rejects_non_finite_price(price: float) -> None:
    with pytest.raises(ValueError, match="price"):
        _intent(price=price)


@pytest.mark.parametrize("leverage", [0, -1])
def test_execution_intent_rejects_non_positive_leverage(leverage: int) -> None:
    with pytest.raises(ValueError, match="leverage"):
        _intent(leverage=leverage)


@pytest.mark.parametrize("price", [0.0, -1.0])
def test_execution_intent_rejects_non_positive_price_when_given(price: float) -> None:
    with pytest.raises(ValueError, match="price"):
        _intent(price=price)


def test_execution_intent_allows_none_price() -> None:
    intent = _intent(price=None)
    assert intent.price is None


def test_execution_intent_rejects_empty_idempotency_key() -> None:
    with pytest.raises(ValueError, match="idempotency_key"):
        _intent(idempotency_key="")


def test_execution_intent_rejects_empty_strategy_name() -> None:
    with pytest.raises(ValueError, match="strategy"):
        _intent(strategy=StrategyIdentity(name="", version="1"))


def test_execution_intent_rejects_empty_strategy_version() -> None:
    with pytest.raises(ValueError, match="strategy"):
        _intent(strategy=StrategyIdentity(name="pump_short", version=""))


@pytest.mark.parametrize("status", list(ExecutionStatus))
def test_execution_result_committed_matches_status(status: ExecutionStatus) -> None:
    result = ExecutionResult(mode=TradingMode.PAPER, status=status)
    assert result.committed == (status != ExecutionStatus.REJECTED)


# ---- DisabledBroker ----


async def test_disabled_broker_always_rejects_without_touching_rdb() -> None:
    rdb = MagicMock()
    result = await DisabledBroker().open(_intent(), cfg=_cfg(), rdb=rdb)
    assert result.status == ExecutionStatus.REJECTED
    assert result.committed is False
    rdb.set.assert_not_called()


# ---- build_broker ----


def test_build_broker_returns_paper_broker() -> None:
    assert isinstance(build_broker(TradingMode.PAPER, exchanges={}), PaperBroker)


def test_build_broker_returns_disabled_broker() -> None:
    assert isinstance(build_broker(TradingMode.DISABLED, exchanges={}), DisabledBroker)


@pytest.mark.parametrize("mode", [TradingMode.LIVE_PROBE, TradingMode.LIVE_MICRO])
def test_build_broker_raises_for_unimplemented_modes(mode: TradingMode) -> None:
    with pytest.raises(NotImplementedError):
        build_broker(mode, exchanges={})


def test_build_broker_shadow_returns_shadow_broker() -> None:
    assert isinstance(build_broker(TradingMode.SHADOW, exchanges={}), ShadowBroker)


# ---- PaperBroker: non-episode path (pump_short / liquidation_cascade shape) ----


async def test_paper_broker_dispatches_to_open_paper_with_full_kwargs() -> None:
    intent = _intent()
    with patch(
        "schurfer_execution.execution_intent.paper.open_paper",
        AsyncMock(return_value=42),
    ) as open_paper:
        result = await PaperBroker().open(intent, cfg=_cfg(), rdb=MagicMock())

    open_paper.assert_awaited_once()
    kw = open_paper.call_args.kwargs
    assert kw["instrument"] is intent.instrument
    assert kw["price"] == intent.price
    assert kw["size_usd"] == intent.size_usd
    assert kw["leverage"] == intent.leverage
    assert kw["score"] == intent.score
    assert kw["setup_context"] is intent.setup_context
    assert kw["side"] == intent.side
    assert kw["exit_params"] is None
    assert result.status == ExecutionStatus.PAPER_OPENED
    assert result.trade_id == 42
    assert result.committed is True


async def test_paper_broker_rejects_when_price_is_none() -> None:
    with patch(
        "schurfer_execution.execution_intent.paper.open_paper", new_callable=AsyncMock
    ) as open_paper:
        result = await PaperBroker().open(_intent(price=None), cfg=_cfg(), rdb=MagicMock())

    open_paper.assert_not_awaited()
    assert result.status == ExecutionStatus.REJECTED


async def test_paper_broker_redis_write_matches_direct_open_paper_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Strongest proof paper behavior is unchanged: the real paper.open_paper
    (not mocked) writes the same Redis key/TTL/payload whether called
    directly or through PaperBroker."""
    monkeypatch.setattr("schurfer_execution.paper.journal.open_trade", AsyncMock(return_value=None))
    rdb = MagicMock()
    rdb.set = AsyncMock()
    cfg = _cfg(db_url=None)

    result = await PaperBroker().open(_intent(), cfg=cfg, rdb=rdb)

    rdb.set.assert_called_once()
    key, payload_json = rdb.set.call_args.args
    assert key == "position:paper:bybit:BEAT"
    payload = json.loads(payload_json)
    assert payload["entry_price"] == 1.5
    assert payload["size_usd"] == 50.0
    assert payload["leverage"] == 3
    assert payload["side"] == "short"
    assert result.trade_id is None  # cfg.db_url is None, matches paper.open_paper's own contract


# ---- PaperBroker: episode path (early_momentum shape) ----


def _episode_intent(**overrides: Any) -> ExecutionIntent:
    fields: dict[str, Any] = {
        "strategy": StrategyIdentity(name="early_momentum", version="4"),
        "instrument": _instrument(),
        "side": "long",
        "size_usd": 100.0,
        "leverage": 5,
        "score": 100,
        "setup_context": {"strategy": "early_momentum_v4"},
        "idempotency_key": "e1:entry:base",
        "price": 1.5,
        "exit_params": {"initial_sl_pct": 10.0},
        "claim": EpisodeClaim(episode_id="e1", claim_token="tok-1"),  # noqa: S106
    }
    fields.update(overrides)
    return ExecutionIntent(**fields)


async def test_paper_broker_episode_dispatches_to_open_paper_for_episode() -> None:
    intent = _episode_intent()
    outcome = OpenTradeOutcome(trade_id=42, created=True, recovered=False, claim_valid=True)
    with patch(
        "schurfer_execution.execution_intent.paper.open_paper_for_episode",
        AsyncMock(return_value=outcome),
    ) as open_paper_for_episode:
        result = await PaperBroker().open(intent, cfg=_cfg(dry_run=True), rdb=MagicMock())

    open_paper_for_episode.assert_awaited_once()
    kw = open_paper_for_episode.call_args.kwargs
    assert kw["episode_id"] == "e1"
    assert kw["claim_token"] == "tok-1"  # noqa: S105
    assert kw["entry_idempotency_key"] == "e1:entry:base"
    assert kw["exit_params"] == {"initial_sl_pct": 10.0}
    assert result.status == ExecutionStatus.PAPER_OPENED
    assert result.trade_id == 42
    assert result.claim_valid is True


async def test_paper_broker_episode_rejects_when_exit_params_missing() -> None:
    with patch(
        "schurfer_execution.execution_intent.paper.open_paper_for_episode", new_callable=AsyncMock
    ) as open_paper_for_episode:
        result = await PaperBroker().open(
            _episode_intent(exit_params=None), cfg=_cfg(dry_run=True), rdb=MagicMock()
        )

    open_paper_for_episode.assert_not_awaited()
    assert result.status == ExecutionStatus.REJECTED


async def test_paper_broker_episode_rejects_when_db_url_missing() -> None:
    with patch(
        "schurfer_execution.execution_intent.paper.open_paper_for_episode", new_callable=AsyncMock
    ) as open_paper_for_episode:
        result = await PaperBroker().open(
            _episode_intent(), cfg=_cfg(dry_run=True, db_url=None), rdb=MagicMock()
        )

    open_paper_for_episode.assert_not_awaited()
    assert result.status == ExecutionStatus.REJECTED


async def test_paper_broker_episode_idempotency_collision_keeps_claim_valid() -> None:
    """trade_id=None with claim_valid=True means an idempotency-key
    collision (the row already exists under a different episode/strategy),
    not an invalid claim -- the caller must still be allowed to terminate
    the episode with its own (still-valid) claim_token."""
    outcome = OpenTradeOutcome(trade_id=None, created=False, recovered=False, claim_valid=True)
    with patch(
        "schurfer_execution.execution_intent.paper.open_paper_for_episode",
        AsyncMock(return_value=outcome),
    ):
        result = await PaperBroker().open(
            _episode_intent(), cfg=_cfg(dry_run=True), rdb=MagicMock()
        )

    assert result.status == ExecutionStatus.REJECTED
    assert result.claim_valid is True


async def test_paper_broker_episode_invalid_claim_sets_claim_valid_false() -> None:
    """A reclaimed/expired claim under the caller -- the caller's own
    claim_token is stale and must not be used to terminate the episode."""
    outcome = OpenTradeOutcome(trade_id=None, created=False, recovered=False, claim_valid=False)
    with patch(
        "schurfer_execution.execution_intent.paper.open_paper_for_episode",
        AsyncMock(return_value=outcome),
    ):
        result = await PaperBroker().open(
            _episode_intent(), cfg=_cfg(dry_run=True), rdb=MagicMock()
        )

    assert result.status == ExecutionStatus.REJECTED
    assert result.claim_valid is False


# ---- ShadowBroker ----


def _shadow_intent(**overrides: Any) -> ExecutionIntent:
    fields: dict[str, Any] = {
        "strategy": StrategyIdentity(name="liquidation_cascade", version="2"),
        "instrument": _instrument(),
        "side": "long",
        "size_usd": 100.0,
        "leverage": 5,
        "score": 100,
        "setup_context": {"strategy": "liquidation_cascade_v2"},
        "idempotency_key": "liquidation_cascade:v2:bybit:BEATUSDT:2026-08-24T00:00:00",
        "price": 1.5,
    }
    fields.update(overrides)
    return ExecutionIntent(**fields)


async def test_shadow_broker_writes_decision_with_full_kwargs() -> None:
    intent = _shadow_intent()
    with (
        patch(
            "schurfer_execution.execution_intent.journal.ensure_strategy",
            AsyncMock(return_value=7),
        ) as ensure_strategy,
        patch(
            "schurfer_execution.execution_intent.decisions.write_decision",
            new_callable=AsyncMock,
        ) as write_decision,
    ):
        result = await ShadowBroker().open(intent, cfg=_cfg(), rdb=MagicMock())

    ensure_strategy.assert_awaited_once_with(_cfg().db_url, name="liquidation_cascade", version="2")
    write_decision.assert_awaited_once()
    kw = write_decision.call_args.kwargs
    assert kw["base"] == intent.instrument.base
    assert kw["exchange"] == intent.instrument.exchange
    assert kw["score"] == intent.score
    assert kw["strategy_version"] == "2"
    assert kw["strategy_id"] == 7
    assert kw["trading_mode"] == "shadow"
    assert kw["features"] is intent.setup_context
    assert kw["price"] == intent.price
    assert result.status == ExecutionStatus.SHADOW_RECORDED
    assert result.trade_id is None
    assert result.committed is True


async def test_shadow_broker_decision_id_is_deterministic_from_idempotency_key() -> None:
    """A strategy that re-emits the same intent every tick (see
    liquidation_cascade.py's own comment on this) must collapse to one
    decision row via ON CONFLICT (decision_id) DO NOTHING -- which only
    works if the same idempotency_key always derives the same decision_id."""
    intent_a = _shadow_intent()
    intent_b = _shadow_intent()  # same idempotency_key, fresh object
    with (
        patch(
            "schurfer_execution.execution_intent.journal.ensure_strategy",
            AsyncMock(return_value=7),
        ),
        patch(
            "schurfer_execution.execution_intent.decisions.write_decision",
            new_callable=AsyncMock,
        ) as write_decision,
    ):
        await ShadowBroker().open(intent_a, cfg=_cfg(), rdb=MagicMock())
        await ShadowBroker().open(intent_b, cfg=_cfg(), rdb=MagicMock())

    first_id = write_decision.call_args_list[0].kwargs["decision_id"]
    second_id = write_decision.call_args_list[1].kwargs["decision_id"]
    assert first_id == second_id

    other_id_intent = _shadow_intent(idempotency_key="a-different-key")
    with (
        patch(
            "schurfer_execution.execution_intent.journal.ensure_strategy",
            AsyncMock(return_value=7),
        ),
        patch(
            "schurfer_execution.execution_intent.decisions.write_decision",
            new_callable=AsyncMock,
        ) as write_decision_other,
    ):
        await ShadowBroker().open(other_id_intent, cfg=_cfg(), rdb=MagicMock())
    assert write_decision_other.call_args.kwargs["decision_id"] != first_id


async def test_shadow_broker_rejects_when_price_is_none() -> None:
    with patch(
        "schurfer_execution.execution_intent.decisions.write_decision", new_callable=AsyncMock
    ) as write_decision:
        result = await ShadowBroker().open(_shadow_intent(price=None), cfg=_cfg(), rdb=MagicMock())

    write_decision.assert_not_awaited()
    assert result.status == ExecutionStatus.REJECTED


async def test_shadow_broker_rejects_when_db_url_missing() -> None:
    with patch(
        "schurfer_execution.execution_intent.decisions.write_decision", new_callable=AsyncMock
    ) as write_decision:
        result = await ShadowBroker().open(_shadow_intent(), cfg=_cfg(db_url=None), rdb=MagicMock())

    write_decision.assert_not_awaited()
    assert result.status == ExecutionStatus.REJECTED


# ---- canonical strategy identity matches journal's own registry parser ----
#
# Each of trader.py/early_momentum.py/liquidation_cascade.py now derives its
# intent's StrategyIdentity via journal.strategy_identity(setup_context) --
# the SAME function journal.open_trade(_for_episode) uses to register the
# app.strategies row -- instead of hand-building name/version separately.
# These pin the exact real setup_context shape each strategy builds today,
# so a future edit to any of them that silently reintroduces a second,
# independent identity source fails here (colleague review: this is
# precisely how pump_short's StrategyIdentity(name="pump_short",
# version=cfg.strategy_version) drifted from the registered
# ("pump_short", "1_market_quality") -- cfg.strategy_version is the whole
# raw "pump_short_v1_market_quality" string, never parsed).


@pytest.mark.parametrize(
    ("setup_context", "expected"),
    [
        # pump_short: trader.py never sets "strategy", only a possibly-
        # combined "strategy_version" (journal.strategy_identity's own
        # historical convention default name is "pump_short").
        ({"strategy_version": "pump_short_v1_market_quality"}, ("pump_short", "1_market_quality")),
        ({"strategy_version": "1"}, ("pump_short", "1")),
        # early_momentum: sets a combined "strategy" key directly.
        ({"strategy": "early_momentum_v4"}, ("early_momentum", "4")),
        # liquidation_cascade: same combined-"strategy" convention.
        ({"strategy": "liquidation_cascade_v2"}, ("liquidation_cascade", "2")),
    ],
)
def test_journal_strategy_identity_matches_each_strategys_real_setup_context(
    setup_context: dict[str, Any], expected: tuple[str, str]
) -> None:
    from schurfer_execution.journal import strategy_identity

    assert strategy_identity(setup_context) == expected
