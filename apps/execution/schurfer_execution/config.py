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


@dataclass
class Config:
    redis_addr: str = field(default_factory=lambda: os.getenv("REDIS_ADDR", "localhost:6379"))

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
