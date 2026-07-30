"""Point-in-time diagnostics for the bounded Bybit public-trades pilot."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import os
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from statistics import fmean, median
from typing import Any

from .reporting import (
    format_number,
    format_percentage,
    json_ready,
    markdown_table,
    normalize_code_revision,
    parse_utc_datetime,
)

ORDERFLOW_CONTRACT_VERSION = "bybit_orderflow_pilot_v1"
ORDERFLOW_REPORT_VERSION = "bybit_orderflow_pilot_report_v1"
ORDERFLOW_COHORT_START = datetime(2026, 7, 30, 18, 15, tzinfo=UTC)
ORDERFLOW_PREBUFFER_SECONDS = 1_800
ORDERFLOW_CAPTURE_AFTER_SECONDS = 3_600
ORDERFLOW_REQUIRED_CONTROLS = 3
ORDERFLOW_MIN_COMPLETE_EPISODES = 100
ORDERFLOW_MIN_CLUSTERS = 30
ORDERFLOW_MIN_MARKET_DAYS = 7
ORDERFLOW_MAX_ENDPOINT_STALENESS_MS = 5_000

LEAD_WINDOWS = (
    ("30m_to_15m", -1_800, -900),
    ("15m_to_5m", -900, -300),
    ("5m_to_1m", -300, -60),
    ("1m_to_trigger", -60, 0),
)
POST_HORIZONS_SECONDS = (60, 300, 900, 3_600)


@dataclass(frozen=True)
class LeadMetrics:
    window: str
    active_buckets: int
    total_notional_usd: float
    notional_per_second_usd: float
    notional_imbalance: float | None
    trade_imbalance: float | None
    price_return_pct: float | None
    return_to_trigger_pct: float | None
    max_lag_ms: int | None


@dataclass(frozen=True)
class HorizonMetrics:
    horizon_seconds: int
    return_pct: float | None
    max_up_pct: float | None
    endpoint_staleness_ms: int | None


@dataclass(frozen=True)
class SubjectObservation:
    pump_event_id: int
    event_base: str
    event_symbol: str
    observed_symbol: str
    role: str
    first_observed_at: datetime
    capture_expires_at: datetime
    records: int
    max_lag_ms: int | None
    anchor_staleness_ms: int | None
    lead_metrics: tuple[LeadMetrics, ...]
    horizons: tuple[HorizonMetrics, ...]


@dataclass(frozen=True)
class CaptureEpisode:
    pump_event_id: int
    event_base: str
    event_symbol: str
    first_observed_at: datetime
    capture_expires_at: datetime
    subjects: tuple[SubjectObservation, ...]


@dataclass(frozen=True)
class CountRow:
    name: str
    count: int


@dataclass(frozen=True)
class CaptureQualityRow:
    role: str
    subjects: int
    records: int
    median_records: float | None
    median_anchor_staleness_ms: float | None
    p95_max_lag_ms: float | None


@dataclass(frozen=True)
class LeadFeatureRow:
    window: str
    matched_episodes: int
    median_event_imbalance: float | None
    median_control_imbalance: float | None
    median_imbalance_lift: float | None
    median_event_notional_per_second: float | None
    median_control_notional_per_second: float | None
    median_event_price_return_pct: float | None
    median_event_return_to_trigger_pct: float | None


@dataclass(frozen=True)
class LaneRow:
    lane: str
    feature: str
    horizon_seconds: int
    matched_episodes: int
    clusters: int
    largest_cluster_share_pct: float | None
    median_feature: float | None
    median_event_return_pct: float | None
    median_return_lift_pct: float | None
    rank_correlation: float | None
    weakest_asset_exclusion_correlation: float | None
    weakest_day_exclusion_correlation: float | None
    positive_feature_price_up_pct: float | None


@dataclass(frozen=True)
class OrderflowManifest:
    report_version: str
    capture_contract_version: str
    code_revision: str
    working_tree_dirty: bool
    generated_at: datetime
    dataset_since: datetime
    dataset_until_exclusive: datetime
    input_fingerprint: str
    root: str
    prebuffer_seconds: int
    capture_after_seconds: int
    required_controls: int
    lead_windows: tuple[tuple[str, int, int], ...]
    post_horizons_seconds: tuple[int, ...]
    interpretation: str = "discovery_only_no_strategy_change"


@dataclass(frozen=True)
class OrderflowPilotReport:
    manifest: OrderflowManifest
    files: int
    records: int
    capture_episodes: int
    complete_matched_episodes: int
    clusters: int
    market_days: int
    readiness: str
    readiness_requirements: tuple[str, ...]
    exclusion_reasons: tuple[CountRow, ...]
    capture_quality: tuple[CaptureQualityRow, ...]
    lead_features: tuple[LeadFeatureRow, ...]
    lanes: tuple[LaneRow, ...]


@dataclass
class _WindowAccumulator:
    active_buckets: int = 0
    buy_notional: float = 0
    sell_notional: float = 0
    buy_trades: int = 0
    sell_trades: int = 0
    first_start_ms: int | None = None
    first_open: float | None = None
    last_start_ms: int | None = None
    last_close: float | None = None
    max_lag_ms: int | None = None

    def add(self, bucket: dict[str, Any]) -> None:
        start_ms = bucket["bucket_start_ms"]
        self.active_buckets += 1
        self.buy_notional += bucket["buy_notional"]
        self.sell_notional += bucket["sell_notional"]
        self.buy_trades += bucket["buy_trades"]
        self.sell_trades += bucket["sell_trades"]
        if self.first_start_ms is None or start_ms < self.first_start_ms:
            self.first_start_ms = start_ms
            self.first_open = bucket["open"]
        if self.last_start_ms is None or start_ms > self.last_start_ms:
            self.last_start_ms = start_ms
            self.last_close = bucket["close"]
        lag = bucket["max_lag_ms"]
        self.max_lag_ms = lag if self.max_lag_ms is None else max(self.max_lag_ms, lag)

    def finish(
        self,
        name: str,
        duration_seconds: int,
        trigger_anchor_close: float | None,
    ) -> LeadMetrics:
        total_notional = self.buy_notional + self.sell_notional
        total_trades = self.buy_trades + self.sell_trades
        price_return = None
        if self.first_open is not None and self.last_close is not None:
            price_return = (self.last_close / self.first_open - 1) * 100
        return LeadMetrics(
            window=name,
            active_buckets=self.active_buckets,
            total_notional_usd=total_notional,
            notional_per_second_usd=total_notional / duration_seconds,
            notional_imbalance=(
                (self.buy_notional - self.sell_notional) / total_notional
                if total_notional > 0
                else None
            ),
            trade_imbalance=(
                (self.buy_trades - self.sell_trades) / total_trades if total_trades > 0 else None
            ),
            price_return_pct=price_return,
            return_to_trigger_pct=(
                (trigger_anchor_close / self.last_close - 1) * 100
                if trigger_anchor_close is not None and self.last_close is not None
                else None
            ),
            max_lag_ms=self.max_lag_ms,
        )


@dataclass
class _HorizonAccumulator:
    last_event_ms: int | None = None
    close: float | None = None
    high: float | None = None

    def add(self, bucket: dict[str, Any]) -> None:
        last_event_ms = bucket["last_event_at_ms"]
        if self.last_event_ms is None or last_event_ms > self.last_event_ms:
            self.last_event_ms = last_event_ms
            self.close = bucket["close"]
        high = bucket["high"]
        self.high = high if self.high is None else max(self.high, high)


def _finite_number(value: Any, field: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{field} must be numeric")
    normalized = float(value)
    if not math.isfinite(normalized) or (positive and normalized <= 0):
        qualifier = "finite and positive" if positive else "finite"
        raise ValueError(f"{field} must be {qualifier}")
    return normalized


def _integer(value: Any, field: str, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer")
    if (positive and value <= 0) or (not positive and value < 0):
        qualifier = "positive" if positive else "non-negative"
        raise ValueError(f"{field} must be {qualifier}")
    return value


def _validated_record(payload: Any, source: Path, line_number: int) -> dict[str, Any]:
    prefix = f"{source}:{line_number}"
    if not isinstance(payload, dict):
        raise ValueError(f"{prefix}: record must be an object")
    if payload.get("contract_version") != ORDERFLOW_CONTRACT_VERSION:
        raise ValueError(f"{prefix}: unsupported order-flow contract")
    pump_event_id = _integer(payload.get("pump_event_id"), "pump_event_id", positive=True)
    event_base = payload.get("event_base")
    event_symbol = payload.get("event_symbol")
    observed_symbol = payload.get("observed_symbol")
    role = payload.get("role")
    if not all(
        isinstance(value, str) and value.strip()
        for value in (
            event_base,
            event_symbol,
            observed_symbol,
        )
    ):
        raise ValueError(f"{prefix}: record identity is incomplete")
    if role not in {"event", "control"}:
        raise ValueError(f"{prefix}: role must be event or control")
    if (role == "event") != (observed_symbol == event_symbol):
        raise ValueError(f"{prefix}: event role and observed symbol are inconsistent")
    first_observed_ms = _integer(
        payload.get("first_observed_at_ms"),
        "first_observed_at_ms",
        positive=True,
    )
    capture_expires_ms = _integer(
        payload.get("capture_expires_at_ms"),
        "capture_expires_at_ms",
        positive=True,
    )
    if capture_expires_ms - first_observed_ms != ORDERFLOW_CAPTURE_AFTER_SECONDS * 1_000:
        raise ValueError(f"{prefix}: capture duration does not match the registered contract")
    bucket = payload.get("bucket")
    if not isinstance(bucket, dict):
        raise ValueError(f"{prefix}: bucket must be an object")
    if (
        bucket.get("schema_version") != 1
        or bucket.get("exchange") != "bybit"
        or bucket.get("symbol") != observed_symbol
    ):
        raise ValueError(f"{prefix}: bucket identity does not match the record")
    start_ms = _integer(bucket.get("bucket_start_ms"), "bucket_start_ms", positive=True)
    first_event_ms = _integer(bucket.get("first_event_at_ms"), "first_event_at_ms", positive=True)
    last_event_ms = _integer(bucket.get("last_event_at_ms"), "last_event_at_ms", positive=True)
    _integer(
        bucket.get("last_received_at_ms"),
        "last_received_at_ms",
        positive=True,
    )
    if not (start_ms <= first_event_ms <= last_event_ms < start_ms + 1_000):
        raise ValueError(f"{prefix}: event timestamps escape the one-second bucket")
    activation_bucket_ms = first_observed_ms // 1_000 * 1_000
    first_future_bucket_ms = activation_bucket_ms + 1_000
    cutoff_ms = first_observed_ms - ORDERFLOW_PREBUFFER_SECONDS * 1_000
    if start_ms == activation_bucket_ms:
        raise ValueError(f"{prefix}: activation boundary bucket must be excluded")
    if not (
        cutoff_ms <= start_ms < first_observed_ms
        or first_future_bucket_ms <= start_ms < capture_expires_ms
    ):
        raise ValueError(f"{prefix}: bucket is outside the registered capture window")
    for field in ("open", "high", "low", "close"):
        bucket[field] = _finite_number(bucket.get(field), field, positive=True)
    if bucket["high"] < max(bucket["open"], bucket["close"]):
        raise ValueError(f"{prefix}: bucket high is inconsistent")
    if bucket["low"] > min(bucket["open"], bucket["close"]):
        raise ValueError(f"{prefix}: bucket low is inconsistent")
    for field in ("buy_notional", "sell_notional", "buy_quantity", "sell_quantity"):
        bucket[field] = _finite_number(bucket.get(field), field)
        if bucket[field] < 0:
            raise ValueError(f"{prefix}: {field} must be non-negative")
    for field in ("buy_trades", "sell_trades", "max_lag_ms"):
        bucket[field] = _integer(bucket.get(field), field)
    if bucket["buy_trades"] + bucket["sell_trades"] <= 0:
        raise ValueError(f"{prefix}: non-empty bucket must contain a trade")
    payload["pump_event_id"] = pump_event_id
    payload["first_observed_at_ms"] = first_observed_ms
    payload["capture_expires_at_ms"] = capture_expires_ms
    return payload


def _subject_from_file(
    path: Path,
    *,
    fingerprint_path: str,
    since_ms: int,
    until_ms: int,
    digest: Any,
) -> tuple[SubjectObservation | None, int]:
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
    anchor_staleness_ms = (
        first_observed_ms - anchor["last_event_at_ms"] if anchor is not None else None
    )
    anchor_valid = (
        anchor is not None
        and anchor_staleness_ms is not None
        and 0 <= anchor_staleness_ms <= ORDERFLOW_MAX_ENDPOINT_STALENESS_MS
    )
    horizon_metrics: list[HorizonMetrics] = []
    for seconds in POST_HORIZONS_SECONDS:
        accumulator = horizons[seconds]
        target_ms = first_observed_ms + seconds * 1_000
        staleness = (
            target_ms - accumulator.last_event_ms if accumulator.last_event_ms is not None else None
        )
        endpoint_valid = (
            anchor_valid
            and staleness is not None
            and 0 <= staleness <= ORDERFLOW_MAX_ENDPOINT_STALENESS_MS
            and accumulator.close is not None
            and accumulator.high is not None
        )
        anchor_close = anchor["close"] if anchor_valid and anchor is not None else None
        horizon_metrics.append(
            HorizonMetrics(
                horizon_seconds=seconds,
                return_pct=(
                    (accumulator.close / anchor_close - 1) * 100
                    if endpoint_valid and anchor_close is not None
                    else None
                ),
                max_up_pct=(
                    (accumulator.high / anchor_close - 1) * 100
                    if endpoint_valid and anchor_close is not None
                    else None
                ),
                endpoint_staleness_ms=staleness,
            )
        )
    return (
        SubjectObservation(
            pump_event_id=pump_event_id,
            event_base=event_base,
            event_symbol=event_symbol,
            observed_symbol=observed_symbol,
            role=role,
            first_observed_at=datetime.fromtimestamp(first_observed_ms / 1_000, tz=UTC),
            capture_expires_at=datetime.fromtimestamp(capture_expires_ms / 1_000, tz=UTC),
            records=records,
            max_lag_ms=max_record_lag_ms,
            anchor_staleness_ms=anchor_staleness_ms,
            lead_metrics=tuple(
                windows[name].finish(
                    name,
                    upper_seconds - lower_seconds,
                    anchor["close"] if anchor_valid and anchor is not None else None,
                )
                for name, lower_seconds, upper_seconds in LEAD_WINDOWS
            ),
            horizons=tuple(horizon_metrics),
        ),
        records,
    )


def load_capture_episodes(
    root: Path,
    *,
    since: datetime,
    until: datetime,
) -> tuple[tuple[CaptureEpisode, ...], int, int, str]:
    if not root.is_dir():
        raise ValueError(f"order-flow root does not exist: {root}")
    digest = hashlib.sha256()
    grouped: dict[int, list[SubjectObservation]] = {}
    files = 0
    records = 0
    since_ms = int(since.timestamp() * 1_000)
    until_ms = int(until.timestamp() * 1_000)
    for path in sorted(root.glob("*/event-*/*.jsonl.gz")):
        subject, subject_records = _subject_from_file(
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

    episodes: list[CaptureEpisode] = []
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
            CaptureEpisode(
                pump_event_id=event_id,
                event_base=identity[0],
                event_symbol=identity[1],
                first_observed_at=identity[2],
                capture_expires_at=identity[3],
                subjects=tuple(sorted(subjects, key=lambda row: (row.role, row.observed_symbol))),
            )
        )
    return tuple(episodes), files, records, digest.hexdigest()


def _metric(subject: SubjectObservation, window: str) -> LeadMetrics:
    return next(row for row in subject.lead_metrics if row.window == window)


def _horizon(subject: SubjectObservation, seconds: int) -> HorizonMetrics:
    return next(row for row in subject.horizons if row.horizon_seconds == seconds)


def _mean(values: list[float]) -> float | None:
    return fmean(values) if values else None


def _median(values: list[float]) -> float | None:
    return median(values) if values else None


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=values.__getitem__)
    ranks = [0.0] * len(values)
    index = 0
    while index < len(order):
        end = index + 1
        while end < len(order) and values[order[end]] == values[order[index]]:
            end += 1
        rank = (index + end - 1) / 2 + 1
        for ordered_index in order[index:end]:
            ranks[ordered_index] = rank
        index = end
    return ranks


def _rank_correlation(left: list[float], right: list[float]) -> float | None:
    if len(left) < 3 or len(left) != len(right):
        return None
    left_ranks = _ranks(left)
    right_ranks = _ranks(right)
    left_mean = fmean(left_ranks)
    right_mean = fmean(right_ranks)
    numerator = sum(
        (left_value - left_mean) * (right_value - right_mean)
        for left_value, right_value in zip(left_ranks, right_ranks, strict=True)
    )
    left_scale = sum((value - left_mean) ** 2 for value in left_ranks)
    right_scale = sum((value - right_mean) ** 2 for value in right_ranks)
    denominator = math.sqrt(left_scale * right_scale)
    return numerator / denominator if denominator > 0 else None


def _weakest_leave_one_group_out_correlation(
    features: list[float],
    returns: list[float],
    groups: list[str],
) -> float | None:
    correlations: list[float] = []
    for excluded in set(groups):
        selected = [index for index, group in enumerate(groups) if group != excluded]
        correlation = _rank_correlation(
            [features[index] for index in selected],
            [returns[index] for index in selected],
        )
        if correlation is not None:
            correlations.append(correlation)
    return min(correlations) if correlations else None


def _episode_parts(
    episode: CaptureEpisode,
) -> tuple[SubjectObservation | None, list[SubjectObservation]]:
    events = [subject for subject in episode.subjects if subject.role == "event"]
    controls = [subject for subject in episode.subjects if subject.role == "control"]
    return (events[0] if len(events) == 1 else None), controls


def _complete_episode(episode: CaptureEpisode, until: datetime) -> tuple[bool, str | None]:
    event, controls = _episode_parts(episode)
    if episode.capture_expires_at > until:
        return False, "right_censored"
    if event is None:
        return False, "missing_or_duplicate_event_subject"
    if len(controls) < ORDERFLOW_REQUIRED_CONTROLS:
        return False, "insufficient_controls"
    for subject in [event, *controls]:
        if subject.anchor_staleness_ms is None or (
            subject.anchor_staleness_ms > ORDERFLOW_MAX_ENDPOINT_STALENESS_MS
        ):
            return False, "stale_or_missing_anchor"
        if any(row.active_buckets == 0 for row in subject.lead_metrics):
            return False, "incomplete_pre_windows"
        if any(row.return_pct is None for row in subject.horizons):
            return False, "incomplete_post_horizons"
    return True, None


def _quality_rows(episodes: tuple[CaptureEpisode, ...]) -> tuple[CaptureQualityRow, ...]:
    rows: list[CaptureQualityRow] = []
    subjects = [subject for episode in episodes for subject in episode.subjects]
    for role in ("event", "control"):
        selected = [subject for subject in subjects if subject.role == role]
        anchor_staleness = [
            float(subject.anchor_staleness_ms)
            for subject in selected
            if subject.anchor_staleness_ms is not None
        ]
        max_lags = [
            float(subject.max_lag_ms) for subject in selected if subject.max_lag_ms is not None
        ]
        rows.append(
            CaptureQualityRow(
                role=role,
                subjects=len(selected),
                records=sum(subject.records for subject in selected),
                median_records=_median([float(subject.records) for subject in selected]),
                median_anchor_staleness_ms=_median(anchor_staleness),
                p95_max_lag_ms=_percentile(max_lags, 0.95),
            )
        )
    return tuple(rows)


def _lead_feature_rows(episodes: list[CaptureEpisode]) -> tuple[LeadFeatureRow, ...]:
    rows: list[LeadFeatureRow] = []
    for window, _, _ in LEAD_WINDOWS:
        event_imbalances: list[float] = []
        control_imbalances: list[float] = []
        lifts: list[float] = []
        event_notionals: list[float] = []
        control_notionals: list[float] = []
        event_returns: list[float] = []
        event_returns_to_trigger: list[float] = []
        for episode in episodes:
            event, controls = _episode_parts(episode)
            if event is None:
                continue
            event_metric = _metric(event, window)
            control_metrics = [_metric(control, window) for control in controls]
            control_values = [
                row.notional_imbalance
                for row in control_metrics
                if row.notional_imbalance is not None
            ]
            if event_metric.notional_imbalance is None or not control_values:
                continue
            control_imbalance = median(control_values)
            event_imbalances.append(event_metric.notional_imbalance)
            control_imbalances.append(control_imbalance)
            lifts.append(event_metric.notional_imbalance - control_imbalance)
            event_notionals.append(event_metric.notional_per_second_usd)
            control_notionals.append(median(row.notional_per_second_usd for row in control_metrics))
            if event_metric.price_return_pct is not None:
                event_returns.append(event_metric.price_return_pct)
            if event_metric.return_to_trigger_pct is not None:
                event_returns_to_trigger.append(event_metric.return_to_trigger_pct)
        rows.append(
            LeadFeatureRow(
                window=window,
                matched_episodes=len(lifts),
                median_event_imbalance=_median(event_imbalances),
                median_control_imbalance=_median(control_imbalances),
                median_imbalance_lift=_median(lifts),
                median_event_notional_per_second=_median(event_notionals),
                median_control_notional_per_second=_median(control_notionals),
                median_event_price_return_pct=_median(event_returns),
                median_event_return_to_trigger_pct=_median(event_returns_to_trigger),
            )
        )
    return tuple(rows)


def _lane_row(
    episodes: list[CaptureEpisode],
    *,
    lane: str,
    feature_name: str,
    horizon_seconds: int,
    feature_for: Any,
    short_return: bool,
    return_for: Any | None = None,
) -> LaneRow:
    features: list[float] = []
    event_returns: list[float] = []
    return_lifts: list[float] = []
    adverse_flags: list[float] = []
    bases: list[str] = []
    market_days: list[str] = []
    for episode in episodes:
        event, controls = _episode_parts(episode)
        if event is None:
            continue
        feature = feature_for(event, controls)
        event_return = (
            return_for(event, horizon_seconds)
            if return_for is not None
            else _horizon(event, horizon_seconds).return_pct
        )
        control_returns = [
            value
            for control in controls
            if (
                value := (
                    return_for(control, horizon_seconds)
                    if return_for is not None
                    else _horizon(control, horizon_seconds).return_pct
                )
            )
            is not None
        ]
        if feature is None or event_return is None or not control_returns:
            continue
        normalized_return = -event_return if short_return else event_return
        normalized_controls = [-value if short_return else value for value in control_returns]
        features.append(feature)
        event_returns.append(normalized_return)
        return_lifts.append(normalized_return - median(normalized_controls))
        bases.append(episode.event_base)
        market_days.append(episode.first_observed_at.date().isoformat())
        if feature > 0:
            adverse_flags.append(1.0 if event_return > 0 else 0.0)
    adverse_mean = _mean(adverse_flags)
    cluster_counts = Counter(bases)
    return LaneRow(
        lane=lane,
        feature=feature_name,
        horizon_seconds=horizon_seconds,
        matched_episodes=len(features),
        clusters=len(cluster_counts),
        largest_cluster_share_pct=(
            max(cluster_counts.values()) / len(features) * 100 if features else None
        ),
        median_feature=_median(features),
        median_event_return_pct=_median(event_returns),
        median_return_lift_pct=_median(return_lifts),
        rank_correlation=_rank_correlation(features, return_lifts),
        weakest_asset_exclusion_correlation=_weakest_leave_one_group_out_correlation(
            features,
            return_lifts,
            bases,
        ),
        weakest_day_exclusion_correlation=_weakest_leave_one_group_out_correlation(
            features,
            return_lifts,
            market_days,
        ),
        positive_feature_price_up_pct=(adverse_mean * 100 if adverse_mean is not None else None),
    )


def _imbalance_lift(
    window: str,
    event: SubjectObservation,
    controls: list[SubjectObservation],
) -> float | None:
    event_value = _metric(event, window).notional_imbalance
    control_values = [
        value
        for control in controls
        if (value := _metric(control, window).notional_imbalance) is not None
    ]
    if event_value is None or not control_values:
        return None
    return event_value - median(control_values)


def _exhaustion_lift(
    event: SubjectObservation,
    controls: list[SubjectObservation],
) -> float | None:
    event_early = _metric(event, "5m_to_1m").notional_imbalance
    event_late = _metric(event, "1m_to_trigger").notional_imbalance
    control_values: list[float] = []
    for control in controls:
        early = _metric(control, "5m_to_1m").notional_imbalance
        late = _metric(control, "1m_to_trigger").notional_imbalance
        if early is not None and late is not None:
            control_values.append(early - late)
    if event_early is None or event_late is None or not control_values:
        return None
    return (event_early - event_late) - median(control_values)


def _return_from_window_end(
    subject: SubjectObservation,
    window: str,
    horizon_seconds: int,
) -> float | None:
    to_trigger = _metric(subject, window).return_to_trigger_pct
    post_trigger = _horizon(subject, horizon_seconds).return_pct
    if to_trigger is None or post_trigger is None:
        return None
    return ((1 + to_trigger / 100) * (1 + post_trigger / 100) - 1) * 100


def _lane_rows(episodes: list[CaptureEpisode]) -> tuple[LaneRow, ...]:
    rows: list[LaneRow] = []
    for window in ("30m_to_15m", "15m_to_5m", "5m_to_1m"):
        for horizon in (60, 300, 900):
            rows.append(
                _lane_row(
                    episodes,
                    lane="early_long",
                    feature_name=f"{window}_imbalance_lift",
                    horizon_seconds=horizon,
                    feature_for=lambda event, controls, selected=window: _imbalance_lift(
                        selected,
                        event,
                        controls,
                    ),
                    return_for=lambda subject, seconds, selected=window: _return_from_window_end(
                        subject, selected, seconds
                    ),
                    short_return=False,
                )
            )
    for horizon in (60, 300, 900):
        rows.append(
            _lane_row(
                episodes,
                lane="squeeze_avoidance",
                feature_name="1m_to_trigger_imbalance_lift",
                horizon_seconds=horizon,
                feature_for=lambda event, controls: _imbalance_lift(
                    "1m_to_trigger",
                    event,
                    controls,
                ),
                short_return=False,
            )
        )
    for horizon in (900, 3_600):
        rows.append(
            _lane_row(
                episodes,
                lane="delayed_short",
                feature_name="5m_to_1m_minus_1m_exhaustion_lift",
                horizon_seconds=horizon,
                feature_for=_exhaustion_lift,
                short_return=True,
            )
        )
    return tuple(rows)


def build_orderflow_report(
    episodes: tuple[CaptureEpisode, ...],
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
) -> OrderflowPilotReport:
    exclusions: Counter[str] = Counter()
    complete: list[CaptureEpisode] = []
    for episode in episodes:
        eligible, reason = _complete_episode(episode, until)
        if eligible:
            complete.append(episode)
        elif reason is not None:
            exclusions[reason] += 1
    clusters = len({episode.event_base for episode in complete})
    market_days = len({episode.first_observed_at.date() for episode in complete})
    requirements = (
        f"{ORDERFLOW_MIN_COMPLETE_EPISODES} complete matched episodes",
        f"{ORDERFLOW_MIN_CLUSTERS} asset clusters",
        f"{ORDERFLOW_MIN_MARKET_DAYS} UTC market days",
    )
    ready = (
        len(complete) >= ORDERFLOW_MIN_COMPLETE_EPISODES
        and clusters >= ORDERFLOW_MIN_CLUSTERS
        and market_days >= ORDERFLOW_MIN_MARKET_DAYS
    )
    return OrderflowPilotReport(
        manifest=OrderflowManifest(
            report_version=ORDERFLOW_REPORT_VERSION,
            capture_contract_version=ORDERFLOW_CONTRACT_VERSION,
            code_revision=normalize_code_revision(code_revision),
            working_tree_dirty=working_tree_dirty,
            generated_at=generated_at,
            dataset_since=since,
            dataset_until_exclusive=until,
            input_fingerprint=input_fingerprint,
            root=str(root),
            prebuffer_seconds=ORDERFLOW_PREBUFFER_SECONDS,
            capture_after_seconds=ORDERFLOW_CAPTURE_AFTER_SECONDS,
            required_controls=ORDERFLOW_REQUIRED_CONTROLS,
            lead_windows=LEAD_WINDOWS,
            post_horizons_seconds=POST_HORIZONS_SECONDS,
        ),
        files=files,
        records=records,
        capture_episodes=len(episodes),
        complete_matched_episodes=len(complete),
        clusters=clusters,
        market_days=market_days,
        readiness="discovery_ready" if ready else "collecting",
        readiness_requirements=requirements,
        exclusion_reasons=tuple(
            CountRow(name, count)
            for name, count in sorted(
                exclusions.items(),
                key=lambda item: (-item[1], item[0]),
            )
        ),
        capture_quality=_quality_rows(episodes),
        lead_features=_lead_feature_rows(complete),
        lanes=_lane_rows(complete),
    )


def render_json(report: OrderflowPilotReport) -> str:
    return json.dumps(json_ready(asdict(report)), sort_keys=True, indent=2) + "\n"


def render_markdown(report: OrderflowPilotReport) -> str:
    lines = [
        "# Bybit Order-Flow Pilot Report",
        "",
        f"Generated: {report.manifest.generated_at.isoformat()}",
        f"Code revision: `{report.manifest.code_revision}`",
        f"Working tree dirty: {'yes' if report.manifest.working_tree_dirty else 'no'}",
        f"Capture contract: `{report.manifest.capture_contract_version}`",
        f"Input fingerprint: `{report.manifest.input_fingerprint}`",
        (
            f"Scope: {report.manifest.dataset_since.isoformat()} <= first observed < "
            f"{report.manifest.dataset_until_exclusive.isoformat()}"
        ),
        "",
        "> Discovery-only report. It cannot change production strategy or authorize trading.",
        "",
        "## Readiness",
        "",
        *markdown_table(
            ("Metric", "Value"),
            [
                ("Capture episodes", report.capture_episodes),
                ("Complete matched episodes", report.complete_matched_episodes),
                ("Asset clusters", report.clusters),
                ("UTC market days", report.market_days),
                ("Files", report.files),
                ("Records", report.records),
                ("Status", report.readiness),
            ],
        ),
        "",
        "Required before interpreting discovery associations: "
        + ", ".join(report.readiness_requirements)
        + ".",
        "",
        "## Exclusions",
        "",
        *markdown_table(
            ("Reason", "Episodes"),
            [(row.name, row.count) for row in report.exclusion_reasons],
        ),
        "",
        "## Capture quality",
        "",
        *markdown_table(
            (
                "Role",
                "Subjects",
                "Records",
                "Median records",
                "Median anchor stale",
                "P95 max lag",
            ),
            [
                (
                    row.role,
                    row.subjects,
                    row.records,
                    format_number(row.median_records, 0),
                    format_number(row.median_anchor_staleness_ms, 0, suffix=" ms"),
                    format_number(row.p95_max_lag_ms, 0, suffix=" ms"),
                )
                for row in report.capture_quality
            ],
        ),
        "",
        "## Pre-trigger order flow",
        "",
        *markdown_table(
            (
                "Window",
                "N",
                "Event imbalance",
                "Control imbalance",
                "Imbalance lift",
                "Event notional/s",
                "Control notional/s",
                "Event price return",
                "Remaining move to trigger",
            ),
            [
                (
                    row.window,
                    row.matched_episodes,
                    format_number(row.median_event_imbalance, 3),
                    format_number(row.median_control_imbalance, 3),
                    format_number(row.median_imbalance_lift, 3),
                    format_number(row.median_event_notional_per_second, 2),
                    format_number(row.median_control_notional_per_second, 2),
                    format_percentage(row.median_event_price_return_pct),
                    format_percentage(row.median_event_return_to_trigger_pct),
                )
                for row in report.lead_features
            ],
        ),
        "",
        "## Separate discovery lanes",
        "",
        *markdown_table(
            (
                "Lane",
                "Feature",
                "Horizon",
                "N",
                "Clusters",
                "Largest cluster",
                "Median feature",
                "Event return",
                "Return lift",
                "Rank corr",
                "Weakest asset LOO",
                "Weakest day LOO",
                "Positive feature / price up",
            ),
            [
                (
                    row.lane,
                    row.feature,
                    f"{row.horizon_seconds // 60}m",
                    row.matched_episodes,
                    row.clusters,
                    format_percentage(row.largest_cluster_share_pct),
                    format_number(row.median_feature, 3),
                    format_percentage(row.median_event_return_pct),
                    format_percentage(row.median_return_lift_pct),
                    format_number(row.rank_correlation, 3),
                    format_number(row.weakest_asset_exclusion_correlation, 3),
                    format_number(row.weakest_day_exclusion_correlation, 3),
                    format_percentage(row.positive_feature_price_up_pct),
                )
                for row in report.lanes
            ],
        ),
        "",
        (
            "Early-long and squeeze-avoidance use long returns. Delayed-short uses signed "
            "short returns. Correlations are descriptive and compare each event with its "
            "point-in-time matched controls."
        ),
        "",
    ]
    if report.readiness != "discovery_ready":
        lines.extend(
            [
                "*Economic interpretation is withheld while the registered sample is collecting.*",
                "",
            ]
        )
    return "\n".join(lines)


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
    episodes, files, records, fingerprint = load_capture_episodes(
        args.root,
        since=args.since,
        until=until,
    )
    report = build_orderflow_report(
        episodes,
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
