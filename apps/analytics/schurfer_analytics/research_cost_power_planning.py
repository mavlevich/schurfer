"""Offline cost, power and accrual planning for a future research line.

This is a planning calculation. It registers no signal, reads no active cohort and makes
no database, exchange or production call. It reads only saved, hash-verified artifacts:

- **Costs:** the pre-blind book-cost result (#477-#479). Its groups stay separate: paper
  entry/exit books versus the source-lead same-book crossing, version, venue, notional,
  spread bucket and quote freshness. The threshold is the registered
  `break_even_mid_move_bps` (ask and bid VWAP impacts, which already contain the
  half-spreads, plus a fee scenario per side). The spread is never added again.
- **Dispersion:** the saved inputs of HYP-012b (discovery, formal venues), HYP-012c
  (holdout, in-band candidates only) and HYP-029 (September legs), replayed with each
  study's own evaluation code. The replay must reproduce the published counts and means,
  or the input is refused. Each dataset is a separate dispersion scenario; none is the
  distribution of a future signal. HYP-012c evaluates exactly the in-band candidates its
  formal read evaluated; every other holdout candidate stays unread.
- **Flow:** the published accrual counters of the v2 identity audit, as an illustration
  only.

Power is simulated by resampling whole clusters (assets, and UTC days as a time-dependence
check) of centered historical net returns with a fixed effect added. A simulated cohort
passes when its mean is positive and the lower bound of a cluster-robust 95% interval is
above zero, which approximates the registered asset-cluster bootstrap rule. A smaller
calibration runs the registered `cluster_bootstrap_mean` on the same simulated cohorts at
zero and at a positive effect, and reports how often the two rules agree.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from statistics import NormalDist, fmean, pstdev
from typing import TYPE_CHECKING, Any

from . import mexc_early_trigger_hyp029 as hyp029
from . import source_lead_gap_hyp012c as hyp012c
from . import source_lead_multi_source as hyp012b
from .clustered_inference import ClusterObservation, cluster_bootstrap_mean, derived_seed
from .preblind_book_cost_baseline import FEE_SCENARIOS_BPS
from .preblind_book_cost_baseline import READER_VERSION as PREBLIND_READER_VERSION
from .reporting import normalize_code_revision
from .source_lead_multi_source_report import _candidate_from_json, outcome_for

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

REPORT_VERSION = "research_cost_power_planning_v1"
DEFAULT_ARTIFACT_ROOT = Path("runtime/research")
DEFAULT_AUDIT_DOC = Path("docs/research/source-lead-v2-identity-accrual-audit-2026-10-01.md")

# Net effects after all trading costs, fixed before any simulation ran.
EFFECT_GRID_BPS: tuple[float, ...] = (10.0, 25.0, 50.0, 100.0)
POWER_TARGETS: tuple[float, ...] = (0.8, 0.9)
# A positive mean with a 95% two-sided lower bound above zero (the HYP-029 and v2 style).
ONE_SIDED_ALPHA = 0.025
EVIDENCE_FLOOR_RESOLVED = 100  # AI_RULES default registered evidence floor
# Roughly 25% steps, so a required size is located within one step.
SAMPLE_SIZE_GRID: tuple[int, ...] = (
    50, 75, 100, 125, 150, 200, 250, 300, 400, 500, 600, 800, 1_000, 1_200, 1_500,
    2_000, 2_500, 3_000, 4_000, 5_000, 6_000, 8_000, 10_000, 12_000, 15_000, 20_000,
)  # fmt: skip
SIMULATION_REPLICATES = 1_000
CALIBRATION_REPLICATES = 200
CALIBRATION_BOOTSTRAP_ITERATIONS = 1_000
CALIBRATION_EFFECT_BPS = 50.0
CALIBRATION_MAX_N = 1_200
# The registered floor, where heavy tails matter most, and the 50 bps / 80% size.
SIMULATION_SEED = 20_261_001
CLUSTER_SCHEMES: tuple[str, ...] = ("asset", "utc_day")
# Fewer clusters than this makes a resampled distribution too coarse to rely on.
MIN_SIMULATION_CLUSTERS = 20
MIN_WEEKS_FOR_WEEK_DEPENDENCE = 8
REPEAT_SENSITIVITY_EPISODES_PER_CLUSTER: tuple[int, ...] = (1, 2, 4, 8)
REPLAY_TOLERANCE_PCT = 1e-9

# Capacity: the pre-blind read measured only USD 50 books.
UNMEASURED_NOTIONALS_USD: tuple[float, ...] = (500.0, 5_000.0)
# Extra costs the book read does not observe; scenarios, never measurements.
QUOTE_TO_FILL_SCENARIOS_BPS: tuple[float, ...] = (0.0, 15.0, 30.0)  # v2 exit slippage grid
FUNDING_SCENARIOS_BPS_PER_8H: tuple[float, ...] = (0.0, hyp012b.FUNDING_BPS_PER_8H)
HOLD_SCENARIOS_MINUTES: tuple[int, ...] = (30, 60)

# Calendar and economics scenarios. The historical rate is filled in from the audit.
FLOW_SCENARIOS_PER_DAY: tuple[float, ...] = (0.0, 0.5, 1.0, 3.0, 5.0)
RESOLVED_FRACTION_SCENARIOS: tuple[float, ...] = (1.0, 0.9, 0.7)
REJECTION_FRACTION_SCENARIOS: tuple[float, ...] = (0.0, 0.2)
MAX_CONCURRENT_SCENARIOS: tuple[int, ...] = (1, 3)
CALENDAR_HORIZONS_DAYS: tuple[int, ...] = (91, 183)  # about 3 and 6 months
DAYS_PER_MONTH = 30.44
# Not agreed yet: shown as scenarios, no single threshold is chosen.
MONTHLY_OPERATING_COST_SCENARIOS_USD: tuple[float, ...] = (0.0, 10.0, 25.0)
MONTHLY_TARGET_RESULT_SCENARIOS_USD: tuple[float, ...] = (0.0, 10.0, 50.0)

# Published v2 audit counters: Gate identities with a registered Bybit target,
# source-eligible captures in the v4 candidate window 2026-09-03 .. 2026-09-25T20:00Z.
AUDIT_ROW_MARKER = "| Candidate window, 2026-09-03 to 2026-09-25 20:00Z | 1,728 | 952 | 133 | 42 |"
AUDIT_SOURCE_ELIGIBLE_CAPTURES = 42
AUDIT_WINDOW_DAYS = (
    datetime(2026, 9, 25, 20, tzinfo=UTC) - datetime(2026, 9, 3, tzinfo=UTC)
).total_seconds() / 86_400


class ArtifactIntegrityError(ValueError):
    """A saved artifact exists but cannot be trusted; the report refuses to run."""


@dataclass(frozen=True)
class ArtifactSpec:
    key: str
    relative_path: str
    registered_sha256: str | None
    provenance: str


ARTIFACTS: tuple[ArtifactSpec, ...] = (
    ArtifactSpec(
        "preblind_result",
        "preblind-book-cost-baseline/result.json",
        "d5505fadc046347b02e851b5ea169251ae2f12df0f409451dd801d7bb7828b39",
        "docs/research/preblind-book-cost-baseline-v1-readout.md",
    ),
    ArtifactSpec(
        "hyp012b_inputs",
        "hyp012b/discovery/inputs.json",
        None,  # pinned by the discovery result's inputs_sha256
        "docs/research/discovery-ledger.md (HYP-012b)",
    ),
    ArtifactSpec(
        "hyp012b_result",
        "hyp012b/discovery/result.json",
        "8a6e0865aad8c8b85571a6e1d49eb8bd44d6dd1a4af18970731446b3d9d74d08",
        "docs/research/discovery-ledger.md (HYP-012b)",
    ),
    ArtifactSpec(
        "hyp012c_inputs",
        "hyp012c/holdout/inputs.json",
        "8cca5341129c515f290887cda347393d66cd01e91dd38a2abf8a726aeb7df6d0",
        "docs/research/discovery-ledger.md (HYP-012c)",
    ),
    ArtifactSpec(
        "hyp012c_result",
        "hyp012c/holdout/result.json",
        "bbe76b757ab9b6be2a208636e89d605cd582fd44b868b138b21d0f29333f9560",
        "docs/research/discovery-ledger.md (HYP-012c)",
    ),
    ArtifactSpec(
        "hyp029_inputs",
        "hyp029/inputs.json",
        "def8d5f1742b9314cdadc174f94cd2e60e02b8673cff72ef3cb4c9d15ced56ea",
        "docs/research/discovery-ledger.md (HYP-029)",
    ),
    ArtifactSpec(
        "hyp029_result",
        "hyp029/result.json",
        "3fda59f79ebe701ec4095b88e7059bdcaf4be4476f01b3e4a3add8501c11e7b7",
        "docs/research/discovery-ledger.md (HYP-029)",
    ),
)


@dataclass(frozen=True)
class LoadedArtifact:
    spec: ArtifactSpec
    status: str  # "verified" or "missing"
    sha256: str | None
    size_bytes: int | None
    payload: dict[str, Any] | None

    def fingerprint(self) -> dict[str, Any]:
        return {
            "key": self.spec.key,
            "path": self.spec.relative_path,
            "status": self.status,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "registered_sha256": self.spec.registered_sha256,
            "provenance": self.spec.provenance,
        }


def _sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def load_artifact(root: Path, spec: ArtifactSpec) -> LoadedArtifact:
    """A missing file is an explicit gap. A present file must match its `.sha256`
    sidecar and the registered digest and parse as a JSON object, or it is refused."""
    path = root / spec.relative_path
    if not path.exists():
        return LoadedArtifact(spec, "missing", None, None, None)
    body = path.read_bytes()
    digest = _sha256(body)
    sidecar = path.with_name(path.name + ".sha256")
    if not sidecar.exists():
        raise ArtifactIntegrityError(f"{spec.relative_path} has no .sha256 sidecar")
    stored = sidecar.read_text(encoding="utf-8").split()
    if not stored or stored[0] != digest:
        raise ArtifactIntegrityError(f"{spec.relative_path} does not match its sidecar")
    if spec.registered_sha256 is not None and digest != spec.registered_sha256:
        raise ArtifactIntegrityError(f"{spec.relative_path} is not the registered artifact")
    try:
        payload = json.loads(body)
    except ValueError as error:
        raise ArtifactIntegrityError(f"{spec.relative_path} is not valid JSON") from error
    if not isinstance(payload, dict):
        raise ArtifactIntegrityError(f"{spec.relative_path} is not a JSON object")
    return LoadedArtifact(spec, "verified", digest, len(body), payload)


def load_artifacts(
    root: Path, specs: Sequence[ArtifactSpec] = ARTIFACTS
) -> dict[str, LoadedArtifact]:
    return {spec.key: load_artifact(root, spec) for spec in specs}


# --- Trading break-even thresholds -------------------------------------------------------


def trading_thresholds(preblind: dict[str, Any] | None) -> dict[str, Any]:
    """Per registered group: the stored break-even distributions at each fee scenario.
    Only impacts and fees enter the threshold; the entry spread labels the bucket."""
    if preblind is None:
        return {
            "status": "missing",
            "missing_measurement": "pre-blind book-cost result artifact",
            "groups": [],
            "capacity": _capacity([]),
        }
    if preblind.get("reader_version") != PREBLIND_READER_VERSION:
        raise ArtifactIntegrityError("pre-blind result has another reader version")
    groups = []
    for group in preblind["groups"]:
        thresholds = group["break_even_mid_move_bps"]
        groups.append(
            {
                "population": group["population"],
                "version": group["version"],
                "venue": group["venue"],
                "requested_notional": group["requested_notional"],
                "entry_spread_bucket": group["entry_spread_bucket"],
                "quote_age_quality": group["quote_age_quality"],
                "n": thresholds[f"fee_{FEE_SCENARIOS_BPS[0]:g}"]["n"],
                "break_even_bps": {
                    f"fee_{fee:g}": {
                        key: thresholds[f"fee_{fee:g}"][key]
                        for key in ("mean", "p50", "p90", "p99", "max")
                    }
                    for fee in FEE_SCENARIOS_BPS
                },
            }
        )
    return {
        "status": "verified",
        "window": [preblind["window_start"], preblind["window_end"]],
        "fee_scenarios_bps_per_side": list(FEE_SCENARIOS_BPS),
        "groups": groups,
        "capacity": _capacity(groups),
        "quote_age_unknown_groups": sum(g["quote_age_quality"] == "unknown" for g in groups),
    }


def _notional_usd(label: str) -> float | None:
    try:
        return float(label.removeprefix("$"))
    except ValueError:
        return None


def _capacity(groups: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    measured = sorted(
        {n for g in groups if (n := _notional_usd(str(g["requested_notional"]))) is not None}
    )
    rows: list[dict[str, Any]] = [
        {"notional_usd": n, "status": "measured_book_quotes"} for n in measured
    ]
    rows.extend(
        {"notional_usd": n, "status": "capacity_not_measured"}
        for n in UNMEASURED_NOTIONALS_USD
        if n not in measured
    )
    return rows


def unobserved_cost_scenarios() -> list[dict[str, Any]]:
    """Costs outside the book read, as additive bps per round trip. Not measured."""
    rows = []
    for hold in HOLD_SCENARIOS_MINUTES:
        for funding in FUNDING_SCENARIOS_BPS_PER_8H:
            for quote_to_fill in QUOTE_TO_FILL_SCENARIOS_BPS:
                funding_bps = funding * hold / 480
                rows.append(
                    {
                        "hold_minutes": hold,
                        "funding_bps_per_8h": funding,
                        "funding_bps": funding_bps,
                        "quote_to_fill_bps": quote_to_fill,
                        "additional_bps": funding_bps + quote_to_fill,
                    }
                )
    return rows


# --- Dispersion datasets -----------------------------------------------------------------


@dataclass(frozen=True)
class Episode:
    cluster: str
    at: datetime
    net_bps: float


def _require_pin(inputs: LoadedArtifact, result: LoadedArtifact) -> None:
    assert result.payload is not None
    if result.payload.get("inputs_sha256") != inputs.sha256:
        raise ArtifactIntegrityError(f"{result.spec.key} pins another inputs file")


def _require_close(label: str, replayed: float | None, published: float | None) -> None:
    if replayed is None or published is None:
        if replayed is not published:
            raise ArtifactIntegrityError(f"{label}: replay differs from the published result")
        return
    if abs(replayed - published) > REPLAY_TOLERANCE_PCT:
        raise ArtifactIntegrityError(f"{label}: replay differs from the published result")


def hyp029_episodes(inputs: dict[str, Any], result: dict[str, Any]) -> list[Episode]:
    """The registered September legs, net of the registered primary cost."""
    if inputs.get("family_version") != hyp029.FAMILY_VERSION:
        raise ArtifactIntegrityError("HYP-029 inputs of another family")
    episodes = []
    for leg in inputs["legs"]:
        gross, _status = hyp029.leg_return(leg, hyp029.ENTRY_DELAY)
        if gross is None:
            continue
        at = datetime.fromtimestamp(leg["close_t"] + hyp029.ENTRY_DELAY, UTC)
        episodes.append(Episode(leg["symbol"], at, (gross - hyp029.COST_PRIMARY_PCT) * 100))
    if len(episodes) != result["legs"]:
        raise ArtifactIntegrityError("HYP-029: replayed legs differ from the published count")
    replayed = fmean(e.net_bps for e in episodes) / 100 if episodes else None
    _require_close("HYP-029", replayed, result["net_mean_pct_primary_cost"])
    return episodes


def hyp012b_episodes(inputs: dict[str, Any], result: dict[str, Any]) -> list[Episode]:
    """Discovery outcomes of the formal venues, pooled. Discovery evaluated all of them."""
    if result.get("family_version") != hyp012b.FAMILY_VERSION or result.get("stage") != (
        "discovery"
    ):
        raise ArtifactIntegrityError("not a HYP-012b discovery result")
    if inputs.get("stage") != "discovery":
        raise ArtifactIntegrityError("HYP-012b inputs of another stage")
    formal = list(result["tested_family"])
    by_source: dict[str, list[Episode]] = defaultdict(list)
    for candidate in map(_candidate_from_json, inputs["candidates"]):
        if candidate.source_exchange not in formal:
            continue
        key = str(candidate.event_id)
        outcome = outcome_for(candidate, inputs["bars"].get(key), key in inputs["bars"])
        if outcome.resolved and outcome.net_return_pct is not None:
            by_source[candidate.source_exchange].append(
                Episode(candidate.cluster_key, candidate.source_at, outcome.net_return_pct * 100)
            )
    for published in result["formal_results"]:
        mine = by_source.get(published["source"], [])
        if len(mine) != published["resolved"]:
            raise ArtifactIntegrityError(f"HYP-012b {published['source']}: resolved differs")
        if published["mean_net_pct"] is not None:
            replayed = fmean(e.net_bps for e in mine) / 100 if mine else None
            _require_close(f"HYP-012b {published['source']}", replayed, published["mean_net_pct"])
    return sorted(
        (e for source in formal for e in by_source.get(source, [])),
        key=lambda e: (e.at, e.cluster, e.net_bps),
    )


def hyp012c_episodes(inputs: dict[str, Any], result: dict[str, Any]) -> list[Episode]:
    """Only the in-band candidates the formal read evaluated. Every other holdout
    candidate is skipped before any bar is turned into a return."""
    if result.get("family_version") != hyp012c.FAMILY_VERSION:
        raise ArtifactIntegrityError("not a HYP-012c result")
    if inputs.get("stage") != hyp012c.STAGE:
        raise ArtifactIntegrityError("HYP-012c inputs of another stage")
    episodes = []
    for candidate in map(_candidate_from_json, inputs["candidates"]):
        if candidate.source_exchange not in hyp012c.SOURCES:
            continue
        key = str(candidate.event_id)
        if hyp012c.band_status(candidate, inputs["bars"].get(key)) != "in_band":
            continue
        outcome = outcome_for(candidate, inputs["bars"].get(key), key in inputs["bars"])
        if outcome.resolved and outcome.net_return_pct is not None:
            episodes.append(
                Episode(candidate.cluster_key, candidate.source_at, outcome.net_return_pct * 100)
            )
    pooled = result["pooled"]
    if len(episodes) != pooled["resolved"]:
        raise ArtifactIntegrityError("HYP-012c: replayed resolved count differs")
    if pooled["mean_net_pct"] is not None:
        replayed = fmean(e.net_bps for e in episodes) / 100 if episodes else None
        _require_close("HYP-012c", replayed, pooled["mean_net_pct"])
    return episodes


@dataclass(frozen=True)
class DatasetSpec:
    key: str
    label: str
    inputs_key: str
    result_key: str
    episodes: Callable[[dict[str, Any], dict[str, Any]], list[Episode]]
    horizon: str


DATASETS: tuple[DatasetSpec, ...] = (
    DatasetSpec(
        "hyp012b_discovery_formal",
        "HYP-012b discovery, 5 formal venues pooled",
        "hyp012b_inputs",
        "hyp012b_result",
        hyp012b_episodes,
        "30 min",
    ),
    DatasetSpec(
        "hyp012c_holdout_in_band",
        "HYP-012c holdout, in-band only",
        "hyp012c_inputs",
        "hyp012c_result",
        hyp012c_episodes,
        "30 min",
    ),
    DatasetSpec(
        "hyp029_september",
        "HYP-029 September legs",
        "hyp029_inputs",
        "hyp029_result",
        hyp029_episodes,
        "60 min",
    ),
)


def load_dataset(
    spec: DatasetSpec, artifacts: dict[str, LoadedArtifact]
) -> tuple[list[Episode] | None, str | None]:
    inputs, result = artifacts[spec.inputs_key], artifacts[spec.result_key]
    missing = [a.spec.relative_path for a in (inputs, result) if a.status == "missing"]
    if missing:
        return None, "missing artifact: " + ", ".join(missing)
    assert inputs.payload is not None and result.payload is not None
    _require_pin(inputs, result)
    return spec.episodes(inputs.payload, result.payload), None


# --- Dispersion statistics ---------------------------------------------------------------


def _key(scheme: str) -> Callable[[Episode], str]:
    if scheme == "asset":
        return lambda e: e.cluster
    if scheme == "utc_day":
        return lambda e: e.at.astimezone(UTC).date().isoformat()
    raise ValueError(f"unknown cluster scheme {scheme!r}")


def _iso_week(at: datetime) -> str:
    iso = at.astimezone(UTC).isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


@dataclass(frozen=True)
class ClusterCell:
    residual_sum: float
    count: int
    residuals: tuple[float, ...]


def cluster_cells(residuals: Sequence[tuple[str, float]]) -> list[ClusterCell]:
    grouped: dict[str, list[float]] = defaultdict(list)
    for key, value in residuals:
        grouped[key].append(value)
    return [
        ClusterCell(math.fsum(grouped[key]), len(grouped[key]), tuple(grouped[key]))
        for key in sorted(grouped)
    ]


def cluster_variance_of_mean(cells: Sequence[ClusterCell]) -> float | None:
    """Linearized (CR1) variance of the episode-weighted mean over whole clusters."""
    m = len(cells)
    if m < 2:
        return None
    n = sum(c.count for c in cells)
    mean = math.fsum(c.residual_sum for c in cells) / n
    spread = math.fsum((c.residual_sum - mean * c.count) ** 2 for c in cells)
    return spread / n**2 * m / (m - 1)


def intraclass_correlation(cells: Sequence[ClusterCell]) -> float | None:
    """One-way ANOVA estimator with unequal cluster sizes; None when undefined."""
    k = len(cells)
    n = sum(c.count for c in cells)
    if k < 2 or n <= k:
        return None
    grand = math.fsum(c.residual_sum for c in cells) / n
    between = math.fsum(c.count * (c.residual_sum / c.count - grand) ** 2 for c in cells)
    within = math.fsum(
        (value - c.residual_sum / c.count) ** 2 for c in cells for value in c.residuals
    )
    msb = between / (k - 1)
    msw = within / (n - k)
    n0 = (n - sum(c.count**2 for c in cells) / n) / (k - 1)
    denominator = msb + (n0 - 1) * msw
    return (msb - msw) / denominator if denominator > 0 else None


def lag_one_autocorrelation(values: Sequence[float]) -> float | None:
    if len(values) < 3:
        return None
    mean = fmean(values)
    centered = [v - mean for v in values]
    denominator = math.fsum(v * v for v in centered)
    if denominator <= 0:
        return None
    return math.fsum(a * b for a, b in pairwise(centered)) / denominator


def _quantile(sorted_values: Sequence[float], q: float) -> float:
    position = (len(sorted_values) - 1) * q
    lower = math.floor(position)
    upper = math.ceil(position)
    weight = position - lower
    return sorted_values[lower] * (1 - weight) + sorted_values[upper] * weight


def dispersion_summary(episodes: Sequence[Episode]) -> dict[str, Any]:
    """Shape and dependence of centered net returns. The level is irrelevant to power
    (a constant per-trade cost cancels on centering) and is not reported here."""
    values = [e.net_bps for e in episodes]
    mean = fmean(values)
    residuals = sorted(v - mean for v in values)
    n = len(values)
    sd = pstdev(values) * math.sqrt(n / (n - 1)) if n >= 2 else None
    iid_var = sd**2 / n if sd is not None else None
    by_scheme: dict[str, Any] = {}
    for scheme in CLUSTER_SCHEMES:
        key = _key(scheme)
        cells = cluster_cells([(key(e), e.net_bps - mean) for e in episodes])
        cr_var = cluster_variance_of_mean(cells)
        counts = sorted((c.count for c in cells), reverse=True)
        by_scheme[scheme] = {
            "clusters": len(cells),
            "episodes_per_cluster_mean": n / len(cells) if cells else None,
            "largest_cluster_share": counts[0] / n if counts else None,
            "design_effect": cr_var / iid_var if cr_var is not None and iid_var else None,
            "intraclass_correlation": intraclass_correlation(cells),
        }
    ordered = sorted(episodes, key=lambda e: (e.at, e.cluster, e.net_bps))
    weeks = Counter(_iso_week(e.at) for e in episodes)
    return {
        "episodes": n,
        "sd_bps": sd,
        "residual_quantiles_bps": {
            f"p{int(q * 100):02d}": _quantile(residuals, q) for q in (0.01, 0.1, 0.5, 0.9, 0.99)
        }
        if residuals
        else {},
        "first_at": ordered[0].at.isoformat() if ordered else None,
        "last_at": ordered[-1].at.isoformat() if ordered else None,
        "utc_weeks": len(weeks),
        "largest_week_share": max(weeks.values()) / n if weeks else None,
        "lag1_autocorrelation_time_ordered": lag_one_autocorrelation([e.net_bps for e in ordered]),
        "by_cluster_scheme": by_scheme,
        "week_dependence": (
            "estimable"
            if len(weeks) >= MIN_WEEKS_FOR_WEEK_DEPENDENCE
            else f"not_estimable: {len(weeks)} UTC weeks < {MIN_WEEKS_FOR_WEEK_DEPENDENCE}"
        ),
    }


# --- Power -------------------------------------------------------------------------------


def _z(probability: float) -> float:
    return NormalDist().inv_cdf(probability)


def independent_sample_size(sd_bps: float, effect_bps: float, power: float) -> int:
    """Episodes needed under independence for a one-sided test at ONE_SIDED_ALPHA."""
    if sd_bps < 0 or effect_bps <= 0 or not 0 < power < 1:
        raise ValueError("invalid sample-size request")
    return math.ceil(((_z(1 - ONE_SIDED_ALPHA) + _z(power)) * sd_bps / effect_bps) ** 2)


@dataclass(frozen=True)
class SimulatedCohort:
    mean_residual: float
    standard_error: float
    episodes: int
    clusters: int


def _draw(cells: Sequence[ClusterCell], target_n: int, rng: random.Random) -> list[int]:
    """Whole clusters drawn with replacement until the cohort reaches `target_n`
    episodes. Batches never overshoot before the last draw, so this equals drawing one
    cluster at a time."""
    largest = max(c.count for c in cells)
    drawn: list[int] = []
    total = 0
    indices = range(len(cells))
    while total < target_n:
        batch = max(1, (target_n - total) // largest)
        picks = rng.choices(indices, k=batch)
        if batch == 1:
            drawn.append(picks[0])
            total += cells[picks[0]].count
            continue
        drawn.extend(picks)
        total += sum(cells[i].count for i in picks)
    return drawn


def _cohort(cells: Sequence[ClusterCell], drawn: Sequence[int]) -> SimulatedCohort:
    s = sn = ss = nn = 0.0
    n = 0
    for index in drawn:
        cell = cells[index]
        s += cell.residual_sum
        n += cell.count
        ss += cell.residual_sum**2
        sn += cell.residual_sum * cell.count
        nn += cell.count**2
    m = len(drawn)
    mean = s / n
    spread = max(0.0, ss - 2 * mean * sn + mean**2 * nn)
    se = math.sqrt(spread / n**2 * m / (m - 1)) if m >= 2 else math.inf
    return SimulatedCohort(mean, se, n, m)


def simulate_cohorts(
    cells: Sequence[ClusterCell], target_n: int, replicates: int, seed: int
) -> list[SimulatedCohort]:
    # A deterministic research PRNG is required for reproducible simulated cohorts.
    rng = random.Random(seed)  # noqa: S311
    return [_cohort(cells, _draw(cells, target_n, rng)) for _ in range(replicates)]


def passes(cohort: SimulatedCohort, effect_bps: float) -> bool:
    mean = cohort.mean_residual + effect_bps
    return mean > 0 and mean - _z(1 - ONE_SIDED_ALPHA) * cohort.standard_error > 0


def power_curve(
    cells: Sequence[ClusterCell],
    *,
    label: str,
    replicates: int,
    seed: int,
    sizes: Sequence[int] = SAMPLE_SIZE_GRID,
) -> list[dict[str, Any]]:
    """Simulated pass rate per grid size at zero and every effect, with Monte Carlo SE.
    A size is `evaluable` only when its cohorts hold at least MIN_SIMULATION_CLUSTERS
    clusters on average: with fewer, the normal interval is anticonservative, so the size
    can neither satisfy a requirement nor stop the grid. Stops after the first evaluable
    size at which the smallest effect reaches the top target."""
    rows = []
    effects = (0.0, *EFFECT_GRID_BPS)
    for size in sizes:
        cohorts = simulate_cohorts(cells, size, replicates, derived_seed(seed, f"{label}:{size}"))
        rates = {f"{e:g}": fmean(passes(c, e) for c in cohorts) for e in effects}
        clusters = fmean(c.clusters for c in cohorts)
        evaluable = clusters >= MIN_SIMULATION_CLUSTERS
        rows.append(
            {
                "target_episodes": size,
                "mean_episodes": fmean(c.episodes for c in cohorts),
                "mean_clusters_drawn": clusters,
                "evaluable": evaluable,
                "pass_rate": rates,
                "monte_carlo_se": {
                    key: math.sqrt(rate * (1 - rate) / replicates) for key, rate in rates.items()
                },
            }
        )
        if evaluable and rates[f"{EFFECT_GRID_BPS[0]:g}"] >= POWER_TARGETS[-1]:
            break
    return rows


def required_from_curve(
    curve: Sequence[dict[str, Any]], effect_bps: float, target: float
) -> dict[str, Any] | None:
    """The first evaluable size at or above the floor that reaches `target`. If that is
    the first evaluable size and smaller sizes at or above the floor were skipped as not
    evaluable, the true requirement may be smaller: the cell is `censored` and only
    bounds the requirement from above."""
    skipped = False
    first_evaluable = True
    for row in curve:
        if row["target_episodes"] < EVIDENCE_FLOOR_RESOLVED:
            continue
        if not row["evaluable"]:
            skipped = True
            continue
        rate = row["pass_rate"][f"{effect_bps:g}"]
        if rate >= target:
            return {
                "episodes": row["target_episodes"],
                "simulated_power": rate,
                "monte_carlo_se": row["monte_carlo_se"][f"{effect_bps:g}"],
                "clusters_drawn": row["mean_clusters_drawn"],
                "censored": skipped and first_evaluable,
            }
        first_evaluable = False
    return None


def calibrate_against_bootstrap(
    cells: Sequence[ClusterCell],
    *,
    target_n: int,
    replicates: int,
    iterations: int,
    seed: int,
    label: str,
) -> dict[str, Any]:
    """The registered asset-cluster bootstrap (`cluster_bootstrap_mean`) and the fast
    linearized rule on the same simulated cohorts, at zero and at a positive effect."""
    rng = random.Random(derived_seed(seed, f"calibration:{label}"))  # noqa: S311
    decisions: dict[str, list[tuple[bool, bool]]] = {"0": [], f"{CALIBRATION_EFFECT_BPS:g}": []}
    for replicate in range(replicates):
        drawn = _draw(cells, target_n, rng)
        cohort = _cohort(cells, drawn)
        for effect in (0.0, CALIBRATION_EFFECT_BPS):
            observations = tuple(
                ClusterObservation(f"draw-{position}", value + effect)
                for position, index in enumerate(drawn)
                for value in cells[index].residuals
            )
            estimate = cluster_bootstrap_mean(
                observations,
                iterations=iterations,
                seed=derived_seed(seed, f"calibration:{label}:{replicate}:{effect:g}"),
            ).estimate
            bootstrap = estimate.point_estimate > 0 and estimate.lower_bound > 0
            decisions[f"{effect:g}"].append((passes(cohort, effect), bootstrap))
    return {
        "target_episodes": target_n,
        "replicates": replicates,
        "bootstrap_iterations": iterations,
        "by_effect_bps": {
            effect: {
                "linearized_pass_rate": fmean(a for a, _ in pairs),
                "bootstrap_pass_rate": fmean(b for _, b in pairs),
                "agreement": fmean(a == b for a, b in pairs),
            }
            for effect, pairs in decisions.items()
        },
    }


def dataset_power(
    episodes: Sequence[Episode],
    *,
    label: str,
    replicates: int,
    calibration_replicates: int,
    calibration_iterations: int,
    seed: int,
) -> dict[str, Any]:
    summary = dispersion_summary(episodes)
    mean = fmean(e.net_bps for e in episodes)
    sd = summary["sd_bps"]
    independent = (
        {
            f"{effect:g}": {
                f"{target:g}": max(
                    independent_sample_size(sd, effect, target), EVIDENCE_FLOOR_RESOLVED
                )
                for target in POWER_TARGETS
            }
            for effect in EFFECT_GRID_BPS
        }
        if sd is not None
        else None
    )
    icc = summary["by_cluster_scheme"]["asset"]["intraclass_correlation"]
    repeats = (
        [
            {
                "episodes_per_asset": m,
                "design_effect": 1 + (m - 1) * max(0.0, icc),
                "episodes_for_50bps_80pct": max(
                    math.ceil(
                        independent_sample_size(sd, 50.0, 0.8) * (1 + (m - 1) * max(0.0, icc))
                    ),
                    EVIDENCE_FLOOR_RESOLVED,
                ),
            }
            for m in REPEAT_SENSITIVITY_EPISODES_PER_CLUSTER
        ]
        if icc is not None and sd is not None
        else None
    )
    schemes: dict[str, Any] = {}
    for scheme in CLUSTER_SCHEMES:
        key = _key(scheme)
        cells = cluster_cells([(key(e), e.net_bps - mean) for e in episodes])
        if len(cells) < MIN_SIMULATION_CLUSTERS:
            schemes[scheme] = {
                "status": "unavailable",
                "reason": f"{len(cells)} {scheme} clusters < {MIN_SIMULATION_CLUSTERS}",
            }
            continue
        curve = power_curve(cells, label=f"{label}:{scheme}", replicates=replicates, seed=seed)
        evaluable = [row for row in curve if row["evaluable"]]
        null_rates = [row["pass_rate"]["0"] for row in evaluable]
        null_ses = [row["monte_carlo_se"]["0"] for row in evaluable]
        anticonservative = any(
            rate > ONE_SIDED_ALPHA + 3 * se + 0.005
            for rate, se in zip(null_rates, null_ses, strict=True)
        )
        required = {
            f"{effect:g}": {
                f"{target:g}": required_from_curve(curve, effect, target)
                for target in POWER_TARGETS
            }
            for effect in EFFECT_GRID_BPS
        }
        schemes[scheme] = {
            "status": "simulated",
            "historical_clusters": len(cells),
            "curve": curve,
            "evaluable_sizes": [row["target_episodes"] for row in evaluable],
            "null_pass_rate_max": max(null_rates, default=None),
            "zero_effect_check": (
                "not_evaluable"
                if not evaluable
                else "anticonservative"
                if anticonservative
                else "ok"
            ),
            "required": required,
        }
    calibration = []
    asset = schemes.get("asset", {})
    if asset.get("status") == "simulated" and calibration_replicates > 0:
        anchor = asset["required"][f"{CALIBRATION_EFFECT_BPS:g}"]["0.8"]
        anchor_n = min(anchor["episodes"] if anchor else CALIBRATION_MAX_N, CALIBRATION_MAX_N)
        cells = cluster_cells([(e.cluster, e.net_bps - mean) for e in episodes])
        calibration = [
            calibrate_against_bootstrap(
                cells,
                target_n=target_n,
                replicates=calibration_replicates,
                iterations=calibration_iterations,
                seed=seed,
                label=f"{label}:{target_n}",
            )
            for target_n in sorted({EVIDENCE_FLOOR_RESOLVED, anchor_n})
        ]
    return {
        "dispersion": summary,
        "independent_episodes": independent,
        "repeat_sensitivity": repeats,
        "simulation": schemes,
        "bootstrap_calibration": calibration,
    }


def conservative_required(power: dict[str, Any], effect_bps: float, target: float) -> Any:
    """The larger simulated requirement over the cluster schemes that identify it. A
    censored cell (the scheme is first evaluable above the requirement) does not bind; when
    every scheme is censored, the smallest censored size is returned as an upper bound, so
    a censored scheme never binds here either."""
    found = []
    censored = []
    for scheme in CLUSTER_SCHEMES:
        block = power["simulation"].get(scheme, {})
        if block.get("status") != "simulated":
            continue
        cell = block["required"][f"{effect_bps:g}"][f"{target:g}"]
        if cell is None:
            return {"status": "not_reached", "scheme": scheme, "grid_max": SAMPLE_SIZE_GRID[-1]}
        if cell["censored"]:
            censored.append({**cell, "scheme": scheme})
            continue
        found.append({**cell, "scheme": scheme})
    if not found:
        return min(censored, key=lambda cell: cell["episodes"]) if censored else None
    return max(found, key=lambda cell: cell["episodes"])


# --- Calendar and economics --------------------------------------------------------------


def erlang_b(servers: int, offered_load: float) -> float:
    """Blocking probability of an M/G/c/c system (arrivals lost when all slots are busy)."""
    if servers < 1 or offered_load < 0:
        raise ValueError("invalid Erlang B request")
    blocking = 1.0
    for k in range(1, servers + 1):
        blocking = offered_load * blocking / (k + offered_load * blocking)
    return blocking


def calendar_days(episodes: int, flow_per_day: float, resolved_fraction: float) -> float | None:
    rate = flow_per_day * resolved_fraction
    return episodes / rate if rate > 0 else None


def executable_entries_per_month(
    flow_per_day: float,
    *,
    resolved_fraction: float,
    rejection_fraction: float,
    max_concurrent: int,
    hold_minutes: int,
) -> float:
    accepted = flow_per_day * resolved_fraction * (1 - rejection_fraction)
    blocking = erlang_b(max_concurrent, accepted * hold_minutes / 1_440)
    return accepted * (1 - blocking) * DAYS_PER_MONTH


def required_net_bps(
    monthly_cost_usd: float, monthly_target_usd: float, entries: float, notional_usd: float
) -> float | None:
    """Mean net bps per executed trade that covers the monthly cost and target."""
    if entries <= 0 or notional_usd <= 0:
        return None
    return (monthly_cost_usd + monthly_target_usd) / (entries * notional_usd) * 10_000


def monthly_result_usd(effect_bps: float, entries: float, notional_usd: float) -> float:
    return effect_bps / 10_000 * notional_usd * entries


def accrual_reference(audit_doc: Path) -> dict[str, Any]:
    if not audit_doc.exists():
        return {"status": "missing", "path": str(audit_doc), "flow_per_day": None}
    body = audit_doc.read_bytes()
    if AUDIT_ROW_MARKER not in body.decode("utf-8"):
        raise ArtifactIntegrityError("the audit document no longer carries the counters")
    return {
        "status": "verified",
        "path": str(audit_doc),
        "sha256": _sha256(body),
        "source_eligible_captures": AUDIT_SOURCE_ELIGIBLE_CAPTURES,
        "window_days": AUDIT_WINDOW_DAYS,
        "flow_per_day": AUDIT_SOURCE_ELIGIBLE_CAPTURES / AUDIT_WINDOW_DAYS,
        "note": "before target sampling, book and qualification checks; illustration only",
    }


def flow_scenarios(accrual: dict[str, Any]) -> list[float]:
    flows = list(FLOW_SCENARIOS_PER_DAY)
    if accrual["flow_per_day"] is not None:
        flows.append(accrual["flow_per_day"])
    return sorted(set(flows))


def economics(flows: Sequence[float]) -> list[dict[str, Any]]:
    rows = []
    for flow in flows:
        for resolved in RESOLVED_FRACTION_SCENARIOS:
            for rejection in REJECTION_FRACTION_SCENARIOS:
                for concurrent in MAX_CONCURRENT_SCENARIOS:
                    for hold in HOLD_SCENARIOS_MINUTES:
                        entries = executable_entries_per_month(
                            flow,
                            resolved_fraction=resolved,
                            rejection_fraction=rejection,
                            max_concurrent=concurrent,
                            hold_minutes=hold,
                        )
                        rows.append(
                            {
                                "flow_per_day": flow,
                                "resolved_fraction": resolved,
                                "rejection_fraction": rejection,
                                "max_concurrent": concurrent,
                                "hold_minutes": hold,
                                "entries_per_month": entries,
                                "required_net_bps_at_usd50": {
                                    f"cost_{cost:g}_target_{target:g}": required_net_bps(
                                        cost, target, entries, 50.0
                                    )
                                    for cost in MONTHLY_OPERATING_COST_SCENARIOS_USD
                                    for target in MONTHLY_TARGET_RESULT_SCENARIOS_USD
                                },
                                "monthly_usd_at_usd50": {
                                    f"{effect:g}": monthly_result_usd(effect, entries, 50.0)
                                    for effect in EFFECT_GRID_BPS
                                },
                                "larger_notionals": "capacity_not_measured",
                            }
                        )
    return rows


def calendar(power: dict[str, dict[str, Any]], flows: Sequence[float]) -> list[dict[str, Any]]:
    rows = []
    for dataset, block in power.items():
        if block.get("status") != "available":
            continue
        for effect in EFFECT_GRID_BPS:
            for target in POWER_TARGETS:
                required = conservative_required(block, effect, target)
                episodes = required.get("episodes") if isinstance(required, dict) else None
                rows.append(
                    {
                        "dataset": dataset,
                        "effect_bps": effect,
                        "power_target": target,
                        "required": required,
                        "days": {
                            f"flow_{flow:g}_resolved_{resolved:g}": (
                                calendar_days(episodes, flow, resolved) if episodes else None
                            )
                            for flow in flows
                            for resolved in RESOLVED_FRACTION_SCENARIOS
                        },
                        "flow_per_day_needed": {
                            f"days_{days}_resolved_{resolved:g}": (
                                episodes / (days * resolved) if episodes else None
                            )
                            for days in CALENDAR_HORIZONS_DAYS
                            for resolved in RESOLVED_FRACTION_SCENARIOS
                        },
                        "test_result_usd_at_usd50": (
                            monthly_result_usd(effect, episodes, 50.0) if episodes else None
                        ),
                    }
                )
    return rows


def missing_measurements(
    thresholds: dict[str, Any], accrual: dict[str, Any], power: dict[str, dict[str, Any]]
) -> list[str]:
    items = [
        "executed fills: the cost read has book quotes only",
        "latency from signal to order and the quote-to-fill difference",
        "adverse selection and maker non-fill",
        "funding for the future line's holding period",
        "impact and capacity above USD 50",
        "event frequency of the future universe after its own filters",
        "dispersion of the future signal (past strategies are scenarios only)",
        "operating budget and target monthly result (not agreed)",
    ]
    if thresholds["status"] != "verified":
        items.insert(0, "pre-blind book-cost result artifact")
    elif thresholds.get("quote_age_unknown_groups"):
        items.append("source-lead quote age (unknown for every source-lead group)")
        items.append("source-lead exit 30 minutes later (only a same-book immediate cross)")
    if accrual["status"] != "verified":
        items.append("published v2 accrual counters (audit document not found)")
    for dataset, block in power.items():
        if block.get("status") != "available":
            items.append(f"{dataset}: {block['reason']}")
            continue
        if block["dispersion"]["week_dependence"] != "estimable":
            items.append(
                f"{dataset}: week-level dependence ({block['dispersion']['week_dependence']})"
            )
        for scheme, sim in block["simulation"].items():
            if sim.get("status") != "simulated":
                items.append(f"{dataset}: {scheme} simulation ({sim['reason']})")
    return items


# --- Report ------------------------------------------------------------------------------


def parameters() -> dict[str, Any]:
    return {
        "effect_grid_bps": list(EFFECT_GRID_BPS),
        "power_targets": list(POWER_TARGETS),
        "one_sided_alpha": ONE_SIDED_ALPHA,
        "evidence_floor_resolved": EVIDENCE_FLOOR_RESOLVED,
        "sample_size_grid": list(SAMPLE_SIZE_GRID),
        "simulation_seed": SIMULATION_SEED,
        "cluster_schemes": list(CLUSTER_SCHEMES),
        "min_simulation_clusters": MIN_SIMULATION_CLUSTERS,
        "min_weeks_for_week_dependence": MIN_WEEKS_FOR_WEEK_DEPENDENCE,
        "fee_scenarios_bps_per_side": list(FEE_SCENARIOS_BPS),
        "quote_to_fill_scenarios_bps": list(QUOTE_TO_FILL_SCENARIOS_BPS),
        "funding_scenarios_bps_per_8h": list(FUNDING_SCENARIOS_BPS_PER_8H),
        "hold_scenarios_minutes": list(HOLD_SCENARIOS_MINUTES),
        "flow_scenarios_per_day": list(FLOW_SCENARIOS_PER_DAY),
        "resolved_fraction_scenarios": list(RESOLVED_FRACTION_SCENARIOS),
        "rejection_fraction_scenarios": list(REJECTION_FRACTION_SCENARIOS),
        "max_concurrent_scenarios": list(MAX_CONCURRENT_SCENARIOS),
        "calendar_horizons_days": list(CALENDAR_HORIZONS_DAYS),
        "monthly_operating_cost_scenarios_usd": list(MONTHLY_OPERATING_COST_SCENARIOS_USD),
        "monthly_target_result_scenarios_usd": list(MONTHLY_TARGET_RESULT_SCENARIOS_USD),
        "unmeasured_notionals_usd": list(UNMEASURED_NOTIONALS_USD),
        "calibration_effect_bps": CALIBRATION_EFFECT_BPS,
        "calibration_max_n": CALIBRATION_MAX_N,
    }


def build_report(
    artifact_root: Path,
    audit_doc: Path,
    *,
    code_revision: str,
    working_tree_dirty: bool,
    replicates: int = SIMULATION_REPLICATES,
    calibration_replicates: int = CALIBRATION_REPLICATES,
    calibration_iterations: int = CALIBRATION_BOOTSTRAP_ITERATIONS,
    artifact_specs: Sequence[ArtifactSpec] = ARTIFACTS,
) -> dict[str, Any]:
    """Pure over the saved artifacts: the same files and arguments give the same bytes."""
    if replicates < 100:
        raise ValueError("power simulation requires at least 100 replicates")
    artifacts = load_artifacts(artifact_root, artifact_specs)
    thresholds = trading_thresholds(artifacts["preblind_result"].payload)
    accrual = accrual_reference(audit_doc)
    power: dict[str, dict[str, Any]] = {}
    for spec in DATASETS:
        episodes, reason = load_dataset(spec, artifacts)
        if episodes is None:
            power[spec.key] = {"status": "unavailable", "label": spec.label, "reason": reason}
            continue
        if len(episodes) < 2:
            power[spec.key] = {
                "status": "unavailable",
                "label": spec.label,
                "reason": f"{len(episodes)} resolved episodes",
            }
            continue
        power[spec.key] = {
            "status": "available",
            "label": spec.label,
            "horizon": spec.horizon,
            **dataset_power(
                episodes,
                label=spec.key,
                replicates=replicates,
                calibration_replicates=calibration_replicates,
                calibration_iterations=calibration_iterations,
                seed=SIMULATION_SEED,
            ),
        }
    flows = flow_scenarios(accrual)
    params = {
        **parameters(),
        "replicates": replicates,
        "calibration_replicates": calibration_replicates,
        "calibration_iterations": calibration_iterations,
    }
    return {
        "report_version": REPORT_VERSION,
        "status": "conditional_planning_calculation_not_a_forecast",
        "code_revision": normalize_code_revision(code_revision),
        "working_tree_dirty": working_tree_dirty,
        "parameters": params,
        "parameters_sha256": _sha256(json.dumps(params, sort_keys=True).encode()),
        "inputs": [a.fingerprint() for a in artifacts.values()],
        "accrual_reference": accrual,
        "trading_thresholds": thresholds,
        "unobserved_cost_scenarios": unobserved_cost_scenarios(),
        "power": power,
        "calendar": calendar(power, flows),
        "economics_usd50": economics(flows),
        "missing_measurements": missing_measurements(thresholds, accrual, power),
    }


def _fmt(value: Any, digits: int = 1) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float) and math.isinf(value):
        return "inf"
    return f"{value:,.{digits}f}"


def _day_limit(block: dict[str, Any], effect_bps: float) -> list[str]:
    """Name a day-level dependence that the UTC-day simulation could not size."""
    schemes = block["dispersion"]["by_cluster_scheme"]
    if (schemes["utc_day"]["design_effect"] or 0) <= max(schemes["asset"]["design_effect"] or 0, 1):
        return []
    day = block["simulation"].get("utc_day", {})
    if day.get("status") != "simulated":
        return [f"day Deff {_fmt(schemes['utc_day']['design_effect'], 2)}, not simulated"]
    cell = day["required"][f"{effect_bps:g}"]["0.8"]
    if cell is not None and cell["censored"]:
        return [
            f"day Deff {_fmt(schemes['utc_day']['design_effect'], 2)}, day scheme "
            f"evaluable only from {min(day['evaluable_sizes']):,}"
        ]
    return []


def _episodes(required: Any) -> str:
    if required is None:
        return "unavailable"
    if required.get("status") == "not_reached":
        return f">{required['grid_max']:,}"
    bound = "<=" if required.get("censored") else ""
    return f"{bound}{required['episodes']:,}"


def render_markdown(report: dict[str, Any]) -> str:
    """The generated tables; the methodology document carries the interpretation."""
    lines = [
        f"Report `{report['report_version']}`, code `{report['code_revision']}`"
        f" (dirty: {report['working_tree_dirty']}), parameters "
        f"`{report['parameters_sha256'][:12]}`.",
        "",
    ]
    lines += ["### Inputs", "", "| Key | Status | SHA-256 |", "| --- | --- | --- |"]
    for item in report["inputs"]:
        lines.append(f"| {item['key']} | {item['status']} | `{item['sha256'] or '-'}` |")
    accrual = report["accrual_reference"]
    lines += [
        "",
        f"Accrual reference: {accrual['status']}, flow {_fmt(accrual['flow_per_day'], 2)}/day.",
        "",
    ]

    thresholds = report["trading_thresholds"]
    lines += ["### Break-even midpoint move (bps)", ""]
    if thresholds["status"] != "verified":
        lines.append(f"Unavailable: {thresholds['missing_measurement']}.")
    else:
        lines += [
            "| Population | Venue | Spread | Age | n | Mean f0 | Mean f5.5 | p90 f5.5 | Mean f10 |",
            "| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: |",
        ]
        for g in thresholds["groups"]:
            be = g["break_even_bps"]
            lines.append(
                f"| {g['population']} {g['version']} | {g['venue']} | "
                f"{g['entry_spread_bucket']} | {g['quote_age_quality']} | {g['n']} | "
                f"{_fmt(be['fee_0']['mean'], 2)} | {_fmt(be['fee_5.5']['mean'], 2)} | "
                f"{_fmt(be['fee_5.5']['p90'], 2)} | {_fmt(be['fee_10']['mean'], 2)} |"
            )
    lines += [
        "",
        "Capacity: "
        + ", ".join(f"${c['notional_usd']:g} {c['status']}" for c in thresholds["capacity"])
        + ".",
        "",
    ]

    lines += [
        "### Dispersion scenarios",
        "",
        "| Dataset | Hold | Episodes | SD bps | Assets | Deff asset | ICC asset | Days "
        "| Deff day | Weeks | Lag-1 |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for key, block in report["power"].items():
        if block["status"] != "available":
            lines.append(f"| {key} | | | unavailable: {block['reason']} | | | | | | | |")
            continue
        d = block["dispersion"]
        a, u = d["by_cluster_scheme"]["asset"], d["by_cluster_scheme"]["utc_day"]
        lines.append(
            f"| {key} | {block['horizon']} | {d['episodes']} | {_fmt(d['sd_bps'])} | "
            f"{a['clusters']} | {_fmt(a['design_effect'], 2)} | "
            f"{_fmt(a['intraclass_correlation'], 3)} | {u['clusters']} | "
            f"{_fmt(u['design_effect'], 2)} | {d['utc_weeks']} | "
            f"{_fmt(d['lag1_autocorrelation_time_ordered'], 3)} |"
        )

    lines += [
        "",
        "### Zero-effect check",
        "",
        "| Dataset | Scheme | Smallest evaluable size | Max null pass rate | Check |",
        "| --- | --- | ---: | ---: | --- |",
    ]
    for key, block in report["power"].items():
        if block["status"] != "available":
            continue
        for scheme, sim in block["simulation"].items():
            if sim["status"] != "simulated":
                lines.append(f"| {key} | {scheme} | n/a | n/a | {sim['reason']} |")
                continue
            lines.append(
                f"| {key} | {scheme} | "
                f"{min(sim['evaluable_sizes'], default='none')} | "
                f"{_fmt(sim['null_pass_rate_max'], 3)} | "
                f"{sim['zero_effect_check']} |"
            )
    lines += [
        "",
        "### Linearized rule versus the registered asset-cluster bootstrap",
        "",
        "| Dataset | n | Null pass: linear / bootstrap | 50 bps pass: linear / bootstrap "
        "| Agreement at 0 / 50 |",
        "| --- | ---: | --- | --- | --- |",
    ]
    for key, block in report["power"].items():
        for calibration in block.get("bootstrap_calibration") or []:
            z = calibration["by_effect_bps"]["0"]
            p = calibration["by_effect_bps"][f"{CALIBRATION_EFFECT_BPS:g}"]
            lines.append(
                f"| {key} | {calibration['target_episodes']} | "
                f"{_fmt(z['linearized_pass_rate'], 3)} / {_fmt(z['bootstrap_pass_rate'], 3)} | "
                f"{_fmt(p['linearized_pass_rate'], 3)} / {_fmt(p['bootstrap_pass_rate'], 3)} | "
                f"{_fmt(z['agreement'], 3)} / {_fmt(p['agreement'], 3)} |"
            )

    reference_flow = report["accrual_reference"]["flow_per_day"] or 1.0
    lines += [
        "",
        f"### Main table (resolved fraction 0.9, flow {_fmt(reference_flow, 2)}/day)",
        "",
        "| Net effect | Dataset | Episodes 80% / 90% (scheme) | Power at n (MC SE) | "
        "Clusters drawn | Days at flow | Flow/day for 91 / 183 days | "
        "$ over test at $50 | Limitations |",
        "| ---: | --- | --- | --- | ---: | ---: | --- | ---: | --- |",
    ]
    by_key = {(r["dataset"], r["effect_bps"], r["power_target"]): r for r in report["calendar"]}
    for effect in EFFECT_GRID_BPS:
        for key, block in report["power"].items():
            if block["status"] != "available":
                lines.append(f"| {effect:g} | {key} | unavailable | | | | | | {block['reason']} |")
                continue
            r80, r90 = by_key[(key, effect, 0.8)], by_key[(key, effect, 0.9)]
            q80, q90 = r80["required"], r90["required"]
            limits = []
            dispersion = block["dispersion"]
            if dispersion["week_dependence"] != "estimable":
                limits.append(f"{dispersion['utc_weeks']} weeks")
            if (
                q80
                and q80.get("clusters_drawn", 0)
                > dispersion["by_cluster_scheme"]["asset"]["clusters"]
            ):
                limits.append("more assets than observed")
            if any(
                s.get("zero_effect_check") == "anticonservative"
                for s in block["simulation"].values()
            ):
                limits.append("null pass rate high")
            limits.extend(_day_limit(block, effect))
            reached = q80 is not None and q80.get("episodes") is not None
            days = r80["days"].get(f"flow_{reference_flow:g}_resolved_0.9") if reached else None
            lines.append(
                f"| {effect:g} | {key} | {_episodes(q80)} / {_episodes(q90)}"
                f"{' (' + q80['scheme'] + ')' if reached else ''} | "
                + (
                    f"{_fmt(q80['simulated_power'], 3)} ({_fmt(q80['monte_carlo_se'], 3)})"
                    if reached
                    else "n/a"
                )
                + f" | {_fmt(q80['clusters_drawn'], 0) if reached else 'n/a'} | {_fmt(days, 0)} | "
                f"{_fmt(r80['flow_per_day_needed']['days_91_resolved_0.9'], 1)} / "
                f"{_fmt(r80['flow_per_day_needed']['days_183_resolved_0.9'], 1)} | "
                f"{_fmt(r80['test_result_usd_at_usd50'], 0)} | {', '.join(limits) or '-'} |"
            )

    lines += [
        "",
        "### Required mean net bps at $50 (resolved 0.9, rejection 0.2, 1 slot, 60 min hold)",
        "",
        "| Flow/day | Entries/month | "
        + " | ".join(
            f"cost ${c:g} + target ${t:g}"
            for c in MONTHLY_OPERATING_COST_SCENARIOS_USD
            for t in MONTHLY_TARGET_RESULT_SCENARIOS_USD
        )
        + " |",
        "| ---: | ---: | "
        + " | ".join(
            "---:"
            for _ in range(
                len(MONTHLY_OPERATING_COST_SCENARIOS_USD) * len(MONTHLY_TARGET_RESULT_SCENARIOS_USD)
            )
        )
        + " |",
    ]
    for row in report["economics_usd50"]:
        if (
            row["resolved_fraction"],
            row["rejection_fraction"],
            row["max_concurrent"],
            row["hold_minutes"],
        ) != (0.9, 0.2, 1, 60):
            continue
        cells = " | ".join(_fmt(v, 0) for v in row["required_net_bps_at_usd50"].values())
        lines.append(
            f"| {_fmt(row['flow_per_day'], 2)} | {_fmt(row['entries_per_month'], 1)} | {cells} |"
        )
    lines += ["", "### Missing measurements", ""]
    lines += [f"- {item}" for item in report["missing_measurements"]]
    return "\n".join(lines) + "\n"


def report_body(report: dict[str, Any]) -> bytes:
    return json.dumps(report, indent=2, sort_keys=True, allow_nan=False).encode() + b"\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--artifact-root", type=Path, default=DEFAULT_ARTIFACT_ROOT)
    parser.add_argument("--audit-doc", type=Path, default=DEFAULT_AUDIT_DOC)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--code-revision", required=True)
    parser.add_argument("--working-tree-dirty", dest="working_tree_dirty", action="store_true")
    parser.add_argument("--no-working-tree-dirty", dest="working_tree_dirty", action="store_false")
    parser.add_argument("--replicates", type=int, default=SIMULATION_REPLICATES)
    parser.add_argument("--calibration-replicates", type=int, default=CALIBRATION_REPLICATES)
    parser.set_defaults(working_tree_dirty=True)
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    report = build_report(
        args.artifact_root,
        args.audit_doc,
        code_revision=args.code_revision,
        working_tree_dirty=args.working_tree_dirty,
        replicates=args.replicates,
        calibration_replicates=args.calibration_replicates,
    )
    body = report_body(report)
    markdown = render_markdown(report)
    if args.output_dir is not None:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        (args.output_dir / "result.json").write_bytes(body)
        (args.output_dir / "result.json.sha256").write_text(_sha256(body) + "\n")
        (args.output_dir / "tables.md").write_text(markdown, encoding="utf-8")
    sys.stdout.write(markdown)
    sys.stdout.write(f"\nresult sha256 {_sha256(body)}\n")


if __name__ == "__main__":
    main()
