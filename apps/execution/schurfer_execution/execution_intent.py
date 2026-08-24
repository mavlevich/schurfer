"""Universal ExecutionIntent -> ExecutionResult interface: a strategy builds
an intent, a Broker (selected by TradingMode) decides how to execute it.
Strategy code must never branch on cfg.dry_run/cfg.auto_trade itself or call
paper.py/orders.py directly for its entry -- that per-strategy branching is
exactly the pattern this module replaces (see early_momentum.py's own
comment on why live code must not live inside a strategy file).

Scope of this module today: only PAPER and DISABLED have a working broker.
SHADOW needs app.trade_decisions' schema generalized first (it is
pump_short-shaped -- strategy_version but no strategy_name, pump_pct/
pump_event_id columns) -- see feat/execution-shadow-evidence-v1. LIVE_PROBE
and LIVE_MICRO need durable intent persistence, clientOrderId, and
reconciliation before any code may call orders.place_order through this
interface -- see feat/live-order-lifecycle-v1. Both are declared here as
TradingMode/ExecutionStatus values so the type doesn't need to change shape
again when those PRs land, but build_broker raises NotImplementedError for
them; nothing in this codebase can currently select them (see resolve_mode).

TradingMode/ExecutionStatus are StrEnum here (episodes.py's STATUS_* module
constants are plain strings, by contrast) because this module's whole job is
being a strict typed contract: the mode ladder is compared and validated
repeatedly (resolve_mode, Config.__post_init__, build_broker) and benefits
from mypy actually checking it, unlike episodes.py's organically-grown
status set.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final, Protocol

import structlog

from . import paper, symbols

if TYPE_CHECKING:
    from .config import Config

log = structlog.get_logger()


class TradingMode(StrEnum):
    DISABLED = "disabled"
    SHADOW = "shadow"
    PAPER = "paper"
    LIVE_PROBE = "live_probe"  # declared, NOT implemented by any broker yet
    LIVE_MICRO = "live_micro"  # declared, NOT implemented by any broker yet


_LADDER: Final[dict[TradingMode, int]] = {
    TradingMode.DISABLED: 0,
    TradingMode.SHADOW: 1,
    TradingMode.PAPER: 2,
    TradingMode.LIVE_PROBE: 3,
    TradingMode.LIVE_MICRO: 4,
}

# No broker implements these yet (see module docstring) -- resolve_mode and
# build_broker both refuse them, independently, on purpose: two lines of
# defense against a config typo silently reaching a code path that was never
# built or tested.
_UNIMPLEMENTED_MODES: Final[frozenset[TradingMode]] = frozenset(
    {TradingMode.SHADOW, TradingMode.LIVE_PROBE, TradingMode.LIVE_MICRO}
)

# Every strategy name resolve_mode/build_broker are ever called with -- kept
# as a closed set (not "any string") so a typo in a per-strategy env var name
# fails at startup instead of silently resolving to the unset-override default.
STRATEGY_PUMP_SHORT: Final = "pump_short"
STRATEGY_EARLY_MOMENTUM: Final = "early_momentum"
STRATEGY_LIQUIDATION_CASCADE: Final = "liquidation_cascade"
_KNOWN_STRATEGIES: Final = frozenset(
    {STRATEGY_PUMP_SHORT, STRATEGY_EARLY_MOMENTUM, STRATEGY_LIQUIDATION_CASCADE}
)

_MODE_ENV_VAR: Final[dict[str, str]] = {
    STRATEGY_PUMP_SHORT: "pump_short_mode",
    STRATEGY_EARLY_MOMENTUM: "early_momentum_mode",
    STRATEGY_LIQUIDATION_CASCADE: "liquidation_cascade_mode",
}


def parse_mode(value: str) -> TradingMode:
    """Raises ValueError (via TradingMode's own StrEnum lookup) on an unknown
    string -- never guesses/normalizes, a typo in an env var must fail loud."""
    return TradingMode(value)


def _mode_override(cfg: Config, strategy: str) -> TradingMode | None:
    if strategy not in _KNOWN_STRATEGIES:
        raise ValueError(f"unknown strategy {strategy!r}")
    raw = getattr(cfg, _MODE_ENV_VAR[strategy])
    return None if raw is None else parse_mode(raw)


def resolve_mode(cfg: Config, strategy: str) -> TradingMode:
    """The mode ladder's only entry point -- every call site (Config
    validation at startup, and each run_* task at broker-construction time)
    must go through this, never re-derive dry_run/auto_trade -> mode logic
    locally.

    ceiling is what the two existing global switches allow at most:
    auto_trade=True -> LIVE_MICRO, dry_run=True -> PAPER, neither -> DISABLED
    (matches main.py's own task-startup gating, which this function does not
    change or duplicate -- resolve_mode only ever runs for a strategy whose
    task main.py already decided to start).

    An UNSET override never exceeds PAPER, regardless of ceiling -- this is
    the fix for the reviewed danger in an earlier draft of this module: with
    auto_trade=True (ceiling LIVE_MICRO) and no per-strategy override set,
    resolve_mode must return PAPER, not silently inherit the ceiling into
    live trading. A live (or shadow) mode is only ever reached by an
    explicit, correctly-spelled override -- never by omission.
    """
    if cfg.auto_trade:
        ceiling = TradingMode.LIVE_MICRO
    elif cfg.dry_run:
        ceiling = TradingMode.PAPER
    else:
        ceiling = TradingMode.DISABLED

    override = _mode_override(cfg, strategy)
    if override is None:
        return TradingMode.PAPER if _LADDER[ceiling] >= _LADDER[TradingMode.PAPER] else ceiling

    if _LADDER[override] > _LADDER[ceiling]:
        raise ValueError(
            f"{strategy}: mode {override.value!r} exceeds what the current "
            f"config ({ceiling.value!r} ceiling) allows"
        )
    if override in _UNIMPLEMENTED_MODES:
        raise ValueError(
            f"{strategy}: mode {override.value!r} has no implemented broker in this build"
        )
    return override


class ExecutionStatus(StrEnum):
    REJECTED = "rejected"  # business rejection, no side effect -- reason must be set
    SHADOW_RECORDED = "shadow_recorded"  # declared for forward compat; unreachable, no ShadowBroker
    PAPER_OPENED = "paper_opened"
    # LIVE_ACCEPTED / FILL_UNRESOLVED / LIVE_FILLED_JOURNAL_PENDING /
    # JOURNAL_COMMITTED / EMERGENCY_CLOSED: declared for the future live
    # broker's shape, unreachable in this build -- only PaperBroker exists,
    # so only REJECTED and PAPER_OPENED are ever actually produced today.
    LIVE_ACCEPTED = "live_accepted"
    FILL_UNRESOLVED = "fill_unresolved"
    LIVE_FILLED_JOURNAL_PENDING = "live_filled_journal_pending"
    JOURNAL_COMMITTED = "journal_committed"
    EMERGENCY_CLOSED = "emergency_closed"


@dataclass(frozen=True)
class StrategyIdentity:
    """Structural identity for routing/logging only -- NOT the journal
    registry lookup. journal.strategy_identity(setup_context) (unchanged) is
    still the only source that resolves app.strategies' (name, version); it
    reads setup_context's own three conventions (explicit strategy_name, a
    combined "name_vN" in strategy, or pump_short's bare strategy_version).
    This type must never be re-derived from setup_context or cross-checked
    against it -- pump_short's setup_context has no "strategy"/"strategy_name"
    key at all, so a strict equality check would fail every pump_short
    intent. Keeping this a separate, deliberately un-cross-checked field is
    what avoids the two-sources-of-truth bug class fixed on /trades today
    (Source/Strategy columns, and pump_short's "Strategy: unknown" Telegram
    messages) from recurring here.
    """

    name: str
    version: str


@dataclass(frozen=True)
class EpisodeClaim:
    """early_momentum-v4 only -- the claim/lease two-phase reserve-then-open
    workflow (episodes.py) is not a universal pattern; pump_short and
    liquidation_cascade have neither an episode row nor a claim token, and
    must never be routed through open_trade_for_episode."""

    episode_id: str
    claim_token: str


@dataclass(frozen=True)
class ExecutionIntent:
    strategy: StrategyIdentity
    instrument: symbols.ExecutionInstrument
    side: str
    size_usd: float
    leverage: int
    score: int
    setup_context: dict[str, Any]  # evidence payload; brokers never mutate this in place
    idempotency_key: str
    price: float | None = None  # None => a future live broker discovers it; PaperBroker requires it
    exit_params: dict[str, float] | None = None
    claim: EpisodeClaim | None = None

    def __post_init__(self) -> None:
        # Raised only for programmer errors -- never for a business
        # rejection, which must always come back as
        # ExecutionResult(status=REJECTED, ...). Every call site building an
        # intent sits inside a bare `except Exception` scan-loop body that
        # would silently swallow a raise here, so this must only ever fire
        # on a genuine bug in the caller, not on live market/config state.
        if self.side not in ("long", "short"):
            raise ValueError(f"side must be 'long' or 'short', got {self.side!r}")
        # math.isfinite() first: NaN/inf compare False against every `<= 0`
        # (nan <= 0 is False, inf <= 0 is False), so a plain positivity
        # check alone silently lets both straight through (colleague
        # review).
        if not math.isfinite(self.size_usd) or self.size_usd <= 0:
            raise ValueError(f"size_usd must be finite and positive, got {self.size_usd}")
        if self.leverage < 1:
            raise ValueError(f"leverage must be >= 1, got {self.leverage}")
        if self.price is not None and (not math.isfinite(self.price) or self.price <= 0):
            raise ValueError(f"price must be finite and positive when given, got {self.price}")
        if not self.idempotency_key:
            raise ValueError("idempotency_key must not be empty")
        if not self.strategy.name or not self.strategy.version:
            raise ValueError("strategy name and version must not be empty")


@dataclass(frozen=True)
class ExecutionResult:
    mode: TradingMode
    status: ExecutionStatus
    reason: str = ""  # non-empty iff status == REJECTED
    trade_id: int | None = None
    filled_price: float | None = None
    # early_momentum-v4 only, meaningless (default True) for any intent
    # without an EpisodeClaim: journal.OpenTradeOutcome.claim_valid passed
    # through verbatim. False means the episode's claim was already
    # reclaimed or expired under the caller, so the caller's own claim_token
    # is stale and must not be used to terminate the episode. Collapsing
    # this distinction into the reason string and string-matching it back
    # out would be fragile; the underlying API already distinguishes it, so
    # this type does too.
    claim_valid: bool = True

    @property
    def committed(self) -> bool:
        """Convenience for call sites that only need "did a position (or
        shadow record) get created" as a bool. Every non-REJECTED status is
        committed=True by definition, including states unreachable today."""
        return self.status != ExecutionStatus.REJECTED


def _rejected(mode: TradingMode, reason: str, *, claim_valid: bool = True) -> ExecutionResult:
    return ExecutionResult(
        mode=mode, status=ExecutionStatus.REJECTED, reason=reason, claim_valid=claim_valid
    )


class Broker(Protocol):
    """open() is at-most-once for any future live mode and must NEVER be
    retried by a caller on failure/timeout -- journal.open_trade has no
    idempotency key (unlike journal.open_trade_for_episode's
    entry_idempotency_key), so a naive retry can create a second real
    position. This constraint is enforced by convention today (only
    PaperBroker exists, and paper.open_paper/open_paper_for_episode are
    already each individually safe to call once); a future LiveBroker must
    not weaken it."""

    mode: TradingMode

    async def open(self, intent: ExecutionIntent, *, cfg: Config, rdb: Any) -> ExecutionResult: ...


class DisabledBroker:
    """Every call rejects without touching Redis/DB/an exchange -- lets a
    strategy's run_* loop stay written as "always call broker.open()"
    without its own separate "am I disabled" branch."""

    mode = TradingMode.DISABLED

    async def open(self, intent: ExecutionIntent, *, cfg: Config, rdb: Any) -> ExecutionResult:
        return _rejected(self.mode, "strategy disabled")


class PaperBroker:
    """Pure pass-through to the existing paper.py functions -- zero new
    trading logic. Dispatches on intent.claim, never on the presence of
    idempotency_key (which every intent carries, episode or not) -- the
    claim/lease workflow is early_momentum-v4-specific (see EpisodeClaim)."""

    mode = TradingMode.PAPER

    async def open(self, intent: ExecutionIntent, *, cfg: Config, rdb: Any) -> ExecutionResult:
        if intent.price is None:
            return _rejected(self.mode, "paper broker requires a resolved price")

        if intent.claim is not None:
            if intent.exit_params is None:
                return _rejected(self.mode, "episode intent requires exit_params")
            if not cfg.db_url:
                # open_paper_for_episode raises ValueError on a missing
                # db_url rather than returning a sentinel -- checked here so
                # that's a REJECTED result, not an uncaught exception from
                # inside broker.open().
                return _rejected(self.mode, "episode path requires cfg.db_url")
            outcome = await paper.open_paper_for_episode(
                rdb,
                instrument=intent.instrument,
                price=intent.price,
                size_usd=intent.size_usd,
                leverage=intent.leverage,
                score=intent.score,
                setup_context=intent.setup_context,
                cfg=cfg,
                side=intent.side,
                exit_params=intent.exit_params,
                episode_id=intent.claim.episode_id,
                claim_token=intent.claim.claim_token,
                entry_idempotency_key=intent.idempotency_key,
            )
            if outcome.trade_id is None:
                reason = (
                    "episode claim invalid (reclaimed/expired)"
                    if not outcome.claim_valid
                    else "idempotency-key collision"
                )
                return _rejected(self.mode, reason, claim_valid=outcome.claim_valid)
            return ExecutionResult(
                mode=self.mode,
                status=ExecutionStatus.PAPER_OPENED,
                trade_id=outcome.trade_id,
                filled_price=intent.price,
            )

        trade_id = await paper.open_paper(
            rdb,
            instrument=intent.instrument,
            price=intent.price,
            size_usd=intent.size_usd,
            leverage=intent.leverage,
            score=intent.score,
            setup_context=intent.setup_context,
            cfg=cfg,
            side=intent.side,
            exit_params=intent.exit_params,
        )
        return ExecutionResult(
            mode=self.mode,
            status=ExecutionStatus.PAPER_OPENED,
            trade_id=trade_id,
            filled_price=intent.price,
        )


def build_broker(mode: TradingMode, *, exchanges: dict[str, Any]) -> Broker:
    """exchanges is accepted now (unused by PAPER/DISABLED) so the call
    signature at every main.py call site doesn't need to change again once
    a live broker needs it."""
    if mode is TradingMode.PAPER:
        return PaperBroker()
    if mode is TradingMode.DISABLED:
        return DisabledBroker()
    raise NotImplementedError(
        f"TradingMode.{mode.name} has no broker implementation yet -- "
        "see execution_intent.py's module docstring for the PR that adds it"
    )


__all__ = [
    "STRATEGY_EARLY_MOMENTUM",
    "STRATEGY_LIQUIDATION_CASCADE",
    "STRATEGY_PUMP_SHORT",
    "Broker",
    "DisabledBroker",
    "EpisodeClaim",
    "ExecutionIntent",
    "ExecutionResult",
    "ExecutionStatus",
    "PaperBroker",
    "StrategyIdentity",
    "TradingMode",
    "build_broker",
    "parse_mode",
    "resolve_mode",
]
