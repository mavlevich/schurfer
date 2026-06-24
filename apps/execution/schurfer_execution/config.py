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

    # Exit parameters
    take_profit_pct: float = field(default_factory=lambda: _float("TAKE_PROFIT_PCT", 15.0))
    stop_loss_pct: float = field(default_factory=lambda: _float("STOP_LOSS_PCT", 5.0))
    max_hold_minutes: int = field(default_factory=lambda: _int("MAX_HOLD_MINUTES", 60))

    # Signal trader — set AUTO_TRADE=true and SIGNAL_POSITION_USD>0 to enable.
    # Scores are read from Redis (signals:{base}) — written by the api-gateway ticker.
    auto_trade: bool = field(default_factory=lambda: _bool("AUTO_TRADE", False))
    signal_position_usd: float = field(default_factory=lambda: _float("SIGNAL_POSITION_USD", 50.0))
    signal_leverage: int = field(default_factory=lambda: _int("SIGNAL_LEVERAGE", 3))

    def __post_init__(self) -> None:
        if not self.auto_trade:
            return
        if self.signal_position_usd <= 0:
            raise ValueError(f"SIGNAL_POSITION_USD must be > 0, got {self.signal_position_usd}")
        if not 1 <= self.signal_leverage <= 125:
            raise ValueError(f"SIGNAL_LEVERAGE must be 1-125, got {self.signal_leverage}")
