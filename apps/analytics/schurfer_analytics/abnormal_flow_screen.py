"""Frozen contract and pure features for the abnormal-flow economic screen (HYP
discovery family; design: docs/research/abnormal-flow-economic-screen-v1.md).

PRE-REGISTRATION. The feature FORMS here are frozen (owner-frozen 2026-09-20) so the
operationalization cannot be chosen after seeing returns; the numeric THRESHOLDS and
the full run specification (calibration rule, scan lag, entry/exit, matching,
portfolio, verdict) are NOT frozen, and a formal run must refuse to proceed until
every one of them is pinned AND in range (``require_frozen``). This module reads no
returns and makes no economic claim: it holds the contract and the pure per-window
feature maths only.

Primary mechanism (the ONE primary cell): positive within-instrument OI growth over
the prior 60 minutes, aggressive taker-buy notional dominating sell over that same
hour, and restrained price movement (bar high/low, not just closes) before the
decision; long; 720-minute horizon. Novelty is the OI requirement: the same
buy/price signal WITHOUT the OI-growth threshold, over the SAME eligible set, is the
registered ablation, and no incremental OI benefit closes the mechanism.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

CONTRACT_VERSION = "abnormal_flow_screen_v1"

# --- Registered executable rules ---------------------------------------------------
# The formal run may only be pinned to a rule whose behaviour is implemented and
# reviewed here. A free-text label is NOT an executable rule: it lets a run silently
# claim a spec (direction=short, a negative floor, an unbuilt conversion) that the
# code never enforces. Each spec field must be one of these versioned identifiers, so
# the frozen contract names exactly the code path the replay will execute.
CALIBRATION_RULES = frozenset({"fixed_percentiles_on_prestart_window_v1"})
OI_USD_CONVERSION_RULES = frozenset({"bybit_native_value_binance_amount_x_decision_price_v1"})
ENTRY_REFERENCES = frozenset({"next_bar_open_priced_proxy_v1"})
EXIT_REFERENCES = frozenset({"horizon_bar_close_priced_proxy_v1"})
MATCHING_RULES = frozenset({"same_venue_regime_liquidity_pricemove_band_v1"})
FUNDING_MODELS = frozenset({"conservative_8h_v1"})

# An input fingerprint pins the exact frozen dataset the replay is allowed to read:
# the audit's aggregate output hash, optionally namespaced (``algo:<64 hex>`` or a
# bare 64-hex SHA-256). A run whose live inputs hash to anything else must refuse.
_FINGERPRINT_RE = re.compile(r"^(?:[a-z0-9_.-]+:)?[0-9a-f]{64}$")

# --- Frozen FORMS (not thresholds) -------------------------------------------------

LOOKBACK_MINUTES = 60
OUTCOME_HORIZON_MINUTES = 720
DIRECTION = "long"
# The episode cooldown is at least the outcome horizon so overlapping fires on one
# instrument are not counted as independent evidence.
COOLDOWN_MINUTES = OUTCOME_HORIZON_MINUTES


def _finite(x: float | None) -> bool:
    return isinstance(x, int | float) and not isinstance(x, bool) and math.isfinite(x)


def oi_growth_pct_60m(oi_start: float, oi_end: float) -> float | None:
    """Within-instrument OI growth as a PERCENT of native OI amount over the 60m
    lookback: ``(oi_end - oi_start) / oi_start * 100``. No cross-venue comparison and
    no trailing z-score (frozen form). ``None`` (window unavailable) when either value
    is non-finite (NaN/Inf), the start is not a usable positive base, or the end is
    negative, so corrupt data never becomes a misleading percentage."""
    if not _finite(oi_start) or not _finite(oi_end) or oi_start <= 0 or oi_end < 0:
        return None
    return (oi_end - oi_start) / oi_start * 100.0


def buy_pressure_ratio_60m(buy_notional_usd: float, sell_notional_usd: float) -> float | None:
    """Taker buy pressure over the 60m lookback: ``buy / (buy + sell)`` in USD
    notional. ``None`` for a non-finite input, a negative value, or a zero-flow window
    (the ratio is undefined and the window is explicitly unavailable, never silently 0
    or 0.5). Frozen form."""
    if not _finite(buy_notional_usd) or not _finite(sell_notional_usd):
        return None
    if buy_notional_usd < 0 or sell_notional_usd < 0:
        return None
    total = buy_notional_usd + sell_notional_usd
    if total <= 0:
        return None
    return buy_notional_usd / total


def price_containment_max_bar_dev(
    open_price: float, bars: Sequence[tuple[float, float]]
) -> float | None:
    """Price restraint over the lookback as the MAXIMUM absolute deviation of the
    within-window bar EXTREMES (high and low of each 1m bar) from the window's OPENING
    price, as a fraction of that open: ``max over bars of max(|high-open|,|low-open|)
    / open``.

    Bar extremes, not closes: a strong intraminute wick that closes back near the open
    (e.g. SAYLORMOON) is NOT restrained, and closes-only would miss it (chosen here,
    before any returns were read). ``None`` (unavailable) when the open is not a usable
    positive base, there are no bars, or any high/low is non-finite. Frozen form."""
    if not _finite(open_price) or open_price <= 0 or not bars:
        return None
    worst = 0.0
    for high, low in bars:
        if not _finite(high) or not _finite(low):
            return None
        dev = max(abs(high - open_price), abs(low - open_price)) / open_price
        if dev > worst:
            worst = dev
    return worst


# --- Contract (thresholds + run spec NOT frozen; fail-closed until pinned) ----------


@dataclass(frozen=True)
class AbnormalFlowContract:
    """The full screen contract. Every numeric/spec field below defaults to ``None``
    and is DELIBERATELY unset: it must be pinned by one pre-declared outcome-blind rule
    before any returns are read. ``require_frozen`` fail-closes a formal run until every
    field is set AND within its declared range, so a run can never quietly select or
    corrupt (NaN/negative/out-of-range) the parameters it is scoring."""

    contract_version: str = CONTRACT_VERSION
    # Frozen forms (validated as unchanged, not merely present).
    lookback_minutes: int = LOOKBACK_MINUTES
    outcome_horizon_minutes: int = OUTCOME_HORIZON_MINUTES
    cooldown_minutes: int = COOLDOWN_MINUTES
    direction: str = DIRECTION

    # Primary-cell thresholds (frozen later, outcome-blind).
    min_oi_growth_pct: float | None = None
    min_buy_pressure_ratio: float | None = None
    max_price_containment: float | None = None

    # Eligibility floor. min_oi_notional_usd needs a per-venue native-OI -> USD rule at
    # a point-in-time price. Verified per venue: Bybit publishes both native OI amount
    # and a USD open-interest value (use the native USD value); Binance publishes only
    # the base-asset OI amount with no USD value, so its USD OI is amount x the
    # decision-time price. The single registered rule that does exactly this is
    # ``bybit_native_value_binance_amount_x_decision_price_v1``; no venue is ever
    # treated as zero or given an unversioned conversion.
    min_oi_notional_usd: float | None = None
    oi_usd_conversion_rule: str | None = None
    position_usd: float | None = None
    max_participation_frac: float | None = None
    # Participation = position_usd / turnover accumulated over the pre-decision window
    # of this many minutes ENDING at the decision (a realistic short fill period), not
    # the whole 60m lookback and never the future entry minute. Must be < the lookback.
    entry_execution_window_minutes: int | None = None
    # OI freshness ceilings (seconds): the OI observation backing a decision must be no
    # older than this, per venue, so a decision never rests on a day-old OI print.
    # Separate per venue because Bybit and Binance publish OI on different cadences.
    oi_freshness_limit_seconds_bybit: int | None = None
    oi_freshness_limit_seconds_binance: int | None = None

    # Registered run specification the formal run must satisfy in full. The rule fields
    # are versioned identifiers from the registries above, not free text.
    calibration_rule: str | None = None
    calibration_window_days: int | None = None
    scan_lag_minutes: int | None = None
    entry_reference: str | None = None
    exit_reference: str | None = None
    matching_rule: str | None = None
    # Matched controls requested per fired episode. Distinct from portfolio_max_slots:
    # this sizes the control group, the portfolio slot count sizes concurrent capital.
    controls_per_episode: int | None = None
    portfolio_bank_usd: float | None = None
    portfolio_max_slots: int | None = None

    # Literal UTC window the replay is registered to read, and the input fingerprint it
    # must reproduce. Both boundaries are tz-aware UTC ISO-8601 with start < end; the
    # fingerprint is the outcome-blind audit's aggregate hash. A run over any other
    # window or dataset must refuse, so the scored window cannot be chosen from results.
    window_start_utc: str | None = None
    window_end_utc: str | None = None
    input_fingerprint: str | None = None

    # Conservative execution/cost model (priced-proxy entry/exit).
    taker_fee_bps: float | None = None
    entry_slippage_bps: float | None = None
    exit_slippage_bps: float | None = None
    funding_bps_720m_binance: float | None = None
    funding_bps_720m_bybit: float | None = None

    # Evidence / missingness gates and the one-shot verdict inputs.
    min_resolved_episodes: int | None = None
    max_missing_fraction: float | None = None
    min_excess_over_control_pct: float | None = None
    min_distinct_assets: int | None = None
    min_utc_weeks: int | None = None
    max_episodes_per_asset_frac: float | None = None
    max_episodes_per_week_frac: float | None = None
    min_control_coverage_frac: float | None = None

    # Diagnostics-only (v2 nomination, excluded from PASS v1)

    def problems(self) -> tuple[str, ...]:
        """Every reason this contract is not a valid frozen spec: unset fields, frozen
        forms that were altered, and out-of-range or non-finite numbers."""
        issues: list[str] = []

        # Frozen forms must be exactly the pre-registered ones.
        if self.lookback_minutes != LOOKBACK_MINUTES:
            issues.append(f"lookback_minutes must be {LOOKBACK_MINUTES}")
        if self.outcome_horizon_minutes != OUTCOME_HORIZON_MINUTES:
            issues.append(f"outcome_horizon_minutes must be {OUTCOME_HORIZON_MINUTES}")
        if self.cooldown_minutes < self.outcome_horizon_minutes:
            issues.append("cooldown_minutes must be >= outcome_horizon_minutes")
        if self.direction != DIRECTION:
            issues.append(f"direction must be {DIRECTION!r}")

        def num(
            name: str,
            *,
            low: float | None = None,
            high: float | None = None,
            inclusive_low: bool = True,
            inclusive_high: bool = True,
        ) -> None:
            v = getattr(self, name)
            if v is None:
                issues.append(f"{name} is not set")
                return
            if not _finite(v):
                issues.append(f"{name} must be a finite number")
                return
            if low is not None and (v < low or (v == low and not inclusive_low)):
                issues.append(
                    f"{name} must be > {low}" if not inclusive_low else f"{name} must be >= {low}"
                )
            if high is not None and (v > high or (v == high and not inclusive_high)):
                issues.append(
                    f"{name} must be < {high}"
                    if not inclusive_high
                    else f"{name} must be <= {high}"
                )

        def integer(name: str, *, low: int) -> None:
            v = getattr(self, name)
            if v is None:
                issues.append(f"{name} is not set")
            elif not isinstance(v, int) or isinstance(v, bool) or v < low:
                issues.append(f"{name} must be an integer >= {low}")

        def registered(name: str, registry: frozenset[str]) -> None:
            v = getattr(self, name)
            if not isinstance(v, str) or not v:
                issues.append(f"{name} is not set")
            elif v not in registry:
                issues.append(
                    f"{name} must be a registered executable rule, one of "
                    f"{sorted(registry)}; free text is not a spec"
                )

        def utc_boundary(name: str) -> datetime | None:
            v = getattr(self, name)
            if not isinstance(v, str) or not v:
                issues.append(f"{name} is not set")
                return None
            try:
                parsed = datetime.fromisoformat(v.replace("Z", "+00:00"))
            except ValueError:
                issues.append(f"{name} must be an ISO-8601 datetime")
                return None
            if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
                issues.append(f"{name} must be an explicit UTC instant")
                return None
            return parsed

        num("min_oi_growth_pct", low=0.0, inclusive_low=False)
        num("min_buy_pressure_ratio", low=0.5, high=1.0, inclusive_low=False)
        num("max_price_containment", low=0.0, inclusive_low=False)
        num("min_oi_notional_usd", low=0.0, inclusive_low=False)
        registered("oi_usd_conversion_rule", OI_USD_CONVERSION_RULES)
        num("position_usd", low=0.0, inclusive_low=False)
        num("max_participation_frac", low=0.0, high=1.0, inclusive_low=False)
        integer("entry_execution_window_minutes", low=1)
        integer("oi_freshness_limit_seconds_bybit", low=1)
        integer("oi_freshness_limit_seconds_binance", low=1)
        if (
            isinstance(self.entry_execution_window_minutes, int)
            and not isinstance(self.entry_execution_window_minutes, bool)
            and self.entry_execution_window_minutes >= self.lookback_minutes
        ):
            issues.append(
                "entry_execution_window_minutes must be < lookback_minutes "
                "(participation uses a short pre-decision window, not the whole hour)"
            )
        registered("calibration_rule", CALIBRATION_RULES)
        integer("calibration_window_days", low=1)
        integer("scan_lag_minutes", low=0)
        registered("entry_reference", ENTRY_REFERENCES)
        registered("exit_reference", EXIT_REFERENCES)
        registered("matching_rule", MATCHING_RULES)
        integer("controls_per_episode", low=1)
        num("portfolio_bank_usd", low=0.0, inclusive_low=False)
        integer("portfolio_max_slots", low=1)

        num("min_resolved_episodes", low=1.0, inclusive_low=True)
        num("max_missing_fraction", low=0.0, high=1.0, inclusive_low=True, inclusive_high=True)
        num("min_excess_over_control_pct", low=0.0, inclusive_low=True)
        integer("min_distinct_assets", low=1)
        integer("min_utc_weeks", low=1)
        num(
            "max_episodes_per_asset_frac",
            low=0.0,
            high=1.0,
            inclusive_low=False,
            inclusive_high=True,
        )
        num(
            "max_episodes_per_week_frac",
            low=0.0,
            high=1.0,
            inclusive_low=False,
            inclusive_high=True,
        )
        num(
            "min_control_coverage_frac", low=0.0, high=1.0, inclusive_low=False, inclusive_high=True
        )

        num("taker_fee_bps", low=0.0)
        num("entry_slippage_bps", low=0.0)
        num("exit_slippage_bps", low=0.0)
        num("funding_bps_720m_binance", low=0.0)
        num("funding_bps_720m_bybit", low=0.0)
        integer("min_resolved_episodes", low=1)
        num("max_missing_fraction", low=0.0, high=1.0)
        num("min_excess_over_control_pct", low=0.0)

        start = utc_boundary("window_start_utc")
        end = utc_boundary("window_end_utc")
        if start is not None and end is not None and start >= end:
            issues.append("window_start_utc must be strictly before window_end_utc")
        fp = self.input_fingerprint
        if not isinstance(fp, str) or not fp:
            issues.append("input_fingerprint is not set")
        elif not _FINGERPRINT_RE.match(fp):
            issues.append("input_fingerprint must be a sha256 hex, optionally 'algo:'-prefixed")

        return tuple(issues)

    def is_frozen(self) -> bool:
        return not self.problems()

    def require_frozen(self) -> None:
        """Raise unless every threshold/spec field is pinned AND in range. A formal,
        returns-reading run calls this first, so parameters can never be chosen from,
        or corrupted before, the results."""
        problems = self.problems()
        if problems:
            raise NotFrozenError(
                "abnormal-flow contract is not a valid frozen spec; refusing a formal "
                "run until the pre-declared outcome-blind rule pins/repairs: " + "; ".join(problems)
            )

    def compute_hash(self) -> str:
        """Canonical SHA-256 hash of the fully frozen contract configuration."""
        import dataclasses
        import hashlib
        import json

        d = dataclasses.asdict(self)
        d.pop("contract_hash", None)
        encoded = json.dumps(d, separators=(",", ":"), sort_keys=True).encode("utf-8")
        return f"sha256:{hashlib.sha256(encoded).hexdigest()}"

    def to_json(self) -> str:
        """Serialize this contract to JSON, including its self-computed hash."""
        import dataclasses
        import json

        d = dataclasses.asdict(self)
        d["contract_hash"] = self.compute_hash()
        return json.dumps(d, indent=2, sort_keys=True) + "\n"

    @classmethod
    def from_json(cls, json_str: str) -> AbnormalFlowContract:
        """Deserialize from JSON and verify the canonical hash."""
        import json

        d = json.loads(json_str)
        expected_hash = d.pop("contract_hash", None)
        obj = cls(**d)
        if expected_hash is not None and obj.compute_hash() != expected_hash:
            raise ValueError(
                f"Contract hash mismatch: expected {expected_hash}, got {obj.compute_hash()}"
            )

        return obj


class NotFrozenError(RuntimeError):
    """A formal (returns-reading) run was attempted before the contract's thresholds
    and run specification were fully pinned and validated. Discovery/scanning that
    reads no returns does not raise this."""
