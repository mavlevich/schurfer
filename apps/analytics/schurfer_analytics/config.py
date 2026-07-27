import math
import os
from dataclasses import dataclass, field

from .exchange_registry import DEFAULT_EXCHANGES


def _list(env: str, default: str) -> list[str]:
    val = os.getenv(env) or default
    return [x.strip() for x in val.split(",") if x.strip()]


def _float(env: str, default: float) -> float:
    return float(os.getenv(env) or default)


@dataclass
class Config:
    redis_addr: str = field(default_factory=lambda: os.getenv("REDIS_ADDR", "localhost:6379"))
    db_url: str | None = field(default_factory=lambda: os.getenv("DATABASE_URL"))
    exchanges: list[str] = field(
        default_factory=lambda: _list(
            "PUMP_EXCHANGES",
            ",".join(DEFAULT_EXCHANGES),
        )
    )
    measurement_min_pct: float = field(
        default_factory=lambda: _float("PUMP_MEASUREMENT_MIN_PCT", 20.0)
    )
    entry_min_pct: float = field(
        default_factory=lambda: _float(
            "PUMP_ENTRY_MIN_PCT",
            _float("PUMP_MIN_PCT", 30.0),
        )
    )
    interval: int = field(default_factory=lambda: int(os.getenv("SCAN_INTERVAL", "60")))
    close_after_misses: int = field(
        default_factory=lambda: int(os.getenv("PUMP_CLOSE_AFTER_MISSES", "3"))
    )

    def __post_init__(self) -> None:
        if (
            not math.isfinite(self.measurement_min_pct)
            or not math.isfinite(self.entry_min_pct)
            or self.measurement_min_pct <= 0
            or self.entry_min_pct > 5_000
            or self.measurement_min_pct > self.entry_min_pct
        ):
            raise ValueError(
                "pump thresholds must satisfy "
                "0 < PUMP_MEASUREMENT_MIN_PCT <= PUMP_ENTRY_MIN_PCT <= 5000"
            )
