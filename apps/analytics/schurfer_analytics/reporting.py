"""Shared serialization and presentation helpers for read-only analytics CLIs."""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import fields, is_dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable


class ReportWindowNotStartedError(ValueError):
    """Raised when an exclusive report cutoff does not follow its cohort start."""


def resolve_report_until(
    requested_until: datetime | None,
    generated_at: datetime,
    *,
    cohort_start: datetime,
    report_label: str,
) -> datetime:
    """Resolve a report cutoff and fail with a concise cohort-aware error."""
    until = requested_until or generated_at
    if until <= cohort_start:
        raise ReportWindowNotStartedError(
            f"the registered {report_label} cohort starts at "
            f"{cohort_start.isoformat()}; retry after that time"
        )
    return until


def profit_factor(values: Iterable[float]) -> float | None:
    """Return gross positive returns divided by absolute gross negative returns."""
    normalized = tuple(values)
    loss_magnitude = abs(sum(value for value in normalized if value <= 0))
    if loss_magnitude == 0:
        return None
    return sum(value for value in normalized if value > 0) / loss_magnitude


def normalize_code_revision(value: str) -> str:
    revision = value.strip()
    if not revision:
        raise ValueError("code revision must not be empty")
    return revision


def format_number(
    value: float | None,
    decimals: int = 2,
    *,
    suffix: str = "",
    missing: str = "—",
) -> str:
    return missing if value is None else f"{value:.{decimals}f}{suffix}"


def format_percentage(
    value: float | None,
    decimals: int = 2,
    *,
    missing: str = "—",
) -> str:
    return format_number(value, decimals, suffix="%", missing=missing)


def json_ready(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: json_ready(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [json_ready(item) for item in value]
    return value


def json_dataclass_default(value: Any) -> Any:
    """Expose dataclass fields to ``json.dumps`` without a recursive deep copy."""
    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: getattr(value, field.name) for field in fields(value)}
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def render_dataclass_json(value: Any) -> str:
    """Render the stable indented JSON contract without a recursive ``asdict`` copy."""
    return json.dumps(
        value,
        default=json_dataclass_default,
        indent=2,
        sort_keys=True,
        allow_nan=False,
    )


def canonical_json_array_fingerprint(
    values: Iterable[Any],
    *,
    default: Callable[[Any], Any] | None = None,
) -> str:
    """Hash canonical JSON-array bytes while serializing one value at a time."""
    digest = hashlib.sha256()
    digest.update(b"[")
    for index, value in enumerate(values):
        if index:
            digest.update(b",")
        digest.update(
            json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                default=default,
            ).encode()
        )
    digest.update(b"]")
    return digest.hexdigest()


def markdown_table(headers: tuple[str, ...], rows: list[tuple[Any, ...]]) -> list[str]:
    if not rows:
        return ["_No rows._"]
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    lines.extend(
        "| " + " | ".join(str(cell).replace("|", "\\|") for cell in row) + " |" for row in rows
    )
    return lines


def horizon_label(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes}m"
    if minutes < 1440:
        return f"{minutes // 60}h"
    return f"{minutes // 1440}d"


def parse_utc_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid ISO-8601 datetime: {value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
