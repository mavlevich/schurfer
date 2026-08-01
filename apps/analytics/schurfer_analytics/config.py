import math
import os
from dataclasses import dataclass, field

from .exchange_registry import DEFAULT_EXCHANGES


def _list(env: str, default: str) -> list[str]:
    val = os.getenv(env) or default
    return [x.strip() for x in val.split(",") if x.strip()]


def _float(env: str, default: float) -> float:
    return float(os.getenv(env) or default)


def _bool(env: str, default: bool) -> bool:
    value = os.getenv(env)
    if value is None or value == "":
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{env} must be a boolean value")


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
    source_lead_capture_enabled: bool = field(
        default_factory=lambda: _bool("SOURCE_LEAD_CAPTURE_ENABLED", True)
    )
    source_lead_targets: tuple[str, ...] = field(
        default_factory=lambda: tuple(_list("SOURCE_LEAD_TARGET_EXCHANGES", "binance,bybit"))
    )
    source_lead_notional_usd: float = field(
        default_factory=lambda: _float("SOURCE_LEAD_NOTIONAL_USD", 50.0)
    )
    source_lead_timeout_seconds: float = field(
        default_factory=lambda: _float("SOURCE_LEAD_TIMEOUT_SECONDS", 5.0)
    )
    source_lead_batch_size: int = field(
        default_factory=lambda: int(os.getenv("SOURCE_LEAD_BATCH_SIZE", "8"))
    )
    source_lead_queue_size: int = field(
        default_factory=lambda: int(os.getenv("SOURCE_LEAD_QUEUE_SIZE", "16"))
    )
    source_lead_shutdown_timeout_seconds: float = field(
        default_factory=lambda: _float("SOURCE_LEAD_SHUTDOWN_TIMEOUT_SECONDS", 10.0)
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
        if (
            not self.source_lead_targets
            or any(target not in {"binance", "bybit"} for target in self.source_lead_targets)
            or len(set(self.source_lead_targets)) != len(self.source_lead_targets)
        ):
            raise ValueError("source-lead targets must be unique Binance/Bybit exchanges")
        if (
            not math.isfinite(self.source_lead_notional_usd)
            or self.source_lead_notional_usd <= 0
            or not math.isfinite(self.source_lead_timeout_seconds)
            or self.source_lead_timeout_seconds <= 0
            or self.source_lead_batch_size <= 0
            or self.source_lead_queue_size <= 0
            or not math.isfinite(self.source_lead_shutdown_timeout_seconds)
            or self.source_lead_shutdown_timeout_seconds <= 0
        ):
            raise ValueError("source-lead capture bounds must be positive and finite")
