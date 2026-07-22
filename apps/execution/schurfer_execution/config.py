import os
from dataclasses import dataclass, field


def _env(key: str, default: str | None = None) -> str | None:
    return os.getenv(key) or default


def _float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key) or default)
    except ValueError:
        return default


def _int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key) or default)
    except ValueError:
        return default


def _bool(key: str, default: bool) -> bool:
    val = os.getenv(key)
    if val is None:
        return default
    return val.strip().lower() not in ("0", "false", "no", "")


@dataclass
class Config:
    redis_addr: str = field(default_factory=lambda: os.getenv("REDIS_ADDR", "localhost:6379"))

    # Set to true to route all exchanges to their sandbox/testnet endpoints.
    # Requires testnet API keys in the exchange key env vars.
    testnet: bool = field(default_factory=lambda: _bool("TESTNET", False))

    # Exchange API keys — only exchanges with keys configured are active.
    # OKX and KuCoin also require a passphrase.
    binance_api_key: str | None = field(default_factory=lambda: _env("BINANCE_API_KEY"))
    binance_api_secret: str | None = field(default_factory=lambda: _env("BINANCE_API_SECRET"))

    bybit_api_key: str | None = field(default_factory=lambda: _env("BYBIT_API_KEY"))
    bybit_api_secret: str | None = field(default_factory=lambda: _env("BYBIT_API_SECRET"))

    okx_api_key: str | None = field(default_factory=lambda: _env("OKX_API_KEY"))
    okx_api_secret: str | None = field(default_factory=lambda: _env("OKX_API_SECRET"))
    okx_passphrase: str | None = field(default_factory=lambda: _env("OKX_PASSPHRASE"))

    gate_api_key: str | None = field(default_factory=lambda: _env("GATE_API_KEY"))
    gate_api_secret: str | None = field(default_factory=lambda: _env("GATE_API_SECRET"))

    kucoin_api_key: str | None = field(default_factory=lambda: _env("KUCOIN_API_KEY"))
    kucoin_api_secret: str | None = field(default_factory=lambda: _env("KUCOIN_API_SECRET"))
    kucoin_passphrase: str | None = field(default_factory=lambda: _env("KUCOIN_PASSPHRASE"))

    bingx_api_key: str | None = field(default_factory=lambda: _env("BINGX_API_KEY"))
    bingx_api_secret: str | None = field(default_factory=lambda: _env("BINGX_API_SECRET"))

    mexc_api_key: str | None = field(default_factory=lambda: _env("MEXC_API_KEY"))
    mexc_api_secret: str | None = field(default_factory=lambda: _env("MEXC_API_SECRET"))

    # Risk parameters
    max_positions: int = field(default_factory=lambda: _int("MAX_POSITIONS", 5))
    max_position_usd: float = field(default_factory=lambda: _float("MAX_POSITION_USD", 500.0))
    daily_loss_limit_usd: float = field(
        default_factory=lambda: _float("DAILY_LOSS_LIMIT_USD", 200.0)
    )
    score_threshold: int = field(default_factory=lambda: _int("SCORE_THRESHOLD", 6))
    # Minimum gap between initial SL and liquidation price, as % of liquidation distance.
    # E.g. 20.0 means SL must be at most 80% of the way to liquidation.
    liquidation_buffer_pct: float = field(
        default_factory=lambda: _float("LIQUIDATION_BUFFER_PCT", 20.0)
    )
    # Minimum acceptable funding rate (% per 8h, normalized). Negative = shorts pay longs.
    # -0.1 blocks only extreme cases while we gather data to calibrate.
    min_funding_rate_pct: float = field(
        default_factory=lambda: _float("MIN_FUNDING_RATE_PCT", -0.1)
    )
    # When True, skip entry if funding rate cannot be fetched (fail-closed).
    # Default False (fail-open) is safe for dry-run; set True for live AUTO_TRADE.
    require_funding_rate: bool = field(default_factory=lambda: _bool("REQUIRE_FUNDING_RATE", False))
    # Two-sided execution-quality gate. It remains measurable when disabled in
    # dry-run, but AUTO_TRADE is forbidden unless the gate is enabled.
    require_market_quality: bool = field(
        default_factory=lambda: _bool("REQUIRE_MARKET_QUALITY", True)
    )
    max_spread_bps: float = field(default_factory=lambda: _float("MAX_SPREAD_BPS", 50.0))
    max_liquidity_impact_bps: float = field(
        default_factory=lambda: _float("MAX_LIQUIDITY_IMPACT_BPS", 50.0)
    )
    liquidity_depth_multiplier: float = field(
        default_factory=lambda: _float("LIQUIDITY_DEPTH_MULTIPLIER", 2.0)
    )

    # Signal trader — set AUTO_TRADE=true and SIGNAL_POSITION_USD>0 to enable.
    # Scores are read from Redis (signals:{base}) — written by the api-gateway ticker.
    auto_trade: bool = field(default_factory=lambda: _bool("AUTO_TRADE", False))
    dry_run: bool = field(default_factory=lambda: _bool("DRY_RUN", False))
    signal_position_usd: float = field(default_factory=lambda: _float("SIGNAL_POSITION_USD", 50.0))
    signal_leverage: int = field(default_factory=lambda: _int("SIGNAL_LEVERAGE", 3))
    # Strategy version stamped on every decision so accumulated statistics are not
    # mixed across rule changes. Bump when the scoring or entry rules change.
    strategy_version: str = field(
        default_factory=lambda: os.getenv("STRATEGY_VERSION", "pump_short_v1_market_quality")
    )
    # Risk-based position sizing: risk this % of equity per trade.
    # 0.0 = disabled (use fixed SIGNAL_POSITION_USD).
    # When > 0, SIGNAL_POSITION_USD acts as a hard ceiling.
    # Typical range: 0.25-1.0. Formula: size = equity * risk% / initial_sl%.
    risk_per_trade_pct: float = field(default_factory=lambda: _float("RISK_PER_TRADE_PCT", 0.0))

    # Entry quality filters — checked after score/funding/liquidation, before sizing.
    # Fail-closed: if OHLCV fetch fails or data is insufficient, the trade is skipped.
    # REQUIRE_RED_CANDLE: the last *closed* 5m candle ([-2]) must be red (close < open).
    # The still-forming candle ([-1]) is excluded — it can flip before close.
    require_red_candle: bool = field(default_factory=lambda: _bool("REQUIRE_RED_CANDLE", False))
    # MIN_RETRACE_PCT: price must have pulled back at least this % from the candle-window high.
    # 0.0 = disabled. Typical starting value: 1.0-3.0.
    min_retrace_pct: float = field(default_factory=lambda: _float("MIN_RETRACE_PCT", 0.0))

    # Notifications (optional — omit to disable)
    telegram_bot_token: str | None = field(default_factory=lambda: _env("TELEGRAM_BOT_TOKEN"))
    telegram_chat_id: str | None = field(default_factory=lambda: _env("TELEGRAM_CHAT_ID"))

    # Trade journal — required when AUTO_TRADE is on (see below), optional otherwise.
    db_url: str | None = field(default_factory=lambda: _env("DATABASE_URL"))

    def __post_init__(self) -> None:
        if self.auto_trade and self.dry_run:
            raise ValueError("AUTO_TRADE and DRY_RUN are mutually exclusive")
        if self.auto_trade and not self.db_url:
            # Without a journal, the daily-loss circuit breaker degrades to
            # unrealized-only and forgets every closed trade's PnL — realized
            # losses simply vanish from the running total. Not acceptable
            # once real orders are being placed.
            raise ValueError("DATABASE_URL is required when AUTO_TRADE=true")
        if self.auto_trade and not self.require_market_quality:
            raise ValueError("REQUIRE_MARKET_QUALITY must be true when AUTO_TRADE=true")
        if not self.auto_trade and not self.dry_run:
            return
        if self.signal_position_usd <= 0:
            raise ValueError(f"SIGNAL_POSITION_USD must be > 0, got {self.signal_position_usd}")
        if not 1 <= self.signal_leverage <= 125:
            raise ValueError(f"SIGNAL_LEVERAGE must be 1-125, got {self.signal_leverage}")
        if not 0 <= self.liquidation_buffer_pct < 100:
            raise ValueError(
                f"LIQUIDATION_BUFFER_PCT must be 0-99, got {self.liquidation_buffer_pct}"
            )
        if not 0.0 <= self.risk_per_trade_pct <= 5.0:
            raise ValueError(f"RISK_PER_TRADE_PCT must be 0-5, got {self.risk_per_trade_pct}")
        if not 0.0 <= self.min_retrace_pct <= 20.0:
            raise ValueError(f"MIN_RETRACE_PCT must be 0-20, got {self.min_retrace_pct}")
        if not 0.0 < self.max_spread_bps <= 10_000.0:
            raise ValueError(f"MAX_SPREAD_BPS must be > 0 and <= 10000, got {self.max_spread_bps}")
        if not 0.0 < self.max_liquidity_impact_bps <= 10_000.0:
            raise ValueError(
                "MAX_LIQUIDITY_IMPACT_BPS must be > 0 and <= 10000, "
                f"got {self.max_liquidity_impact_bps}"
            )
        if not 1.0 <= self.liquidity_depth_multiplier <= 10.0:
            raise ValueError(
                f"LIQUIDITY_DEPTH_MULTIPLIER must be 1-10, got {self.liquidity_depth_multiplier}"
            )
