"""Explicit immutable windows for I25 coverage; discovery globals are never changed."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from . import new_perp_stage1 as s


@dataclass(frozen=True)
class CoverageWindow:
    start_day: str
    cut_day: str

    def __post_init__(self) -> None:
        if (
            date.fromisoformat(self.start_day).isoformat() != self.start_day
            or date.fromisoformat(self.cut_day).isoformat() != self.cut_day
            or self.start_day >= self.cut_day
        ):
            raise ValueError("invalid coverage window")

    @property
    def start_ms(self) -> int:
        return s.day_ms(self.start_day)

    @property
    def cut_ms(self) -> int:
        return s.day_ms(self.cut_day)

    def contains(self, stamp: int) -> bool:
        return self.start_ms <= stamp < self.cut_ms

    def contains_day(self, day: str) -> bool:
        date.fromisoformat(day)
        return self.start_day <= day < self.cut_day

    def cuts(self, t0_ms: int) -> dict[str, Any]:
        entry, exit_ = s.entry_clock(t0_ms)
        return {
            "entry_ms": entry,
            "exit_ms": exit_,
            # X is the end of [X - 1 minute, X), not a new bar start.
            "window_censored": exit_ > self.cut_ms,
            "lookback_censored": entry - s.LOOKBACK_DAYS * s.DAY_MS < self.start_ms,
        }

    def funding_span(self, t0_ms: int) -> tuple[int, int]:
        return (
            max(self.start_ms, t0_ms + s.PATH_FIRST_DAY * s.DAY_MS),
            min(t0_ms + s.PATH_LAST_DAY * s.DAY_MS, self.cut_ms - 1),
        )


HOLDOUT = CoverageWindow("2023-02-01", "2024-09-16")
