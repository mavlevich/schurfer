import os
from dataclasses import dataclass, field


def _list(env: str, default: str) -> list[str]:
    val = os.getenv(env) or default
    return [x.strip() for x in val.split(",") if x.strip()]


@dataclass
class Config:
    redis_addr: str = field(default_factory=lambda: os.getenv("REDIS_ADDR", "localhost:6379"))
    db_url: str | None = field(default_factory=lambda: os.getenv("DATABASE_URL"))
    exchanges: list[str] = field(
        default_factory=lambda: _list(
            "PUMP_EXCHANGES",
            "binance,bybit,okx,gate,bitget,mexc,kucoin,bingx,coinex,phemex,cryptocom,htx",
        )
    )
    min_pct: float = field(default_factory=lambda: float(os.getenv("PUMP_MIN_PCT", "30")))
    interval: int = field(default_factory=lambda: int(os.getenv("SCAN_INTERVAL", "60")))
    close_after_misses: int = field(
        default_factory=lambda: int(os.getenv("PUMP_CLOSE_AFTER_MISSES", "3"))
    )
