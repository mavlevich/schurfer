"""Calibration-only tool for the net-buy accumulation v2 amendment.

This is NOT the final `CONTRACT_VERSION = net_buy_accumulation_discovery_v2`. It is
a separate, outcome-blind calibration instrument that implements the v2 eligibility
rules (see `docs/research/net-buy-accumulation-discovery-v2.md`) and, over a fixed
calibration window, emits per-threshold deduplicated FIRE counts, coverage and
concentration. It never reads a return or PnL, never forms an economic verdict, and
`formal_run=True` hard-fails (`FormalRunLockError`). Its whole job is to feed the
frozen deterministic calibration algorithm the fire rate it needs; the human-frozen
constants (grid, tolerances, margins, MDE, uncertainty) live outside it.

v2 eligibility implemented here (rev.4):

- W (1440) is 100% present, 100% trades_complete, and 100% timely (a bar `m` is
  timely when `created_at(m) <= bucket_end(m) + MAX_FINALIZATION_LAG`, the
  non-backfill guard measured from bucket end).
- B (10080) is 100% present, at least `B_COMPLETENESS_MIN_FRACTION` trades_complete,
  and 100% timely.
- `baseline_daily_activity = mean(activity over present-and-complete B minutes) *
  1440` (mean-of-present, not a partial sum).
- `score_m = sum(net_buy over W) / baseline_daily_activity`.
- `elevated_buy(m)` requires m's own trailing-7d window to be 100% present and at
  least the fraction complete; `score_s = share of W minutes elevated_buy`.

The numeric constants below are candidates / `[artifact pending]`; freezing them is
the amendment's open-decisions step, not this tool's job.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

CALIBRATION_TOOL_VERSION = "net_buy_accumulation_v2_calibration"

# --- frozen v1-inherited structure ------------------------------------------
W_MINUTES = 1440
B_MINUTES = 7 * 1440
COOLDOWN_MINUTES = 24 * 60
BASELINE_ACTIVITY_FLOOR_USD = 100_000.0
BARS_MARKET_TYPE = "linear"
BARS_CAPTURE_VERSION = "v1"

# --- CANDIDATE constants (human-frozen at the amendment step, not here) ------
# Defaults are the draft candidates; the real run passes the frozen values.
DEFAULT_B_COMPLETENESS_MIN_FRACTION = 0.99
DEFAULT_MAX_FINALIZATION_LAG_SECONDS = 120  # [artifact pending] from the lag dist
DEFAULT_THETA_M_GRID = (0.10, 0.15, 0.20, 0.25, 0.30)
DEFAULT_THETA_S_GRID = (0.10, 0.15, 0.20, 0.25, 0.35)

PRIMARY_MAG = "P-MAG"
PRIMARY_SHAPE = "P-SHAPE"


class FormalRunLockError(RuntimeError):
    """Raised if anything asks this calibration tool for a formal run. The formal
    read is locked until the mandatory economics/concentration metrics land in
    their own PR; this instrument only ever produces outcome-blind counts."""


def assert_calibration_only(*, formal_run: bool) -> None:
    if formal_run:
        raise FormalRunLockError(
            "net_buy_accumulation_v2_calibration is calibration-only: it reads no "
            "returns and cannot produce a formal_run. The formal v2 read is locked "
            "until the mandatory metrics land."
        )


@dataclass(frozen=True)
class ThresholdCount:
    """Outcome-blind counts for one (primary, threshold) on the calibration
    window. No returns are read: these are fires and coverage/concentration only."""

    primary: str
    theta: float
    raw_crossings: int
    dedup_fires: int
    distinct_assets: int
    distinct_venues: int
    distinct_weeks: int
    # Concentration: the largest single-cluster share of dedup fires (0..1), a
    # coverage diagnostic that needs no outcomes.
    top_cluster_share: float


@dataclass(frozen=True)
class CalibrationArtifact:
    """The fingerprinted, reproducible calibration output. Carries the grid, every
    input constant, the per-threshold counts and coverage, and the input data
    provenance, so freezing thresholds/window off it is mechanical and auditable."""

    tool_version: str
    generated_at: str
    code_revision: str
    calibration_window_start: str
    calibration_window_end: str
    b_completeness_min_fraction: float
    max_finalization_lag_seconds: int
    baseline_activity_floor_usd: float
    theta_m_grid: tuple[float, ...]
    theta_s_grid: tuple[float, ...]
    cold_bar_manifests: dict[str, str]
    counts: tuple[ThresholdCount, ...]
    coverage: dict[str, Any] = field(default_factory=dict)

    def fingerprint(self) -> str:
        payload = json.dumps(self._canonical(), sort_keys=True, default=str).encode()
        return hashlib.sha256(payload).hexdigest()

    def _canonical(self) -> dict[str, Any]:
        # Exclude generated_at (wall clock) from the fingerprint so the same inputs
        # reproduce the same hash.
        return {
            "tool_version": self.tool_version,
            "code_revision": self.code_revision,
            "calibration_window_start": self.calibration_window_start,
            "calibration_window_end": self.calibration_window_end,
            "b_completeness_min_fraction": self.b_completeness_min_fraction,
            "max_finalization_lag_seconds": self.max_finalization_lag_seconds,
            "baseline_activity_floor_usd": self.baseline_activity_floor_usd,
            "theta_m_grid": list(self.theta_m_grid),
            "theta_s_grid": list(self.theta_s_grid),
            "cold_bar_manifests": self.cold_bar_manifests,
            "counts": [c.__dict__ for c in self.counts],
            "coverage": self.coverage,
        }

    def to_json(self) -> str:
        body = self._canonical()
        body["generated_at"] = self.generated_at
        body["fingerprint_sha256"] = self.fingerprint()
        return json.dumps(body, indent=2, sort_keys=True, default=str)


# --- deterministic calibration algorithm (frozen; steps 1-6 of the amendment) ---

STOP_MIN_RESOLVED_FIRES = 100


def n_target(
    *,
    resolved_floor: int = STOP_MIN_RESOLVED_FIRES,
    expected_unresolved_rate: float,
    sizing_margin: float,
) -> float:
    """Fire target the calibration must reach so the RESOLVED floor (100) is met
    with margin. Calibration counts FIRES, but the floor is 100 RESOLVED, so we
    gross up by the expected unresolved rate and apply `sizing_margin`."""
    if not 0.0 <= expected_unresolved_rate < 1.0:
        raise ValueError("expected_unresolved_rate must be in [0, 1)")
    return resolved_floor / (1.0 - expected_unresolved_rate) * sizing_margin


@dataclass(frozen=True)
class PrimarySelection:
    """The mechanical per-primary output of the frozen algorithm: the chosen
    (most-selective-that-can-clear) threshold and the prospective days it needs at
    its measured fire rate. `too_slow` means no grid threshold reaches the target
    within `MAX_WINDOW_DAYS`."""

    primary: str
    chosen_theta: float | None
    fire_rate_per_day: float | None
    required_window_days: float | None
    too_slow: bool


def select_primary(
    primary: str,
    dedup_fires_by_theta: dict[float, int],
    *,
    calibration_days: float,
    n_target_fires: float,
    max_window_days: float,
) -> PrimarySelection:
    """Steps 2-5 for one primary: measure the fire rate per threshold on the fixed
    calibration window, then choose the LARGEST (most selective) threshold whose
    measured rate can reach `n_target_fires` within `max_window_days`, and report
    the required prospective duration for it. Deterministic; no returns are read."""
    if calibration_days <= 0:
        raise ValueError("calibration_days must be positive")
    best: tuple[float, float, float] | None = None  # (theta, rate, required_days)
    for theta in sorted(dedup_fires_by_theta):  # ascending; keep the largest qualifying
        rate = dedup_fires_by_theta[theta] / calibration_days
        if rate <= 0:
            continue
        required_days = n_target_fires / rate
        if required_days <= max_window_days:
            best = (theta, rate, required_days)  # overwrite -> ends on the largest theta
    if best is None:
        return PrimarySelection(primary, None, None, None, too_slow=True)
    return PrimarySelection(primary, best[0], best[1], best[2], too_slow=False)


@dataclass(frozen=True)
class WindowDecision:
    """Step 6: the overall prospective window. `window_days` is the max of the two
    primaries' required durations (rounded up by the frozen rule). `too_slow` is
    set if EITHER primary cannot reach the target within the ceiling -- the window
    is never hand-extended past it."""

    per_primary: tuple[PrimarySelection, ...]
    window_days: int | None
    too_slow: bool


def decide_window(selections: list[PrimarySelection]) -> WindowDecision:
    if any(s.too_slow for s in selections):
        return WindowDecision(tuple(selections), None, too_slow=True)
    import math

    durations = [s.required_window_days for s in selections if s.required_window_days is not None]
    window_days = math.ceil(max(durations)) if durations else None
    return WindowDecision(tuple(selections), window_days, too_slow=False)


def base_cluster_of(symbol: str) -> str:
    """Cluster key: base ticker, merging bybit/binance. Collision audit lives in
    the report layer; this is only the key."""
    return symbol[:-4].upper() if symbol.upper().endswith("USDT") else symbol.upper()


def iso_week(ts: datetime) -> str:
    iso = ts.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def dedup_cooldown(
    crossings: list[tuple[str, str, datetime]],
    *,
    cooldown_minutes: int = COOLDOWN_MINUTES,
) -> list[tuple[str, str, datetime]]:
    """Apply the 24h minimum-gap reset per (exchange, symbol): keep a crossing only
    when at least the cooldown has elapsed since the last kept fire on the same
    instrument. Input rows are `(exchange, symbol, fire_ts)`."""
    kept: list[tuple[str, str, datetime]] = []
    last: dict[tuple[str, str], datetime] = {}
    for ex, sym, ts in sorted(crossings, key=lambda r: (r[0], r[1], r[2])):
        prev = last.get((ex, sym))
        if prev is not None and ts - prev < timedelta(minutes=cooldown_minutes):
            continue
        kept.append((ex, sym, ts))
        last[(ex, sym)] = ts
    return kept


def summarize_threshold(
    primary: str,
    theta: float,
    raw_crossings: list[tuple[str, str, datetime]],
) -> ThresholdCount:
    """Turn raw crossing rows into a ThresholdCount (dedup + coverage). Outcome-blind."""
    dedup = dedup_cooldown(raw_crossings)
    clusters: dict[str, int] = {}
    venues: set[str] = set()
    weeks: set[str] = set()
    for ex, sym, ts in dedup:
        clusters[base_cluster_of(sym)] = clusters.get(base_cluster_of(sym), 0) + 1
        venues.add(ex)
        weeks.add(iso_week(ts))
    top_share = (max(clusters.values()) / len(dedup)) if dedup else 0.0
    return ThresholdCount(
        primary=primary,
        theta=theta,
        raw_crossings=len(raw_crossings),
        dedup_fires=len(dedup),
        distinct_assets=len(clusters),
        distinct_venues=len(venues),
        distinct_weeks=len(weeks),
        top_cluster_share=top_share,
    )


__all__ = [
    "BASELINE_ACTIVITY_FLOOR_USD",
    "CALIBRATION_TOOL_VERSION",
    "DEFAULT_B_COMPLETENESS_MIN_FRACTION",
    "DEFAULT_MAX_FINALIZATION_LAG_SECONDS",
    "DEFAULT_THETA_M_GRID",
    "DEFAULT_THETA_S_GRID",
    "PRIMARY_MAG",
    "PRIMARY_SHAPE",
    "STOP_MIN_RESOLVED_FIRES",
    "CalibrationArtifact",
    "FormalRunLockError",
    "PrimarySelection",
    "ThresholdCount",
    "WindowDecision",
    "assert_calibration_only",
    "base_cluster_of",
    "decide_window",
    "dedup_cooldown",
    "iso_week",
    "n_target",
    "select_primary",
    "summarize_threshold",
]
