"""Shared serialization and presentation helpers for read-only analytics CLIs."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from typing import Any


def json_ready(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: json_ready(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [json_ready(item) for item in value]
    return value


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
