"""A registered hypothesis as something a run can be checked against.

Every research error found on 2026-09-08 was a contract that a human had to
remember to honour and did not: a report run with its default `--until` instead
of the registered window, a result reported in means against a margin written
for medians, a "read once" that reloaded on every invocation, and a winner
chosen from data the confirmation was supposed to be independent of.

The registrations were not wrong. They were prose, and prose does not fail a
run. This turns the parts a machine can check into something a run must pass
before it looks at any outcome.

What it does not do, and cannot: decide whether the question is worth asking, or
whether the window was chosen honestly. Those stay human. What it removes is the
class of error where the answer is real and the label on it is not.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path  # noqa: TC003 -- used at runtime, not only in annotations
from statistics import fmean, median
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

CONTRACT_SCHEMA_VERSION = "research_contract_v1"


class ContractViolationError(Exception):
    """A run does not match the contract it claims to be executing.

    Deliberately not a warning. A run that has drifted from its registration is
    not a run with a caveat; it is a measurement of something nobody registered,
    and the number it produces will be quoted without the caveat.
    """


# Named metrics, so that "which statistic" is part of the registered contract
# rather than a line of code someone can change without changing the
# registration. Swapping median for mean now requires editing the contract,
# which changes its checksum, which is visible in review.
_METRICS: dict[str, Callable[[Sequence[float]], float]] = {
    "median": median,
    "mean": fmean,
}


@dataclass(frozen=True)
class ResearchContract:
    """The machine-checkable part of a registered hypothesis.

    Field order is part of the checksum, so this is serialized canonically and
    never by `dict` iteration order.
    """

    hypothesis_id: str
    contract_version: str
    # Half-open, explicit, and never defaulted to "now". A window that can be
    # `datetime.now()` is not a window: two runs of the same contract would
    # cover different data and both would call themselves the registered one.
    window_since: datetime
    window_until: datetime
    strategy_versions: tuple[str, ...]
    allow_fallback: bool
    # The horizon the outcome is read at, in minutes. Also the amount by which
    # the window must be trimmed for an episode to be fully inside it -- see
    # `crosses_window_boundary`.
    outcome_horizon_minutes: int
    # "median" or "mean". Named here rather than chosen at the call site.
    metric: str
    # How the challenger is compared to the baseline. `difference_of_metric`
    # applies the metric to each policy and subtracts; `metric_of_differences`
    # applies it to per-episode differences. They are different numbers and
    # HYP-022 reported one against a margin written for the other.
    comparison: str
    baseline_policy: str
    challenger_policies: tuple[str, ...]
    cost_model_version: str
    minimum_completed_trades: int
    minimum_clusters: int
    # Thresholds, in the metric's own units.
    candidate_margin: float
    rejection_margin: float
    schema_version: str = CONTRACT_SCHEMA_VERSION
    notes: str = ""
    checksum: str = field(default="", compare=False)

    def __post_init__(self) -> None:
        if self.window_since.tzinfo is None or self.window_until.tzinfo is None:
            raise ValueError("contract window bounds must be timezone-aware")
        if self.window_since >= self.window_until:
            raise ValueError("contract window must be non-empty and ordered")
        if self.metric not in _METRICS:
            raise ValueError(f"unknown metric {self.metric!r}; expected one of {sorted(_METRICS)}")
        if self.comparison not in ("difference_of_metric", "metric_of_differences"):
            raise ValueError(f"unknown comparison {self.comparison!r}")
        if not self.challenger_policies:
            raise ValueError("a contract must name at least one challenger")
        if self.baseline_policy in self.challenger_policies:
            raise ValueError("the baseline cannot also be a challenger")
        if self.candidate_margin <= self.rejection_margin:
            raise ValueError("the candidate margin must exceed the rejection margin")

    # --- identity ----------------------------------------------------------

    def canonical_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("checksum")
        payload["window_since"] = self.window_since.isoformat()
        payload["window_until"] = self.window_until.isoformat()
        payload["strategy_versions"] = list(self.strategy_versions)
        payload["challenger_policies"] = list(self.challenger_policies)
        return payload

    def compute_checksum(self) -> str:
        encoded = json.dumps(
            self.canonical_payload(), sort_keys=True, separators=(",", ":")
        ).encode()
        return hashlib.sha256(encoded).hexdigest()

    def to_json(self) -> str:
        payload = self.canonical_payload()
        payload["checksum"] = self.compute_checksum()
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def load_contract(path: Path) -> ResearchContract:
    """Read a contract and refuse it if its checksum does not match.

    The checksum is not about tampering by an adversary; it is about a
    registration edited after a result was seen. That leaves no trace otherwise,
    and it is the single easiest way to turn a failed hypothesis into a passed
    one.
    """
    payload = json.loads(path.read_text())
    stated = payload.pop("checksum", "")
    contract = ResearchContract(
        hypothesis_id=payload["hypothesis_id"],
        contract_version=payload["contract_version"],
        window_since=datetime.fromisoformat(payload["window_since"]),
        window_until=datetime.fromisoformat(payload["window_until"]),
        strategy_versions=tuple(payload["strategy_versions"]),
        allow_fallback=payload["allow_fallback"],
        outcome_horizon_minutes=payload["outcome_horizon_minutes"],
        metric=payload["metric"],
        comparison=payload["comparison"],
        baseline_policy=payload["baseline_policy"],
        challenger_policies=tuple(payload["challenger_policies"]),
        cost_model_version=payload["cost_model_version"],
        minimum_completed_trades=payload["minimum_completed_trades"],
        minimum_clusters=payload["minimum_clusters"],
        candidate_margin=payload["candidate_margin"],
        rejection_margin=payload["rejection_margin"],
        schema_version=payload.get("schema_version", CONTRACT_SCHEMA_VERSION),
        notes=payload.get("notes", ""),
    )
    actual = contract.compute_checksum()
    if stated and stated != actual:
        raise ContractViolationError(
            f"{path}: checksum {stated} does not match the contract's contents ({actual}). "
            "A registration edited after the fact leaves no other trace."
        )
    if contract.schema_version != CONTRACT_SCHEMA_VERSION:
        raise ContractViolationError(
            f"{path}: schema {contract.schema_version!r} is not {CONTRACT_SCHEMA_VERSION!r}"
        )
    return contract


# --- checks that run before any outcome is loaded --------------------------


def validate_configuration(
    contract: ResearchContract,
    *,
    since: datetime,
    until: datetime,
    metric: str,
    comparison: str,
    baseline_policy: str,
    challenger_policies: Sequence[str],
    cost_model_version: str,
    strategy_versions: Sequence[str],
    allow_fallback: bool,
) -> None:
    """Refuse a run whose configuration is not the registered one.

    Called before the first query, on purpose. Checking afterwards would mean
    the outcomes had already been read by a run that turns out not to have been
    the registered experiment, and reading is the irreversible part.
    """
    problems: list[str] = []
    if since != contract.window_since:
        problems.append(
            f"window starts {since.isoformat()}, contract says {contract.window_since.isoformat()}"
        )
    if until != contract.window_until:
        problems.append(
            f"window ends {until.isoformat()}, contract says {contract.window_until.isoformat()}"
        )
    if metric != contract.metric:
        problems.append(f"metric is {metric!r}, contract says {contract.metric!r}")
    if comparison != contract.comparison:
        problems.append(f"comparison is {comparison!r}, contract says {contract.comparison!r}")
    if baseline_policy != contract.baseline_policy:
        problems.append(
            f"baseline is {baseline_policy!r}, contract says {contract.baseline_policy!r}"
        )
    if tuple(challenger_policies) != contract.challenger_policies:
        problems.append(
            f"challengers are {tuple(challenger_policies)!r}, "
            f"contract says {contract.challenger_policies!r}"
        )
    if cost_model_version != contract.cost_model_version:
        problems.append(
            f"cost model is {cost_model_version!r}, contract says {contract.cost_model_version!r}"
        )
    if tuple(strategy_versions) != contract.strategy_versions:
        problems.append(
            f"strategy versions are {tuple(strategy_versions)!r}, "
            f"contract says {contract.strategy_versions!r}"
        )
    if allow_fallback != contract.allow_fallback:
        problems.append(
            f"allow_fallback is {allow_fallback}, contract says {contract.allow_fallback}"
        )
    if problems:
        raise ContractViolationError(
            f"{contract.hypothesis_id}: run does not match its contract. " + "; ".join(problems)
        )


def crosses_window_boundary(contract: ResearchContract, decision_at: datetime) -> bool:
    """Whether this decision's outcome window runs past the contract's window.

    A decision taken twenty minutes before the window closes has a 60-minute
    outcome that is measured on data outside it. In a discovery/holdout split
    those minutes belong to the other side, so the two windows overlap and the
    holdout is no longer independent -- silently, and only near the boundary
    where nobody looks.
    """
    return decision_at + timedelta(minutes=contract.outcome_horizon_minutes) > contract.window_until


# --- the metric, applied as registered --------------------------------------


def compare(
    contract: ResearchContract,
    baseline_values: Sequence[float],
    challenger_values: Sequence[float],
) -> float:
    """Apply the registered metric and comparison. No other path exists.

    `difference_of_metric` and `metric_of_differences` are different numbers.
    HYP-022 registered the first and reported the second, and nothing in the
    code objected because the choice lived at the call site.
    """
    if not baseline_values or not challenger_values:
        raise ContractViolationError("cannot compare empty samples")
    statistic = _METRICS[contract.metric]
    if contract.comparison == "difference_of_metric":
        return statistic(challenger_values) - statistic(baseline_values)
    if len(baseline_values) != len(challenger_values):
        raise ContractViolationError(
            "metric_of_differences requires paired samples of equal length; "
            f"got {len(baseline_values)} and {len(challenger_values)}"
        )
    return statistic(
        [
            challenger - baseline
            for challenger, baseline in zip(challenger_values, baseline_values, strict=True)
        ]
    )


def verdict(
    contract: ResearchContract,
    *,
    value: float | None,
    completed_trades: int,
    clusters: int,
    formal_inference_available: bool = True,
) -> str:
    """The registered decision rule, with no room to reinterpret it.

    The evidence floors are checked before the margins, deliberately. A striking
    number on a thin sample is the easiest thing in the world to promote, and
    the promotion is invisible afterwards.
    """
    if value is None:
        return "inconclusive"
    if completed_trades < contract.minimum_completed_trades:
        return "inconclusive"
    if clusters < contract.minimum_clusters:
        return "inconclusive"
    if not formal_inference_available:
        return "inconclusive"
    if value >= contract.candidate_margin:
        return "candidate"
    if value <= contract.rejection_margin:
        return "rejected"
    return "inconclusive"


# --- the sample, frozen on the first run ------------------------------------


@dataclass(frozen=True)
class SampleManifest:
    """Which episodes a contract's first run actually measured.

    Without this, "read once" is a sentence a report prints about itself. A
    second run picks up whatever has accumulated since, reports the same
    sentence, and the difference between the two numbers is invisible.
    """

    hypothesis_id: str
    contract_checksum: str
    episode_ids: tuple[int, ...]
    frozen_at: str

    @property
    def sample_checksum(self) -> str:
        encoded = ",".join(str(value) for value in sorted(self.episode_ids)).encode()
        return hashlib.sha256(encoded).hexdigest()

    def to_json(self) -> str:
        payload = asdict(self)
        payload["episode_ids"] = sorted(self.episode_ids)
        payload["sample_checksum"] = self.sample_checksum
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def freeze_or_verify_sample(
    contract: ResearchContract,
    episode_ids: Sequence[int],
    path: Path,
) -> SampleManifest:
    """Record the sample on the first run; require it to match on every later one.

    A re-run that finds different episodes is not a re-run. It is a second
    experiment on overlapping data, and reporting it under the same hypothesis
    id is how one registered question quietly becomes several.
    """
    manifest = SampleManifest(
        hypothesis_id=contract.hypothesis_id,
        contract_checksum=contract.compute_checksum(),
        episode_ids=tuple(sorted(episode_ids)),
        frozen_at=datetime.now(UTC).isoformat(),
    )
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(manifest.to_json())
        return manifest

    stored = json.loads(path.read_text())
    if stored["hypothesis_id"] != contract.hypothesis_id:
        raise ContractViolationError(
            f"{path} belongs to {stored['hypothesis_id']}, not {contract.hypothesis_id}"
        )
    if stored["contract_checksum"] != manifest.contract_checksum:
        raise ContractViolationError(
            f"{contract.hypothesis_id}: the contract changed since this sample was frozen. "
            "The registration was edited after the experiment was defined."
        )
    if stored["sample_checksum"] != manifest.sample_checksum:
        stored_ids = set(stored["episode_ids"])
        current_ids = set(manifest.episode_ids)
        raise ContractViolationError(
            f"{contract.hypothesis_id}: this run measures a different sample than the frozen one. "
            f"{len(current_ids - stored_ids)} episodes appeared, "
            f"{len(stored_ids - current_ids)} disappeared. "
            "A re-run that finds different episodes is a second experiment."
        )
    return manifest
