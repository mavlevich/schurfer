"""Bounded sensitivity analysis for the Bybit order-flow pilot's endpoint-staleness
completeness bound.

Registered 2026-08-06 (see ROADMAP.md's `gate_inconclusive_endpoint_completeness`
entry): the v1 pilot report (`orderflow_pilot_report.py`) locks
`ORDERFLOW_MAX_ENDPOINT_STALENESS_MS` at 5000ms and applies it independently at
the anchor plus four post-trigger horizons, across the event and all 3 controls —
roughly 20 conditions that must all pass. On Bybit's actual per-symbol trade
frequency for pump candidates, that leaves only ~1.4% of captured episodes
"complete" (8 of 586 on the 2026-08-05 read), nowhere near the registered 100
complete / 30 clusters / 7 days threshold.

This is a read-only, discovery-only diagnostic. It does not touch, import as
mutable, or change `orderflow_pilot_report.py`'s registered `v1` constant,
contract, or output in any way — it re-parses the SAME raw capture files and
reuses v1's own bound-independent helpers (record validation, per-window
accumulation, lane-return math) directly, adding only the one piece v1's public
dataclasses don't expose: the raw anchor/horizon close and staleness values
BEFORE any staleness bound is applied, so multiple candidate bounds can be
evaluated from a single parse instead of six full re-parses.

Explicitly out of scope for this report (see ROADMAP.md): ticker/mid capture,
5-6 controls, a 24-hour accumulation layer, liquidity buckets, and an after-cost
simulator. None of that data exists in the current capture, and none of it is
needed to answer the one question this report exists for: does any lane show a
pre-trigger effect that holds up across a defensible range of the bound, or does
it only appear once the bound is loosened past the point where it still means
anything for a ~1-minute lane?
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .orderflow_pilot_report import (
    LEAD_WINDOWS,
    ORDERFLOW_COHORT_START,
    ORDERFLOW_CONTRACT_VERSION,
    ORDERFLOW_MAX_ENDPOINT_STALENESS_MS,
    ORDERFLOW_REQUIRED_CONTROLS,
    POST_HORIZONS_SECONDS,
    CaptureEpisode,
    CountRow,
    HorizonMetrics,
    LaneRow,
    LeadMetrics,
    SubjectObservation,
    _episode_parts,
    _HorizonAccumulator,
    _lane_rows,
    _validated_record,
    _WindowAccumulator,
)
from .reporting import json_ready, markdown_table, normalize_code_revision, parse_utc_datetime

ORDERFLOW_SENSITIVITY_REPORT_VERSION = "bybit_orderflow_endpoint_sensitivity_v1"
# Round steps from the registered v1 bound up to the point where staleness starts
# to rival the shortest lane horizon (60s) in magnitude. 20s is already a third of
# that horizon; deliberately stop the primary range there.
ORDERFLOW_SENSITIVITY_CANDIDATE_BOUNDS_MS = (5_000, 10_000, 15_000, 20_000, 30_000)
# 60000ms == the entire 1-minute horizon. A price reference this stale does not
# measure "return over the next minute" any more, it measures something closer to
# "return from whenever the last trade happened to land" — shown for reference
# only, never a candidate for the registered bound.
ORDERFLOW_SENSITIVITY_DIAGNOSTIC_BOUND_MS = 60_000


@dataclass(frozen=True)
class RawHorizonObservation:
    horizon_seconds: int
    close: float | None
    high: float | None
    endpoint_staleness_ms: int | None


@dataclass(frozen=True)
class RawSubjectObservation:
    pump_event_id: int
    event_base: str
    event_symbol: str
    observed_symbol: str
    role: str
    first_observed_at: datetime
    capture_expires_at: datetime
    records: int
    max_lag_ms: int | None
    anchor_close: float | None
    anchor_staleness_ms: int | None
    lead_metrics: tuple[LeadMetrics, ...]
    horizons: tuple[RawHorizonObservation, ...]


@dataclass(frozen=True)
class RawCaptureEpisode:
    pump_event_id: int
    event_base: str
    event_symbol: str
    first_observed_at: datetime
    capture_expires_at: datetime
    subjects: tuple[RawSubjectObservation, ...]


@dataclass(frozen=True)
class BoundResult:
    bound_ms: int
    is_diagnostic_only: bool
    complete_episodes: int
    clusters: int
    market_days: int
    exclusion_reasons: tuple[CountRow, ...]
    lanes: tuple[LaneRow, ...]


@dataclass(frozen=True)
class OrderflowSensitivityManifest:
    report_version: str
    capture_contract_version: str
    code_revision: str
    working_tree_dirty: bool
    generated_at: datetime
    dataset_since: datetime
    dataset_until_exclusive: datetime
    input_fingerprint: str
    root: str
    required_controls: int
    registered_v1_bound_ms: int
    candidate_bounds_ms: tuple[int, ...]
    diagnostic_only_bound_ms: int
    interpretation: str = "discovery_only_no_strategy_change"


@dataclass(frozen=True)
class OrderflowSensitivityReport:
    manifest: OrderflowSensitivityManifest
    files: int
    records: int
    capture_episodes: int
    bounds: tuple[BoundResult, ...]


def _count_rows(counter: Counter[str]) -> tuple[CountRow, ...]:
    return tuple(
        CountRow(name, count)
        for name, count in sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    )


def _raw_subject_from_file(
    path: Path,
    *,
    fingerprint_path: str,
    since_ms: int,
    until_ms: int,
    digest: Any,
) -> tuple[RawSubjectObservation | None, int]:
    """Mirrors orderflow_pilot_report._subject_from_file's parse loop exactly, but
    stops before applying any staleness bound — every anchor/horizon close, high,
    and staleness value is retained regardless of validity, so validity can be
    decided later for as many candidate bounds as needed from one parse."""
    identity: tuple[Any, ...] | None = None
    windows = {name: _WindowAccumulator() for name, _, _ in LEAD_WINDOWS}
    horizons = {seconds: _HorizonAccumulator() for seconds in POST_HORIZONS_SECONDS}
    anchor: dict[str, Any] | None = None
    last_bucket_start_ms: int | None = None
    records = 0
    max_record_lag_ms: int | None = None
    try:
        stream = gzip.open(path, mode="rt", encoding="utf-8")  # noqa: SIM115
    except OSError as exc:
        raise ValueError(f"{path}: cannot open gzip stream") from exc
    with stream:
        for line_number, line in enumerate(stream, start=1):
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON") from exc
            record = _validated_record(payload, path, line_number)
            first_observed_ms = record["first_observed_at_ms"]
            if first_observed_ms < since_ms or first_observed_ms >= until_ms:
                continue
            current_identity = (
                record["pump_event_id"],
                record["event_base"],
                record["event_symbol"],
                record["observed_symbol"],
                record["role"],
                first_observed_ms,
                record["capture_expires_at_ms"],
            )
            if identity is None:
                identity = current_identity
                expected_path = (
                    f"{datetime.fromtimestamp(first_observed_ms / 1_000, tz=UTC).date()}"
                    f"/event-{record['pump_event_id']}/"
                    f"{record['role']}-{record['observed_symbol']}.jsonl.gz"
                )
                if fingerprint_path != expected_path:
                    raise ValueError(f"{path}:{line_number}: capture path does not match identity")
            elif current_identity != identity:
                raise ValueError(f"{path}:{line_number}: file contains mixed capture identities")
            bucket = record["bucket"]
            start_ms = bucket["bucket_start_ms"]
            if last_bucket_start_ms is not None and start_ms <= last_bucket_start_ms:
                raise ValueError(f"{path}:{line_number}: buckets must be unique and ordered")
            last_bucket_start_ms = start_ms
            canonical = json.dumps(record, sort_keys=True, separators=(",", ":"))
            digest.update(fingerprint_path.encode())
            digest.update(b"\0")
            digest.update(canonical.encode())
            digest.update(b"\n")
            records += 1
            bucket_lag_ms = bucket["max_lag_ms"]
            max_record_lag_ms = (
                bucket_lag_ms
                if max_record_lag_ms is None
                else max(max_record_lag_ms, bucket_lag_ms)
            )

            relative_start_ms = start_ms - first_observed_ms
            for name, lower_seconds, upper_seconds in LEAD_WINDOWS:
                lower_ms = lower_seconds * 1_000
                upper_ms = upper_seconds * 1_000
                if relative_start_ms >= lower_ms and relative_start_ms + 1_000 <= upper_ms:
                    windows[name].add(bucket)
            if start_ms < first_observed_ms and (
                anchor is None or bucket["last_event_at_ms"] > anchor["last_event_at_ms"]
            ):
                anchor = bucket
            for seconds, accumulator in horizons.items():
                if (
                    first_observed_ms
                    < bucket["last_event_at_ms"]
                    <= first_observed_ms + seconds * 1_000
                ):
                    accumulator.add(bucket)
    if identity is None:
        return None, 0

    (
        pump_event_id,
        event_base,
        event_symbol,
        observed_symbol,
        role,
        first_observed_ms,
        capture_expires_ms,
    ) = identity
    anchor_close = anchor["close"] if anchor is not None else None
    anchor_staleness_ms = (
        first_observed_ms - anchor["last_event_at_ms"] if anchor is not None else None
    )
    horizon_observations = tuple(
        RawHorizonObservation(
            horizon_seconds=seconds,
            close=horizons[seconds].close,
            high=horizons[seconds].high,
            endpoint_staleness_ms=(
                first_observed_ms + seconds * 1_000 - horizons[seconds].last_event_ms
                if horizons[seconds].last_event_ms is not None
                else None
            ),
        )
        for seconds in POST_HORIZONS_SECONDS
    )
    lead_metrics = tuple(
        windows[name].finish(name, upper_seconds - lower_seconds, anchor_close)
        for name, lower_seconds, upper_seconds in LEAD_WINDOWS
    )
    return (
        RawSubjectObservation(
            pump_event_id=pump_event_id,
            event_base=event_base,
            event_symbol=event_symbol,
            observed_symbol=observed_symbol,
            role=role,
            first_observed_at=datetime.fromtimestamp(first_observed_ms / 1_000, tz=UTC),
            capture_expires_at=datetime.fromtimestamp(capture_expires_ms / 1_000, tz=UTC),
            records=records,
            max_lag_ms=max_record_lag_ms,
            anchor_close=anchor_close,
            anchor_staleness_ms=anchor_staleness_ms,
            lead_metrics=lead_metrics,
            horizons=horizon_observations,
        ),
        records,
    )


def load_raw_capture_episodes(
    root: Path,
    *,
    since: datetime,
    until: datetime,
) -> tuple[tuple[RawCaptureEpisode, ...], int, int, str]:
    if not root.is_dir():
        raise ValueError(f"order-flow root does not exist: {root}")
    digest = hashlib.sha256()
    grouped: dict[int, list[RawSubjectObservation]] = {}
    files = 0
    records = 0
    since_ms = int(since.timestamp() * 1_000)
    until_ms = int(until.timestamp() * 1_000)
    for path in sorted(root.glob("*/event-*/*.jsonl.gz")):
        subject, subject_records = _raw_subject_from_file(
            path,
            fingerprint_path=path.relative_to(root).as_posix(),
            since_ms=since_ms,
            until_ms=until_ms,
            digest=digest,
        )
        if subject is None:
            continue
        files += 1
        records += subject_records
        grouped.setdefault(subject.pump_event_id, []).append(subject)

    episodes: list[RawCaptureEpisode] = []
    for event_id, subjects in sorted(grouped.items()):
        identities = {
            (
                subject.event_base,
                subject.event_symbol,
                subject.first_observed_at,
                subject.capture_expires_at,
            )
            for subject in subjects
        }
        if len(identities) != 1:
            raise ValueError(f"event {event_id} contains mixed capture identities")
        symbols = [subject.observed_symbol for subject in subjects]
        if len(symbols) != len(set(symbols)):
            raise ValueError(f"event {event_id} contains duplicate observed symbols")
        identity = next(iter(identities))
        episodes.append(
            RawCaptureEpisode(
                pump_event_id=event_id,
                event_base=identity[0],
                event_symbol=identity[1],
                first_observed_at=identity[2],
                capture_expires_at=identity[3],
                subjects=tuple(sorted(subjects, key=lambda row: (row.role, row.observed_symbol))),
            )
        )
    return tuple(episodes), files, records, digest.hexdigest()


def _resolve_horizon(
    observation: RawHorizonObservation,
    *,
    anchor_close: float | None,
    anchor_valid: bool,
    bound_ms: int,
) -> HorizonMetrics:
    staleness = observation.endpoint_staleness_ms
    close = observation.close
    high = observation.high
    return_pct: float | None = None
    max_up_pct: float | None = None
    if (
        anchor_valid
        and anchor_close is not None
        and staleness is not None
        and 0 <= staleness <= bound_ms
        and close is not None
        and high is not None
    ):
        return_pct = (close / anchor_close - 1) * 100
        max_up_pct = (high / anchor_close - 1) * 100
    return HorizonMetrics(
        horizon_seconds=observation.horizon_seconds,
        return_pct=return_pct,
        max_up_pct=max_up_pct,
        endpoint_staleness_ms=staleness,
    )


def _resolve_subject(raw: RawSubjectObservation, bound_ms: int) -> SubjectObservation:
    anchor_valid = raw.anchor_staleness_ms is not None and 0 <= raw.anchor_staleness_ms <= bound_ms
    return SubjectObservation(
        pump_event_id=raw.pump_event_id,
        event_base=raw.event_base,
        event_symbol=raw.event_symbol,
        observed_symbol=raw.observed_symbol,
        role=raw.role,
        first_observed_at=raw.first_observed_at,
        capture_expires_at=raw.capture_expires_at,
        records=raw.records,
        max_lag_ms=raw.max_lag_ms,
        anchor_staleness_ms=raw.anchor_staleness_ms,
        lead_metrics=raw.lead_metrics,
        horizons=tuple(
            _resolve_horizon(
                observation,
                anchor_close=raw.anchor_close,
                anchor_valid=anchor_valid,
                bound_ms=bound_ms,
            )
            for observation in raw.horizons
        ),
    )


def _resolve_episode(raw: RawCaptureEpisode, bound_ms: int) -> CaptureEpisode:
    return CaptureEpisode(
        pump_event_id=raw.pump_event_id,
        event_base=raw.event_base,
        event_symbol=raw.event_symbol,
        first_observed_at=raw.first_observed_at,
        capture_expires_at=raw.capture_expires_at,
        subjects=tuple(_resolve_subject(subject, bound_ms) for subject in raw.subjects),
    )


def _complete_episode_at_bound(
    episode: CaptureEpisode,
    until: datetime,
    bound_ms: int,
) -> tuple[bool, str | None]:
    """Same completeness rule as orderflow_pilot_report._complete_episode, with the
    anchor check parameterized on the candidate bound instead of v1's fixed
    constant. The horizon check needs no parameter here: _resolve_horizon already
    applied `bound_ms` when it decided return_pct, so "return_pct is None" already
    means "invalid at this bound." """
    event, controls = _episode_parts(episode)
    if episode.capture_expires_at > until:
        return False, "right_censored"
    if event is None:
        return False, "missing_or_duplicate_event_subject"
    if len(controls) < ORDERFLOW_REQUIRED_CONTROLS:
        return False, "insufficient_controls"
    for subject in [event, *controls]:
        if subject.anchor_staleness_ms is None or subject.anchor_staleness_ms > bound_ms:
            return False, "stale_or_missing_anchor"
        if any(row.active_buckets == 0 for row in subject.lead_metrics):
            return False, "incomplete_pre_windows"
        if any(row.return_pct is None for row in subject.horizons):
            return False, "incomplete_post_horizons"
    return True, None


def _bound_result(
    raw_episodes: tuple[RawCaptureEpisode, ...],
    *,
    bound_ms: int,
    until: datetime,
) -> BoundResult:
    exclusions: Counter[str] = Counter()
    complete: list[CaptureEpisode] = []
    for raw in raw_episodes:
        episode = _resolve_episode(raw, bound_ms)
        ok, reason = _complete_episode_at_bound(episode, until, bound_ms)
        if ok:
            complete.append(episode)
        elif reason is not None:
            exclusions[reason] += 1
    return BoundResult(
        bound_ms=bound_ms,
        is_diagnostic_only=bound_ms == ORDERFLOW_SENSITIVITY_DIAGNOSTIC_BOUND_MS,
        complete_episodes=len(complete),
        clusters=len({episode.event_base for episode in complete}),
        market_days=len({episode.first_observed_at.date() for episode in complete}),
        exclusion_reasons=_count_rows(exclusions),
        lanes=_lane_rows(complete),
    )


def build_sensitivity_report(
    raw_episodes: tuple[RawCaptureEpisode, ...],
    *,
    files: int,
    records: int,
    input_fingerprint: str,
    root: Path,
    since: datetime,
    until: datetime,
    generated_at: datetime,
    code_revision: str,
    working_tree_dirty: bool,
) -> OrderflowSensitivityReport:
    bounds_ms = (
        *ORDERFLOW_SENSITIVITY_CANDIDATE_BOUNDS_MS,
        ORDERFLOW_SENSITIVITY_DIAGNOSTIC_BOUND_MS,
    )
    return OrderflowSensitivityReport(
        manifest=OrderflowSensitivityManifest(
            report_version=ORDERFLOW_SENSITIVITY_REPORT_VERSION,
            capture_contract_version=ORDERFLOW_CONTRACT_VERSION,
            code_revision=normalize_code_revision(code_revision),
            working_tree_dirty=working_tree_dirty,
            generated_at=generated_at,
            dataset_since=since,
            dataset_until_exclusive=until,
            input_fingerprint=input_fingerprint,
            root=str(root),
            required_controls=ORDERFLOW_REQUIRED_CONTROLS,
            registered_v1_bound_ms=ORDERFLOW_MAX_ENDPOINT_STALENESS_MS,
            candidate_bounds_ms=ORDERFLOW_SENSITIVITY_CANDIDATE_BOUNDS_MS,
            diagnostic_only_bound_ms=ORDERFLOW_SENSITIVITY_DIAGNOSTIC_BOUND_MS,
        ),
        files=files,
        records=records,
        capture_episodes=len(raw_episodes),
        bounds=tuple(
            _bound_result(raw_episodes, bound_ms=bound_ms, until=until) for bound_ms in bounds_ms
        ),
    )


def render_json(report: OrderflowSensitivityReport) -> str:
    return json.dumps(json_ready(asdict(report)), sort_keys=True, indent=2) + "\n"


def _bound_label(bound: BoundResult) -> str:
    seconds = bound.bound_ms // 1_000
    return f"{seconds}s (diagnostic only)" if bound.is_diagnostic_only else f"{seconds}s"


def _lane_cell(bound: BoundResult, lane: str, feature: str, horizon_seconds: int) -> str:
    row = next(
        (
            item
            for item in bound.lanes
            if item.lane == lane
            and item.feature == feature
            and item.horizon_seconds == horizon_seconds
        ),
        None,
    )
    if row is None or row.matched_episodes == 0:
        return "n/a"
    lift = "n/a" if row.median_return_lift_pct is None else f"{row.median_return_lift_pct:+.2f}%"
    corr = "n/a" if row.rank_correlation is None else f"{row.rank_correlation:.2f}"
    return f"N={row.matched_episodes}, lift={lift}, corr={corr}"


def render_markdown(report: OrderflowSensitivityReport) -> str:
    manifest = report.manifest
    lines = [
        "# Bybit Order-Flow Endpoint Staleness Sensitivity",
        "",
        f"Generated: {manifest.generated_at.isoformat()}",
        f"Code revision: `{manifest.code_revision}`",
        f"Working tree dirty: {'yes' if manifest.working_tree_dirty else 'no'}",
        f"Capture contract: `{manifest.capture_contract_version}`",
        f"Input fingerprint: `{manifest.input_fingerprint}`",
        (
            f"Scope: {manifest.dataset_since.isoformat()} <= first observed < "
            f"{manifest.dataset_until_exclusive.isoformat()}"
        ),
        "",
        (
            "> Discovery-only diagnostic. Re-parses the same raw captures as the "
            "registered `v1` pilot report and never modifies its contract. Cannot "
            "change production strategy or authorize trading."
        ),
        "",
        (
            f"Registered `v1` bound: {manifest.registered_v1_bound_ms}ms. Candidate "
            f"range: {', '.join(f'{ms // 1_000}s' for ms in manifest.candidate_bounds_ms)}. "
            f"{manifest.diagnostic_only_bound_ms // 1_000}s is shown only as an explicitly "
            "unusable diagnostic bound for the 1-minute lane, never a candidate."
        ),
        "",
        "## Readiness by bound",
        "",
    ]
    lines.extend(
        markdown_table(
            ("Bound", "Complete episodes", "Clusters", "UTC market days"),
            [
                (
                    _bound_label(bound),
                    bound.complete_episodes,
                    bound.clusters,
                    bound.market_days,
                )
                for bound in report.bounds
            ],
        )
    )
    lines.extend(
        [
            "",
            "Required before interpreting any lane: 100 complete matched episodes, "
            "30 asset clusters, 7 UTC market days (same thresholds as `v1`).",
            "",
            "## Lane robustness across bounds",
            "",
        ]
    )
    lane_combos: list[tuple[str, str, int]] = []
    seen: set[tuple[str, str, int]] = set()
    for bound in report.bounds:
        for row in bound.lanes:
            key = (row.lane, row.feature, row.horizon_seconds)
            if key not in seen:
                seen.add(key)
                lane_combos.append(key)
    header = ("Lane", "Feature", "Horizon", *(_bound_label(bound) for bound in report.bounds))
    rows = [
        (
            lane,
            feature,
            f"{horizon_seconds // 60}m",
            *(_lane_cell(bound, lane, feature, horizon_seconds) for bound in report.bounds),
        )
        for lane, feature, horizon_seconds in lane_combos
    ]
    lines.extend(markdown_table(header, rows))
    lines.extend(
        [
            "",
            (
                "Read direction (sign of the lift) and rank correlation across the "
                "5-30s range together, not any single column. A lane whose sign or "
                "correlation is unstable within that range, or that only turns "
                "positive at 60s, is not evidence of a pre-trigger effect — treat it "
                "as noise or a liquidity/sample-selection artifact, not a discovery."
            ),
            "",
            "## Exclusion reasons by bound",
            "",
        ]
    )
    reason_names = sorted({row.name for bound in report.bounds for row in bound.exclusion_reasons})
    exclusion_header = ("Reason", *(_bound_label(bound) for bound in report.bounds))
    exclusion_rows = [
        (
            reason,
            *(
                next((row.count for row in bound.exclusion_reasons if row.name == reason), 0)
                for bound in report.bounds
            ),
        )
        for reason in reason_names
    ]
    lines.extend(markdown_table(exclusion_header, exclusion_rows))
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(os.getenv("ORDERFLOW_STORAGE_ROOT", "/data/orderflow")),
    )
    parser.add_argument("--since", type=parse_utc_datetime, default=ORDERFLOW_COHORT_START)
    parser.add_argument(
        "--until",
        type=parse_utc_datetime,
        help="exclusive UTC cutoff; defaults to the run start",
    )
    parser.add_argument("--code-revision", default=os.getenv("SCHURFER_GIT_SHA"))
    parser.add_argument(
        "--working-tree-dirty",
        action=argparse.BooleanOptionalAction,
        required=True,
    )
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    return parser


def _run(args: argparse.Namespace) -> str:
    generated_at = datetime.now(UTC)
    until = args.until or generated_at
    if args.since < ORDERFLOW_COHORT_START:
        raise ValueError(f"order-flow pilot cohort starts at {ORDERFLOW_COHORT_START.isoformat()}")
    if until <= args.since:
        raise ValueError("since must be earlier than until")
    if not args.code_revision:
        raise ValueError("--code-revision or SCHURFER_GIT_SHA is required")
    raw_episodes, files, records, fingerprint = load_raw_capture_episodes(
        args.root,
        since=args.since,
        until=until,
    )
    report = build_sensitivity_report(
        raw_episodes,
        files=files,
        records=records,
        input_fingerprint=fingerprint,
        root=args.root,
        since=args.since,
        until=until,
        generated_at=generated_at,
        code_revision=args.code_revision,
        working_tree_dirty=args.working_tree_dirty,
    )
    return render_json(report) if args.format == "json" else render_markdown(report)


def main() -> None:
    try:
        sys.stdout.write(_run(build_parser().parse_args()))
    except (OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc
