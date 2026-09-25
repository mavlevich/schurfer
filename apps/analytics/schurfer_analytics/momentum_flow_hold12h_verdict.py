"""HYP-015 hold12h verdict -- the DRAFT contract and the pure verdict rule.

**DRAFT / NOT FROZEN, NOT REGISTERED.** This module fixes the FORM of how the
720m-vs-240m hold-duration question is answered (thresholds shape, gate order, the
pure decision), but the overall HYP-015 contract is NOT registered: the diversity /
concentration / improvement constants are PROVISIONAL (to be sized from an
outcome-blind pre-start accrual) and the ACTUAL-funding source is a prerequisite that
does not exist yet (a prospective per-instrument settlement capture, its own PR). A
``formal_run`` therefore fail-closes until both are resolved and a literal future
UTC cohort boundary is registered in a small freeze PR. Nothing here reads returns.

It deliberately contains NO database access and NO outcome data -- only the
thresholds (``Hold12hVerdictContract``) and the pure ordered-gate decision
(``decide_verdict``). The reader (``momentum_flow_hold12h_verdict_report.py``)
computes the statistics from real Postgres and feeds them here; the split keeps the
safety-critical rule unit-testable and independent of the query.

Design and rationale live in docs/research/momentum-flow-hold12h-verdict-v1.md.
The gate ORDER is itself part of the contract: negative economics (Gate B) binds
BEFORE the diversity floor (Gate C), so a mature-but-narrow losing sample resolves
to a rejection, never to "insufficient data" -- "less bad" is not an edge.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, fields
from enum import StrEnum
from hashlib import sha256

from .clustered_inference import CLUSTER_BOOTSTRAP_VERSION
from .momentum_flow_paper_contract import HOLD12H_PAPER_CONTRACT_SHA256


class VerdictOutcome(StrEnum):
    """The mutually exclusive results, in gate order. Only ``CANDIDATE`` authorizes
    the next research gate (episode study / shadow), and NEVER live trading."""

    INSUFFICIENT_DATA = "insufficient_data"
    REJECT_HOLD12H = "reject_hold12h"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    NO_DURATION_IMPROVEMENT = "no_duration_improvement"
    CANDIDATE = "candidate"


# Versioned so a receipt/artifact built under one construction is never compared
# against another. Bumped when any threshold, gate order, or estimand changes.
CONTRACT_VERSION = "hold12h_verdict_v1"

# The actual-funding contract the reader must satisfy (see the report module). Named
# here so the verdict artifact records which funding construction produced its net, and
# the single source for the capture and the stored source. v2 changed what ``complete``
# means (source-completeness of the Bybit v5 history queried by native id; see the
# resolver), so v1 and v2 coverage runs are never mixed under one identifier.
ACTUAL_FUNDING_VERSION = "hold12h_actual_funding_v2"


@dataclass(frozen=True)
class Hold12hVerdictContract:
    """Every threshold the verdict depends on, hashed into a single sha so a run can
    prove which contract produced it. NOT a registered/frozen contract yet (see module).

    PROVISIONAL VALUES (marked below) are placeholders to be frozen from the
    outcome-blind pre-start accrual (counts only, no returns) BEFORE this contract is
    activated; they are concrete here so the rule is testable and the sha is defined,
    but the reviewer confirms them against the accrual before merge. The maturity and
    significance machinery (pairs floor, CI, gate order) is NOT provisional.
    """

    contract_version: str = CONTRACT_VERSION
    actual_funding_version: str = ACTUAL_FUNDING_VERSION
    paper_contract_sha256: str = HOLD12H_PAPER_CONTRACT_SHA256

    # Gate A -- economic maturity (primary, diversity-independent).
    min_analyzable_pairs: int = 100

    # Gate C -- diversity / concentration / missingness floor. PROVISIONAL: sizes from
    # the pre-start accrual; do NOT copy HYP-012's 7 (that came from a 14-asset universe).
    min_distinct_asset_clusters: int = 20  # PROVISIONAL (target ~20-30, from accrual)
    min_distinct_utc_weeks: int = 4
    max_single_asset_fraction: float = 0.25  # PROVISIONAL concentration cap
    max_single_week_fraction: float = 0.40  # PROVISIONAL concentration cap
    max_rejected_stale_fraction: float = 0.50  # PROVISIONAL missingness ceiling
    max_unresolved_fraction: float = 0.20  # PROVISIONAL missingness ceiling
    max_accounting_incomplete_fraction: float = 0.20  # PROVISIONAL missingness ceiling
    max_identity_unresolved_fraction: float = 0.05  # PROVISIONAL; unresolved identity
    # NOTE: integrity failures have NO tolerance -- ANY of them fail-closes the verdict
    # (see Gate 0b); a NaN/invalid row is a defect, not acceptable missingness.

    # Gate D/E -- significance and duration improvement.
    confidence_level: float = 0.95
    # Gate E -- fixed-$300-bank portfolio must beat 240m by a real dollar margin, not
    # merely tie. PROVISIONAL: 5% of the $300 bank.
    min_portfolio_improvement_usd: float = 15.0

    # Fixed-bank portfolio parameters -- FROZEN into the sha so one contract sha means
    # exactly one computation (otherwise the same sha could produce different results).
    bank_usd: float = 300.0
    position_usd: float = 50.0
    max_concurrent_slots: int = 6
    # Deterministic, OUTCOME-BLIND selection when slots are full: consider entries in
    # (entry_at, watch_id) order and skip the arrival when no slot is free -- never a
    # function of any return.
    selection_rule: str = "entry_at_then_watch_id_skip_when_full_v1"
    # The REPORTED adverse-excursion diagnostic method (worst simultaneous adverse-from-
    # entry). NOT a gated risk metric and NOT a true drawdown: the writer stores min-
    # return-from-entry, not peak-to-trough. Named honestly; the verdict does not gate on it.
    adverse_diagnostic_method: str = "adverse_from_entry_diagnostic_v1"
    # Whether two 720m positions of the SAME canonical asset may be open at once. FROZEN
    # here rather than left implicit: True matches the live 360m cooldown, which permits it.
    allow_concurrent_same_asset: bool = True

    # Cluster bootstrap (clustered by canonical asset) -- FROZEN. Version is imported from
    # the shared implementation so the contract string cannot drift from the real method.
    bootstrap_version: str = CLUSTER_BOOTSTRAP_VERSION
    bootstrap_iterations: int = 10_000
    bootstrap_seed: int = 20_260_729

    # The LITERAL formal cohort boundary, an ISO-8601 UTC instant. None until the freeze
    # PR sets a concrete FUTURE instant; while None a formal_run fail-closes because the
    # contract is not registered. Runtime first-writer registration is a DRAFT/readiness
    # convenience only and never substitutes for this frozen literal.
    cohort_start_iso: str | None = None

    def __post_init__(self) -> None:
        if self.min_analyzable_pairs <= 0:
            raise ValueError("min_analyzable_pairs must be positive")
        if self.min_distinct_asset_clusters <= 0:
            raise ValueError("min_distinct_asset_clusters must be positive")
        if self.min_distinct_utc_weeks <= 0:
            raise ValueError("min_distinct_utc_weeks must be positive")
        if not 0 < self.confidence_level < 1:
            raise ValueError("confidence_level must be between zero and one")
        for name in (
            "max_single_asset_fraction",
            "max_single_week_fraction",
            "max_rejected_stale_fraction",
            "max_unresolved_fraction",
            "max_accounting_incomplete_fraction",
            "max_identity_unresolved_fraction",
        ):
            value = getattr(self, name)
            if not 0 < value <= 1:
                raise ValueError(f"{name} must be in (0, 1]")
        if self.min_portfolio_improvement_usd <= 0:
            raise ValueError("min_portfolio_improvement_usd must be positive")
        if self.bank_usd <= 0 or self.position_usd <= 0:
            raise ValueError("bank_usd and position_usd must be positive")
        if self.max_concurrent_slots <= 0:
            raise ValueError("max_concurrent_slots must be positive")
        if self.position_usd * self.max_concurrent_slots > self.bank_usd:
            raise ValueError("position_usd * max_concurrent_slots must not exceed bank_usd")
        if self.bootstrap_iterations < 100:
            raise ValueError("bootstrap_iterations must be at least 100")
        if self.bootstrap_seed < 0:
            raise ValueError("bootstrap_seed must not be negative")

    def canonical_json(self) -> str:
        import json

        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))

    def sha256_hex(self) -> str:
        return sha256(self.canonical_json().encode()).hexdigest()


@dataclass(frozen=True)
class VerdictInputs:
    """The statistics the reader computes from the frozen cohort and hands to the pure
    rule. Every field is over the analyzable pairs unless named otherwise. Returns are
    net of fees and ACTUAL funding. The reader never applies the rule itself.
    """

    analyzable_pairs: int
    # Whether a cluster-bootstrap CI could be computed at all (enough clusters/pairs).
    ci_computable: bool

    # Standalone 720m economics (the "is it profitable on its own" question).
    standalone_720_mean_net: float
    standalone_720_ci_lower: float

    # Paired within-probe duration effect d(p) = net_720 - net_240cf (same entry VWAP).
    paired_diff_ci_lower: float

    # Diversity / concentration (Gate C).
    distinct_asset_clusters: int
    distinct_utc_weeks: int
    max_single_asset_fraction: float
    max_single_week_fraction: float

    # Missingness fractions over the full denominator (Gate C).
    rejected_stale_fraction: float
    unresolved_fraction: float
    accounting_incomplete_fraction: float
    identity_unresolved_fraction: float
    integrity_failure_fraction: float

    # Fixed-$300-bank portfolio replay, same WATCH stream, both policies (Gate E).
    portfolio_720_window_pnl_usd: float
    portfolio_240_window_pnl_usd: float


@dataclass(frozen=True)
class VerdictResult:
    outcome: VerdictOutcome
    gate: str  # the gate that decided it ("A".."E" or "pass")
    reason: str


def _non_finite_fields(inputs: VerdictInputs) -> list[str]:
    """Every float field of ``inputs`` that is NaN or infinite -- an integrity failure
    that must fail-closed rather than flow into a verdict."""
    bad: list[str] = []
    for f in fields(inputs):
        value = getattr(inputs, f.name)
        if isinstance(value, float) and not math.isfinite(value):
            bad.append(f.name)
    return bad


def decide_verdict(contract: Hold12hVerdictContract, inputs: VerdictInputs) -> VerdictResult:
    """The one pure decision, evaluated once at the pre-defined decision-time prefix.

    Ordered gates, FIRST MATCH WINS. The order encodes the pre-registration's core
    honesty guarantees:

      0  integrity            -- any NaN/inf input          => insufficient_data (fail-closed)
      A  economic maturity    -- too few analyzable pairs   => insufficient_data
      B  negative EV first    -- mature AND mean net <= 0   => reject_hold12h
      C  diversity/missingness-- floors not met             => insufficient_data
      D  standalone signif.   -- no CI or CI lower not > 0  => insufficient_evidence
      E  duration + portfolio -- no paired edge / not a     => no_duration_improvement
                                  PROFITABLE $ win over 240m
      -- otherwise                                          => candidate
    """
    # Gate 0 -- integrity. A NaN/inf return, rate, fraction or PnL must never reach a
    # substantive verdict (a NaN comparison is always False, so it could otherwise slip
    # through every gate to "candidate"). Fail-closed.
    bad = _non_finite_fields(inputs)
    if bad:
        return VerdictResult(
            VerdictOutcome.INSUFFICIENT_DATA, "0", f"non-finite inputs: {', '.join(bad)}"
        )

    # Gate 0b -- integrity. ANY integrity failure (a NaN/invalid row, a non-positive
    # notional, a duplicate settlement) blocks the verdict regardless of its fraction: a
    # corrupt input is a defect, never acceptable missingness. Fail-closed.
    if inputs.integrity_failure_fraction > 0:
        return VerdictResult(
            VerdictOutcome.INSUFFICIENT_DATA,
            "0b",
            f"integrity failures present ({inputs.integrity_failure_fraction:.4f} of the "
            "denominator); a formal verdict requires zero",
        )

    # Gate A -- economic maturity, by pair COUNT alone. Deliberately independent of the
    # CI being computable: a mature-but-narrow sample must still reach Gate B (its mean
    # is computable without clusters), so a mature loser cannot hide as insufficient_data
    # merely because it has too few clusters for a bootstrap.
    if inputs.analyzable_pairs < contract.min_analyzable_pairs:
        return VerdictResult(
            VerdictOutcome.INSUFFICIENT_DATA,
            "A",
            f"analyzable pairs {inputs.analyzable_pairs} < {contract.min_analyzable_pairs}",
        )

    # Gate B -- negative EV binds BEFORE the diversity floor and BEFORE the CI. "Less
    # bad" is not an edge: a mature standalone mean net <= 0 is a rejection. The mean is
    # a plain average over the pairs, computable regardless of cluster count.
    if inputs.standalone_720_mean_net <= 0:
        return VerdictResult(
            VerdictOutcome.REJECT_HOLD12H,
            "B",
            f"mature standalone 720m mean net {inputs.standalone_720_mean_net:.6f} <= 0",
        )

    # Gate C -- diversity / concentration / missingness (only reachable once EV is not
    # negative, so a narrow losing sample can never hide here).
    if inputs.distinct_asset_clusters < contract.min_distinct_asset_clusters:
        return VerdictResult(
            VerdictOutcome.INSUFFICIENT_DATA,
            "C",
            f"distinct asset clusters {inputs.distinct_asset_clusters}"
            f" < {contract.min_distinct_asset_clusters}",
        )
    if inputs.distinct_utc_weeks < contract.min_distinct_utc_weeks:
        return VerdictResult(
            VerdictOutcome.INSUFFICIENT_DATA,
            "C",
            f"distinct UTC weeks {inputs.distinct_utc_weeks} < {contract.min_distinct_utc_weeks}",
        )
    if inputs.max_single_asset_fraction > contract.max_single_asset_fraction:
        return VerdictResult(
            VerdictOutcome.INSUFFICIENT_DATA,
            "C",
            f"single-asset concentration {inputs.max_single_asset_fraction:.3f}"
            f" > {contract.max_single_asset_fraction}",
        )
    if inputs.max_single_week_fraction > contract.max_single_week_fraction:
        return VerdictResult(
            VerdictOutcome.INSUFFICIENT_DATA,
            "C",
            f"single-week concentration {inputs.max_single_week_fraction:.3f}"
            f" > {contract.max_single_week_fraction}",
        )
    for name, ceiling in (
        ("rejected_stale_fraction", contract.max_rejected_stale_fraction),
        ("unresolved_fraction", contract.max_unresolved_fraction),
        ("accounting_incomplete_fraction", contract.max_accounting_incomplete_fraction),
        ("identity_unresolved_fraction", contract.max_identity_unresolved_fraction),
    ):
        value = getattr(inputs, name)
        if value > ceiling:
            return VerdictResult(
                VerdictOutcome.INSUFFICIENT_DATA,
                "C",
                f"missingness {name} {value:.3f} > {ceiling}",
            )

    # Gate D -- standalone significance. Requires a computable cluster-bootstrap CI whose
    # lower bound is > 0; a positive point estimate whose CI still crosses zero, or a
    # sample too narrow to bootstrap at all, is not yet evidence.
    if not inputs.ci_computable:
        return VerdictResult(
            VerdictOutcome.INSUFFICIENT_EVIDENCE,
            "D",
            "cluster-bootstrap CI not computable (too few asset clusters)",
        )
    if not inputs.standalone_720_ci_lower > 0:
        return VerdictResult(
            VerdictOutcome.INSUFFICIENT_EVIDENCE,
            "D",
            f"standalone 720m net CI lower {inputs.standalone_720_ci_lower:.6f} not > 0",
        )

    # Gate E -- duration improvement AND a PROFITABLE fixed-bank win. The paired effect
    # must be significant, the 720m $300 bank must itself make money in ABSOLUTE terms
    # (not merely lose less than 240m), and it must beat 240m by a real dollar margin.
    # (Drawdown is reported as a diagnostic but NOT gated: the stored MAE is from-entry,
    # not peak-to-trough, so it is not a trustworthy risk bound.)
    portfolio_improvement = (
        inputs.portfolio_720_window_pnl_usd - inputs.portfolio_240_window_pnl_usd
    )
    if not inputs.paired_diff_ci_lower > 0:
        return VerdictResult(
            VerdictOutcome.NO_DURATION_IMPROVEMENT,
            "E",
            f"paired d(p) CI lower {inputs.paired_diff_ci_lower:.6f} not > 0",
        )
    if inputs.portfolio_720_window_pnl_usd <= 0:
        return VerdictResult(
            VerdictOutcome.NO_DURATION_IMPROVEMENT,
            "E",
            f"720m fixed-bank window PnL ${inputs.portfolio_720_window_pnl_usd:.2f}"
            " not profitable (beating a losing 240m is not an edge)",
        )
    if portfolio_improvement < contract.min_portfolio_improvement_usd:
        return VerdictResult(
            VerdictOutcome.NO_DURATION_IMPROVEMENT,
            "E",
            f"portfolio improvement ${portfolio_improvement:.2f}"
            f" < ${contract.min_portfolio_improvement_usd}",
        )

    return VerdictResult(
        VerdictOutcome.CANDIDATE,
        "pass",
        "mature, diverse, standalone-significant, paired edge, and beats 240m on the $300 bank",
    )


# DRAFT contract instance. NOT registered/frozen: the provisional constants above and
# the unimplemented actual-funding prerequisite keep the overall HYP-015 contract
# unregistered. A small freeze PR fixes the final constants, the funding version, and a
# literal future UTC cohort boundary before any formal run is allowed.
REGISTERED = False
HOLD12H_VERDICT_CONTRACT = Hold12hVerdictContract()
