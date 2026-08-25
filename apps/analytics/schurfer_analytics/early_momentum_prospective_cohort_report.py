"""`feat/early-momentum-prospective-cohort-v1` -- read-only status for the
genuinely fresh `early_momentum_v4` cohort this PR exists to isolate from
history, on the way to a live-probe eligibility decision.

**Deliberately reuses the `early_momentum_net_evidence_report` input/builder pipeline
end to end (repository query, funnel, economics, concurrency, robustness,
capacity) rather than reimplementing any of it.** That module already
computes everything this status needs -- resolved/unresolved funnel,
accounting-complete counts, executable net EV/profit factor/median/
drawdown/losing-streak, distinct assets/weeks, concurrency and capital
occupancy, and a rejection funnel with cohort- and row-level integrity
violations (including a mismatched/unexpected `contract_sha256` already
excluded fail-closed, see `early_momentum_net_evidence.py`'s own
`unexpected_hashes`/`hash_ok` filtering) -- from one already-tested,
already-shipped pipeline. Building a second, parallel economics engine for
this report would be exactly the kind of duplication this codebase's own
research discipline exists to avoid, and would risk the two silently
drifting apart on what "accounting complete" or "profit factor" means.

The only two things genuinely new here:

1. **A separate, later cohort boundary**, registered with the database's
   own clock by execution startup before scanner/trigger tasks begin, then
   passed to the shared evidence input query as `cohort_start`.
2. **A narrower promotion vocabulary**
   (`collecting`/`blocked_integrity`/`fail`/`eligible_for_live_probe_review`) purpose-built for
   a live-probe go/no-go read, mapped from the existing, already-tested
   4-state `Verdict` (`invalid_integrity`/`insufficient_data`/`fail`/
   `pass_live_micro_candidate`) rather than re-deriving the underlying
   evidence-floor/profit-factor/bootstrap/leave-one-out gates a second
   time. Integrity violations and dirty runs map to `blocked_integrity`,
   while eligibility additionally requires a validated immutable artifact.

## Why the contract-hash check here is EXTRA meaningful, not redundant

`early_momentum.py` (execution side) now pins `CONTRACT_SHA256` to a
checked literal that raises at import time on any silent parameter drift
(feat/early-momentum-prospective-cohort-v1). `EXPECTED_CONTRACT_SHA256_HEX`
here (imported from `early_momentum_net_evidence.py`) is the SAME literal
value, duplicated across packages -- `schurfer-analytics` does not depend
on `schurfer-execution` (see that package's own `pyproject.toml`), so this
cannot be a real cross-package import; it is kept in sync by convention,
the same way `liquidation_cascade_repository.py`'s own "must track
apps/execution/..." comments already do elsewhere in this codebase. If the
execution-side contract ever changes on purpose, both this literal AND
early_momentum.py's own `_EXPECTED_CONTRACT_SHA256_HEX` are updated in the
SAME commit -- a mismatch between the two would silently and permanently
exclude every newly-armed episode from ever reaching `eligible_for_live_
probe_review`, so a currently-passing `test_expected_contract_sha256_
matches_the_pinned_execution_side_literal`-style comment is not optional
housekeeping.

## No orders

This module is read-only end to end -- it never calls `execution_intent`,
`orders`, or any broker. `eligible_for_live_probe_review` is a signal for a
human to START the live-probe safety PR (budgets, circuit breaker, kill
switch, reconciliation) -- never an automatic authorization for a single
live order.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import asdict, dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from .early_momentum_net_evidence import EXPECTED_CONTRACT_SHA256_HEX, Verdict
from .early_momentum_net_evidence_report import (
    CohortNotMatureError,
    NetEvidenceReport,
    build_report,
    fetch_report_inputs,
)
from .early_momentum_net_evidence_report import (
    render_markdown as render_evidence_markdown,
)
from .outcome_repository import async_database_url
from .reporting import json_ready as _json_ready
from .reporting import normalize_code_revision, parse_utc_datetime
from .research_dataset_artifact import DatasetArtifactManifest, ResearchDatasetArtifactWriteError

PROSPECTIVE_REPORT_VERSION = "early_momentum_prospective_cohort_v1"
PROSPECTIVE_COHORT_KEY = "early_momentum_v4_prospective_v1"
EXPECTED_RUNTIME_POLICY_SHA256_HEX = (
    "720888b733bc097d53071b26edd5b85b4bb6dcc295a386fc1dc6590f9a2888d8"
)

PROSPECTIVE_VERDICT_COLLECTING = "collecting"
PROSPECTIVE_VERDICT_FAIL = "fail"
PROSPECTIVE_VERDICT_ELIGIBLE = "eligible_for_live_probe_review"
PROSPECTIVE_VERDICT_BLOCKED = "blocked_integrity"


class ProspectiveCohortNotStartedError(Exception):
    """No execution startup has durably registered this cohort yet."""


@dataclass(frozen=True)
class CohortRegistration:
    cohort_key: str
    strategy_name: str
    strategy_version: str
    contract_sha256: str
    runtime_policy_sha256: str
    cohort_started_at: datetime


def _validate_registration(registration: CohortRegistration) -> None:
    if registration.cohort_started_at.tzinfo is None:
        raise ValueError("prospective cohort registration timestamp must be timezone-aware")
    expected = {
        "cohort_key": PROSPECTIVE_COHORT_KEY,
        "strategy_name": "early_momentum",
        "strategy_version": "4",
        "contract_sha256": EXPECTED_CONTRACT_SHA256_HEX,
        "runtime_policy_sha256": EXPECTED_RUNTIME_POLICY_SHA256_HEX,
    }
    actual = asdict(registration)
    mismatches = {
        key: {"expected": value, "actual": actual[key]}
        for key, value in expected.items()
        if actual[key] != value
    }
    if mismatches:
        raise ValueError(f"prospective cohort registration mismatch: {mismatches}")


async def load_registration(db_url: str) -> CohortRegistration:
    engine = create_async_engine(async_database_url(db_url), pool_size=1, max_overflow=0)
    try:
        async with engine.connect() as connection:
            row = (
                (
                    await connection.execute(
                        text(
                            """
                        SELECT cohort_key, strategy_name, strategy_version,
                               encode(contract_sha256, 'hex') AS contract_sha256,
                               encode(runtime_policy_sha256, 'hex') AS runtime_policy_sha256,
                               cohort_started_at
                        FROM app.research_cohort_registrations
                        WHERE cohort_key = :cohort_key
                        """
                        ),
                        {"cohort_key": PROSPECTIVE_COHORT_KEY},
                    )
                )
                .mappings()
                .one_or_none()
            )
    finally:
        await engine.dispose()
    if row is None:
        raise ProspectiveCohortNotStartedError(
            f"cohort {PROSPECTIVE_COHORT_KEY!r} has not been registered by execution startup"
        )
    registration = CohortRegistration(**dict(row))
    _validate_registration(registration)
    return registration


def _registration_from_dict(payload: dict[str, object]) -> CohortRegistration:
    registration = CohortRegistration(
        cohort_key=str(payload["cohort_key"]),
        strategy_name=str(payload["strategy_name"]),
        strategy_version=str(payload["strategy_version"]),
        contract_sha256=str(payload["contract_sha256"]),
        runtime_policy_sha256=str(payload["runtime_policy_sha256"]),
        cohort_started_at=datetime.fromisoformat(str(payload["cohort_started_at"])),
    )
    _validate_registration(registration)
    return registration


def map_verdict_to_prospective(
    verdict: Verdict, *, formal_run: bool = True, immutable_artifact: bool = True
) -> tuple[str, tuple[str, ...]]:
    """Pure mapping, independently testable from the underlying (already
    well-tested) `evaluate_verdict` gate logic itself."""
    from .early_momentum_net_evidence import (
        VERDICT_FAIL,
        VERDICT_INSUFFICIENT_DATA,
        VERDICT_INVALID_INTEGRITY,
        VERDICT_PASS_LIVE_MICRO_CANDIDATE,
    )

    if not formal_run:
        return PROSPECTIVE_VERDICT_BLOCKED, ("non_formal_run",)
    if verdict.verdict == VERDICT_INVALID_INTEGRITY:
        return PROSPECTIVE_VERDICT_BLOCKED, (
            "cannot_evaluate_integrity_violation",
            *verdict.reasons,
        )
    if verdict.verdict == VERDICT_INSUFFICIENT_DATA:
        return PROSPECTIVE_VERDICT_COLLECTING, verdict.reasons
    if verdict.verdict == VERDICT_FAIL:
        return PROSPECTIVE_VERDICT_FAIL, verdict.reasons
    if verdict.verdict == VERDICT_PASS_LIVE_MICRO_CANDIDATE:
        if not immutable_artifact:
            return PROSPECTIVE_VERDICT_COLLECTING, ("immutable_artifact_required_for_eligibility",)
        return PROSPECTIVE_VERDICT_ELIGIBLE, ()
    raise ValueError(  # pragma: no cover
        f"unrecognized underlying verdict state: {verdict.verdict!r}"
    )


@dataclass(frozen=True)
class ProspectiveCohortReport:
    report_version: str
    prospective_cohort_started_at: datetime
    prospective_verdict: str
    prospective_verdict_reasons: tuple[str, ...]
    cohort_registration: CohortRegistration
    source_artifact: dict[str, object] | None
    # The full underlying evidence report (funnel, economics, concurrency,
    # robustness, capacity, dataset_fingerprint, and its own 4-state
    # Verdict) -- nothing here is recomputed, only relabeled/wrapped.
    evidence: NetEvidenceReport


async def generate_prospective_cohort_report(
    *,
    db_url: str | None,
    cohort_end: datetime,
    code_revision: str,
    working_tree_dirty: bool,
    freeze_artifact: bool = False,
    from_artifact: str | None = None,
    artifact_directory: str | None = None,
) -> ProspectiveCohortReport:
    from . import early_momentum_net_evidence_dataset_artifact as artifact

    if freeze_artifact and from_artifact is not None:
        raise ValueError("--freeze-artifact and --from-artifact are mutually exclusive")
    if freeze_artifact and working_tree_dirty:
        raise ValueError(
            "--freeze-artifact requires a clean working tree; refusing to publish "
            "non-formal provenance into the immutable first-writer-wins store"
        )
    source_manifest: DatasetArtifactManifest | None = None
    if from_artifact is not None:
        source_manifest, dataset, legacy_context, raw_registration = artifact.read(
            from_artifact, directory=artifact_directory
        )
        registration = _registration_from_dict(raw_registration)
        if dataset.cohort_start != registration.cohort_started_at:
            raise ValueError(
                "artifact cohort start does not match its durable registration: "
                f"{dataset.cohort_start.isoformat()} != "
                f"{registration.cohort_started_at.isoformat()}"
            )
        if dataset.cohort_end != cohort_end:
            raise ValueError(
                f"--cohort-end {cohort_end.isoformat()} does not match artifact end "
                f"{dataset.cohort_end.isoformat()}"
            )
    else:
        if not db_url:
            raise ValueError("DATABASE_URL is required unless --from-artifact is used")
        registration = await load_registration(db_url)
        dataset, legacy_context = await fetch_report_inputs(
            db_url=db_url,
            cohort_start=registration.cohort_started_at,
            cohort_end=cohort_end,
        )
    evidence = build_report(
        dataset=dataset,
        legacy_context=legacy_context,
        code_revision=code_revision,
        working_tree_dirty=working_tree_dirty,
    )
    if freeze_artifact:
        outcome, source_manifest = artifact.freeze(
            dataset,
            legacy_context,
            registration=_json_ready(asdict(registration)),
            code_revision=code_revision,
            working_tree_dirty=working_tree_dirty,
            directory=artifact_directory,
        )
        if source_manifest is None:
            raise ResearchDatasetArtifactWriteError(f"--freeze-artifact failed: {outcome.value}")
    source_artifact = (
        {
            "fingerprint": source_manifest.fingerprint,
            "dataset_name": source_manifest.dataset_name,
            "dataset_version": source_manifest.dataset_version,
            "code_revision": source_manifest.code_revision,
            "working_tree_dirty": source_manifest.working_tree_dirty,
        }
        if source_manifest is not None
        else None
    )
    normalized_revision = normalize_code_revision(code_revision)
    artifact_formal = (
        source_manifest is not None
        and not source_manifest.working_tree_dirty
        and source_manifest.code_revision == normalized_revision
    )
    prospective_verdict, reasons = map_verdict_to_prospective(
        evidence.verdict,
        formal_run=evidence.formal_run and (source_manifest is None or artifact_formal),
        immutable_artifact=source_manifest is not None,
    )
    return ProspectiveCohortReport(
        report_version=PROSPECTIVE_REPORT_VERSION,
        prospective_cohort_started_at=registration.cohort_started_at,
        prospective_verdict=prospective_verdict,
        prospective_verdict_reasons=reasons,
        cohort_registration=registration,
        source_artifact=source_artifact,
        evidence=evidence,
    )


def render_json(report: ProspectiveCohortReport) -> str:
    return json.dumps(_json_ready(asdict(report)), indent=2, sort_keys=True, default=str)


def render_markdown(report: ProspectiveCohortReport) -> str:
    lines = [
        f"# early_momentum prospective cohort -- {report.prospective_verdict}",
        "",
        f"report_version: `{report.report_version}`",
        f"prospective_cohort_started_at: {report.prospective_cohort_started_at.isoformat()}",
        f"prospective_verdict: `{report.prospective_verdict}`",
    ]
    if report.source_artifact is not None:
        lines.append(f"source_artifact: `{report.source_artifact['fingerprint']}`")
    if report.prospective_verdict_reasons:
        lines.append("reasons: " + ", ".join(f"`{r}`" for r in report.prospective_verdict_reasons))
    lines += [
        "",
        "> Read-only. No orders are ever placed by this report. "
        f"`{PROSPECTIVE_VERDICT_ELIGIBLE}` is a signal to START the live-probe safety PR "
        "(budgets, circuit breaker, kill switch, reconciliation) -- never an automatic "
        "authorization for a single live order.",
        "",
        "## Underlying evidence report",
        "",
    ]
    lines.append(render_evidence_markdown(report.evidence))
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only live-probe-eligibility status for the early_momentum "
        "prospective cohort"
    )
    parser.add_argument(
        "--cohort-end",
        type=parse_utc_datetime,
        required=True,
        help="exclusive UTC ISO-8601 cohort end; must be >= 6h older than the DB's own clock",
    )
    parser.add_argument("--code-revision", type=str, required=True)
    dirty = parser.add_mutually_exclusive_group(required=True)
    dirty.add_argument("--no-working-tree-dirty", action="store_true")
    dirty.add_argument("--working-tree-dirty", action="store_true")
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--freeze-artifact", action="store_true")
    source.add_argument("--from-artifact")
    parser.add_argument("--artifact-directory")
    return parser


async def _run(args: argparse.Namespace) -> str:
    db_url = os.getenv("DATABASE_URL")
    if not db_url and not args.from_artifact:
        raise ValueError("DATABASE_URL is required for early-momentum-prospective-cohort-report")
    report = await generate_prospective_cohort_report(
        db_url=db_url,
        cohort_end=args.cohort_end,
        code_revision=normalize_code_revision(args.code_revision),
        working_tree_dirty=bool(args.working_tree_dirty),
        freeze_artifact=bool(args.freeze_artifact),
        from_artifact=args.from_artifact,
        artifact_directory=args.artifact_directory,
    )
    return render_json(report) if args.format == "json" else render_markdown(report)


def main() -> None:
    args = build_parser().parse_args()
    sys.stdout.write(asyncio.run(_run(args)))


__all__ = [
    "EXPECTED_CONTRACT_SHA256_HEX",
    "EXPECTED_RUNTIME_POLICY_SHA256_HEX",
    "PROSPECTIVE_COHORT_KEY",
    "PROSPECTIVE_REPORT_VERSION",
    "PROSPECTIVE_VERDICT_BLOCKED",
    "PROSPECTIVE_VERDICT_COLLECTING",
    "PROSPECTIVE_VERDICT_ELIGIBLE",
    "PROSPECTIVE_VERDICT_FAIL",
    "CohortNotMatureError",
    "CohortRegistration",
    "ProspectiveCohortNotStartedError",
    "ProspectiveCohortReport",
    "generate_prospective_cohort_report",
    "load_registration",
    "map_verdict_to_prospective",
    "render_json",
    "render_markdown",
]
