#!/usr/bin/env python3
"""Start-relative momentum-capture canary checkpoints (ROADMAP item 6).

Deliberately a separate mechanism from research_checkpoints.py, not a new row
in that module's CHECKPOINTS tuple: that runner fires on fixed calendar dates
tied to research contracts (e.g. OI_GROWTH_FILTER_COHORT_START). The
48-to-72-hour resource and data-quality canary in ROADMAP.md item 6 is defined
relative to when the momentum-capture service actually started, which is not
known in advance and can change (a restart moves it) -- a fixed-date checkpoint
spec cannot express that, so this is a small runner with its own state file
instead of a new CHECKPOINTS entry.

State model (one JSON snapshot file):

    {
      "active_epoch": {
        "started_at_ms": ...,
        "status": "running" | "interrupted",
        "baseline": {...} | null,          # captured once, at epoch creation
        "checkpoints": {
          "24": {"state": "pending"|"collected"|"notified"|"missed", ...},
          "48": {...},
          "72": {...},
        },
        "late_snapshots": [...],           # catch-up reads after a long gap
        "diagnostic_snapshots": [...],     # from --sample-now, never official
      },
      "archived_epochs": [...]             # previous epochs, never discarded
    }

Each run:
1. Reads the momentum-capture service's own started_at_ms from its Redis
   health snapshot (market:momentumcapture:health) -- never a value this
   script computes or is told. A changed started_at_ms means a restart:
   the OLD epoch is archived (full history kept, a Telegram alert sent), not
   silently wiped -- a restart at hour 70 must not erase evidence of a
   struggling canary, and whether the new epoch counts as a fresh canary is a
   human decision, not this script's.
2. If health is unreadable while an epoch is active, that is itself alerted
   once (edge-triggered, like the notifier's own stale/recovered pattern) and
   the epoch is marked interrupted, so a momentum-capture death at hour 24
   cannot pass in total silence.
3. A baseline (storage, swap counters, health) is captured once at epoch
   start. Every checkpoint after that reports both the absolute numbers and
   the delta/rate-per-day since baseline, because the ROADMAP gates this
   canary exists for (bytes/day, sustained swap) are rate gates, not
   snapshot gates.
4. Checkpoints are collected within 2h of their true due time; later than
   that (e.g. the host was down across the window) the offset is marked
   "missed" and, once per run, a single distinctly-labeled late/catch-up
   snapshot is taken instead of mislabeling stale data as if it were that
   checkpoint's own timely read.
5. A Telegram send failure never permanently loses a checkpoint: data is
   saved atomically as "collected" first, and only promoted to "notified"
   once delivery actually succeeds, retried every run until it does. The same
   protection covers the interrupted/recovered/restart operational alerts
   (queued in pending_operational_alerts and replayed verbatim until
   delivered), which have no underlying data to protect, just the message
   itself.
6. --sample-now takes an out-of-band diagnostic read and always sends it, but
   never touches official checkpoint state -- it cannot pre-empt a real
   24/48/72h checkpoint.

This never stops or restarts momentum-capture. The 48-to-72h checkpoint in
ROADMAP.md item 6 is a human go/no-go decision; this runner's only job is to
make sure that decision has real, timely, honestly-labeled numbers in front
of it.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import subprocess
import tempfile
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

SNAPSHOT_VERSION = "momentum_canary_checkpoints_v2"

REDIS_CONTAINER = "schurfer-redis"
POSTGRES_CONTAINER = "schurfer-postgres"
MOMENTUM_CONTAINER = "schurfer-momentum-capture"
COLLECTOR_CONTAINER = "schurfer-collector"
DB_USER = "schurfer"
DB_NAME = "schurfer"
HEALTH_KEY = "market:momentumcapture:health"
HYPERTABLE = "timeseries.bybit_momentum_bars_1m"

# 24h is an early warning, not itself a decision point; 48h/72h bracket the
# ROADMAP item 6 go/no-go window.
CHECKPOINT_OFFSETS_HOURS = (24, 48, 72)

# A checkpoint collected within this much of its true due time still counts
# as that checkpoint. Sized well above normal 15-minute timer jitter so it
# only kicks in for a real interruption (host down, timer disabled), not
# routine scheduling slack.
CHECKPOINT_GRACE = timedelta(hours=2)

logger = logging.getLogger("momentum-canary-checkpoints")


def _run(command: list[str], *, timeout: int) -> str:
    try:
        result = subprocess.run(  # noqa: S603 -- fixed argv, no shell, no user input
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"{command[0]} timed out after {timeout}s: {' '.join(command)}") from exc
    if result.returncode != 0:
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(command)}\n{result.stderr.strip()}"
        )
    return result.stdout


def _read_momentum_health(timeout: int = 15) -> dict[str, str]:
    raw = _run(
        ["docker", "exec", REDIS_CONTAINER, "redis-cli", "--raw", "HGETALL", HEALTH_KEY],
        timeout=timeout,
    )
    lines = raw.splitlines()
    if not lines:
        raise RuntimeError(f"{HEALTH_KEY} is empty or missing in redis")
    if len(lines) % 2 != 0:
        raise RuntimeError(f"{HEALTH_KEY} HGETALL returned an odd number of fields")
    return dict(zip(lines[0::2], lines[1::2], strict=True))


def _docker_stats(container: str, timeout: int = 15) -> dict[str, Any]:
    raw = _run(
        ["docker", "stats", "--no-stream", "--format", "{{json .}}", container], timeout=timeout
    )
    lines = [line for line in raw.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError(f"docker stats returned nothing for {container}")
    return json.loads(lines[-1])


def _psql_csv(query: str, *, timeout: int) -> list[str]:
    raw = _run(
        [
            "docker",
            "exec",
            POSTGRES_CONTAINER,
            "psql",
            "-U",
            DB_USER,
            "-d",
            DB_NAME,
            "-t",
            "-A",
            "-F,",
            "-c",
            query,
        ],
        timeout=timeout,
    )
    line = raw.strip().splitlines()[0] if raw.strip() else ""
    return line.split(",")


def _hypertable_storage(timeout: int = 30) -> dict[str, Any]:
    # HYPERTABLE is a module-level constant, not user input, so this is not a
    # real injection surface -- ruff can't tell the difference from a request
    # parameter, hence the explicit suppressions below.
    detailed_size_query = (
        f"SELECT COALESCE(sum(table_bytes),0), COALESCE(sum(index_bytes),0), "  # noqa: S608
        f"COALESCE(sum(toast_bytes),0), COALESCE(sum(total_bytes),0) "
        f"FROM hypertable_detailed_size('{HYPERTABLE}')"
    )
    compression_stats_query = (
        f"SELECT COALESCE(total_chunks,0), COALESCE(number_compressed_chunks,0), "  # noqa: S608
        f"COALESCE(before_compression_total_bytes,0), COALESCE(after_compression_total_bytes,0) "
        f"FROM hypertable_compression_stats('{HYPERTABLE}')"
    )
    row_count_query = f"SELECT count(*) FROM {HYPERTABLE}"  # noqa: S608

    table_bytes, index_bytes, toast_bytes, total_bytes = (
        int(v) for v in _psql_csv(detailed_size_query, timeout=timeout)
    )
    total_chunks, compressed_chunks, before_bytes, after_bytes = (
        int(v) for v in _psql_csv(compression_stats_query, timeout=timeout)
    )
    (row_count,) = (int(v) for v in _psql_csv(row_count_query, timeout=max(timeout, 60)))
    return {
        "table_bytes": table_bytes,
        "index_bytes": index_bytes,
        "toast_bytes": toast_bytes,
        "total_bytes": total_bytes,
        "total_chunks": total_chunks,
        "compressed_chunks": compressed_chunks,
        "uncompressed_chunks": total_chunks - compressed_chunks,
        "before_compression_total_bytes": before_bytes,
        "after_compression_total_bytes": after_bytes,
        "row_count": row_count,
    }


def _swap_used_mb(meminfo_path: Path = Path("/proc/meminfo")) -> int:
    values: dict[str, int] = {}
    for line in meminfo_path.read_text().splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] in {"SwapTotal:", "SwapFree:"}:
            values[parts[0]] = int(parts[1])
    if "SwapTotal:" not in values or "SwapFree:" not in values:
        raise ValueError("SwapTotal or SwapFree missing from /proc/meminfo")
    return (values["SwapTotal:"] - values["SwapFree:"]) // 1024


def _swap_activity_counters(vmstat_path: Path = Path("/proc/vmstat")) -> dict[str, int]:
    values: dict[str, int] = {}
    for line in vmstat_path.read_text().splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] in {"pswpin", "pswpout"}:
            values[parts[0]] = int(parts[1])
    return values


def _atomic_json(path: Path, payload: dict[str, Any], *, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
        temporary.chmod(mode)
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return value if isinstance(value, dict) else {}


def _notify(message: str) -> str | None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return "telegram credentials are missing"
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=urllib.parse.urlencode({"chat_id": chat_id, "text": message}).encode(),
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
            if response.status >= 300:
                return f"telegram status {response.status}"
    except OSError as exc:
        return f"telegram delivery failed ({type(exc).__name__})"
    return None


def _mib(byte_count: int) -> float:
    return byte_count / (1024 * 1024)


def _utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def collect_checkpoint(
    *, health: dict[str, str], now: datetime, epoch_started_at_ms: int
) -> dict[str, Any]:
    momentum_stats = _docker_stats(MOMENTUM_CONTAINER)
    collector_stats = _docker_stats(COLLECTOR_CONTAINER)
    storage = _hypertable_storage()
    swap_used_mb = _swap_used_mb()
    swap_counters = _swap_activity_counters()
    elapsed_hours = (now.timestamp() * 1000 - epoch_started_at_ms) / (3600 * 1000)
    return {
        "collected_at": _utc(now),
        "elapsed_hours": round(elapsed_hours, 2),
        "health": health,
        "momentum_capture_container": momentum_stats,
        "collector_container": collector_stats,
        "host_swap_used_mb": swap_used_mb,
        "host_swap_counters": swap_counters,
        "timescale_storage": storage,
    }


def _rate_per_day(delta: float, elapsed_hours: float) -> float | None:
    if elapsed_hours <= 0:
        return None
    return delta / (elapsed_hours / 24)


def _hot_bytes(storage: dict[str, Any]) -> int:
    # ROADMAP.md item 6's two storage gates apply to different populations of
    # chunks, not the table as a whole: the still-uncompressed "hot" chunk
    # (normally just the current day, under the 1-day compress_after policy in
    # migration 0024) versus everything already compressed. total_bytes (from
    # hypertable_detailed_size) is the current physical size across ALL
    # chunks; after_compression_total_bytes (from hypertable_compression_stats)
    # is the physical size of only the chunks that are already compressed. The
    # remainder is the hot chunk's own footprint.
    return int(storage["total_bytes"]) - int(storage["after_compression_total_bytes"])


def _delta_block(current: dict[str, Any], baseline: dict[str, Any] | None) -> dict[str, Any] | None:
    if baseline is None:
        return None
    elapsed_hours = current["elapsed_hours"] - baseline["elapsed_hours"]
    current_storage = current["timescale_storage"]
    baseline_storage = baseline["timescale_storage"]

    # A plain delta of the hot-chunk inventory (_hot_bytes now minus
    # _hot_bytes before) understates raw ingest whenever a chunk rotates out
    # of the hot bucket into compressed between the two checkpoints: that
    # whole chunk's growth would otherwise vanish from the delta the moment
    # it compresses, since the new hot chunk starts back near zero. Fix:
    # before_compression_total_bytes is a monotonic cumulative counter -- it
    # only grows, by exactly a chunk's raw pre-compression size, the instant
    # that chunk compresses -- so at any time t, hot(t) + before_compression(t)
    # equals the total raw bytes ever ingested (mass conservation across the
    # hot/compressed boundary, modulo retention-policy drops that are
    # irrelevant over a 3-day canary against a 35-day retention window).
    # Its delta plus the hot-inventory delta reconstructs the true raw
    # ingestion rate regardless of how many rotations happened in between.
    raw_ingest_delta = (_hot_bytes(current_storage) - _hot_bytes(baseline_storage)) + (
        current_storage["before_compression_total_bytes"]
        - baseline_storage["before_compression_total_bytes"]
    )
    # after_compression_total_bytes is itself a monotonic cumulative counter
    # (only grows when a chunk newly compresses, never shrinks), so a plain
    # delta is already correct here as a MEASUREMENT -- no rotation
    # correction needed. It is still a systematically LAGGING one, though,
    # for a reason distinct from the hot-inventory bug above: TimescaleDB's
    # compress_after is measured from a chunk's own newest data, not its
    # start. With chunk_time_interval=1 day and compress_after=1 day (see
    # migration 0024), a chunk covering day D only becomes ELIGIBLE for
    # compression once day D+2 00:00 arrives (a full day after the chunk's
    # own end, which is itself a day after it started) -- not "1 day after
    # it started" -- plus however long the background compression job takes
    # to actually run once eligible. Over a 72h canary at most 1-2 chunks can
    # possibly complete that full pipeline, so dividing the bytes that made
    # it through by the FULL calendar-elapsed time systematically
    # understates the true steady-state compressed rate, worst early in the
    # canary. `compressed_rate_bytes_per_day` below is kept as that honest
    # measurement (a lagging lower bound, not the number to check the 500
    # MiB/day gate against). `steady_state_compressed_rate_bytes_per_day` is
    # a separate ESTIMATE immune to this latency: it multiplies the directly
    # measured raw ingest rate (no compression-job timing dependency) by the
    # compression ratio actually observed so far (also a real, if early,
    # measurement) -- this is the number the gate should actually be read
    # against, clearly labeled as an estimate rather than a direct count.
    compressed_delta = (
        current_storage["after_compression_total_bytes"]
        - baseline_storage["after_compression_total_bytes"]
    )
    compression_ratio = None
    if current_storage["before_compression_total_bytes"] > 0:
        compression_ratio = (
            current_storage["after_compression_total_bytes"]
            / current_storage["before_compression_total_bytes"]
        )
    raw_ingest_rate = _rate_per_day(raw_ingest_delta, elapsed_hours)
    steady_state_compressed_rate = (
        raw_ingest_rate * compression_ratio
        if raw_ingest_rate is not None and compression_ratio is not None
        else None
    )
    rows_delta = current_storage["row_count"] - baseline_storage["row_count"]
    pswpin_delta = current["host_swap_counters"].get("pswpin", 0) - baseline[
        "host_swap_counters"
    ].get("pswpin", 0)
    pswpout_delta = current["host_swap_counters"].get("pswpout", 0) - baseline[
        "host_swap_counters"
    ].get("pswpout", 0)
    return {
        "elapsed_hours_since_baseline": round(elapsed_hours, 2),
        "raw_ingest_delta_bytes": raw_ingest_delta,
        "raw_ingest_rate_bytes_per_day": raw_ingest_rate,
        "compressed_delta_bytes": compressed_delta,
        "compressed_rate_bytes_per_day": _rate_per_day(compressed_delta, elapsed_hours),
        "compression_ratio": compression_ratio,
        "steady_state_compressed_rate_bytes_per_day": steady_state_compressed_rate,
        "rows_delta": rows_delta,
        "pswpin_delta": pswpin_delta,
        "pswpout_delta": pswpout_delta,
    }


# Health fields surfaced verbatim in the Telegram summary, grouped for
# readability. Kept in sync with redis_store.go's StoreHealth -- if a field
# is added there for operational visibility, it belongs here too, per the
# "show every drop/error counter, not a hand-picked few" review note.
_HEALTH_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "Universe",
        (
            "ready_symbols",
            "subscribed_symbols",
            "universe_stale",
            "added_since_start_count",
            "removed_since_start_count",
            "symbols_missing_ticker_count",
            "symbols_missing_trades_count",
            "catalog_items_total",
            "crypto_perpetuals_included",
            "standard_crypto_included",
            "innovation_crypto_included",
            "dated_futures_excluded",
            "stock_perpetuals_excluded",
            "commodity_perpetuals_excluded",
            "unknown_contract_excluded",
            "unknown_symbol_type_excluded",
            "invalid_instrument_excluded",
            "non_usdt_excluded",
            "non_trading_excluded",
        ),
    ),
    (
        "Feed",
        (
            "input_queue_depth",
            "input_queue_peak",
            "input_queue_drops_total",
            "bars_completed_total",
            "late_events_total",
            "ticker_gap_total",
            "trade_reconnect_total",
            "trade_read_timeout_total",
            "nats_disconnect_total",
            "nats_reconnect_total",
            "nats_slow_consumer_total",
            "nats_dropped_total",
        ),
    ),
    (
        "Writer",
        (
            "writer_queue_depth",
            "writer_queue_peak",
            "writer_queue_drops_total",
            "bars_persisted_total",
            "persist_errors_total",
            "persist_retries_total",
            "rows_written_total",
            "payload_hash_mismatch_total",
        ),
    ),
    ("Lag", ("trade_lag_max_ms", "ticker_lag_max_ms")),
    (
        "Processing latency (us)",
        (
            "trade_receive_to_handle_p99_us",
            "trade_receive_to_handle_max_us",
            "trade_handler_p99_us",
            "trade_handler_max_us",
            "ticker_receive_to_handle_p99_us",
            "ticker_receive_to_handle_max_us",
            "ticker_handler_p99_us",
            "ticker_handler_max_us",
            "flush_p99_us",
            "flush_max_us",
            "health_publish_p99_us",
            "health_publish_max_us",
        ),
    ),
)


def _notification_text(
    label: str, snapshot: dict[str, Any], *, baseline: dict[str, Any] | None = None
) -> str:
    health = snapshot["health"]
    storage = snapshot["timescale_storage"]
    mc = snapshot["momentum_capture_container"]
    coll = snapshot["collector_container"]
    lines = [
        f"\U0001f3af Momentum-capture canary checkpoint: {label}",
        f"Status: {health.get('status', 'n/a')}  Elapsed: {snapshot['elapsed_hours']:.1f}h",
    ]
    for group_name, fields in _HEALTH_GROUPS:
        rendered = "  ".join(f"{field}={health.get(field, 'n/a')}" for field in fields)
        lines.append(f"{group_name}: {rendered}")
    lines.append(
        f"RSS momentum-capture: {mc.get('MemUsage', 'n/a')}  CPU: {mc.get('CPUPerc', 'n/a')}"
    )
    lines.append(f"RSS collector: {coll.get('MemUsage', 'n/a')}  CPU: {coll.get('CPUPerc', 'n/a')}")
    lines.append(
        f"Host swap used: {snapshot['host_swap_used_mb']} MiB "
        f"(pswpin={snapshot['host_swap_counters'].get('pswpin', 'n/a')} "
        f"pswpout={snapshot['host_swap_counters'].get('pswpout', 'n/a')} since boot)"
    )
    lines.append(
        f"Timescale storage: {_mib(storage['total_bytes']):.0f} MiB total, "
        f"{storage['compressed_chunks']}/{storage['total_chunks']} chunks compressed "
        f"(before={_mib(storage['before_compression_total_bytes']):.0f} MiB, "
        f"after={_mib(storage['after_compression_total_bytes']):.0f} MiB), "
        f"rows={storage['row_count']:,}"
    )
    delta = _delta_block(snapshot, baseline)
    if delta is not None:
        hot_rate = delta["raw_ingest_rate_bytes_per_day"]
        hot_rate_text = f"{_mib(hot_rate):.0f} MiB/day" if hot_rate is not None else "n/a"
        hot_sign = "+" if delta["raw_ingest_delta_bytes"] >= 0 else ""
        compressed_rate = delta["compressed_rate_bytes_per_day"]
        compressed_rate_text = (
            f"{_mib(compressed_rate):.0f} MiB/day" if compressed_rate is not None else "n/a"
        )
        compressed_sign = "+" if delta["compressed_delta_bytes"] >= 0 else ""
        rows_sign = "+" if delta["rows_delta"] >= 0 else ""
        steady_state_rate = delta["steady_state_compressed_rate_bytes_per_day"]
        steady_state_rate_text = (
            f"{_mib(steady_state_rate):.0f} MiB/day" if steady_state_rate is not None else "n/a"
        )
        ratio_text = (
            f"{delta['compression_ratio']:.2f}x"
            if delta["compression_ratio"] is not None
            else "n/a"
        )
        lines.append(
            f"Since baseline ({delta['elapsed_hours_since_baseline']:.1f}h), ROADMAP item 6's "
            f"two separate gates (hot <=1.5 GiB/day, compressed <=500 MiB/day): "
            f"hot {hot_sign}{_mib(delta['raw_ingest_delta_bytes']):.0f} MiB ({hot_rate_text})"
        )
        lines.append(
            f"Compressed, directly observed so far (a lagging lower bound -- a chunk only "
            f"becomes compression-eligible ~2 days after it starts, so this understates "
            f"steady state early in the canary): "
            f"{compressed_sign}{_mib(delta['compressed_delta_bytes']):.0f} MiB "
            f"({compressed_rate_text})"
        )
        lines.append(
            f"Compressed, steady-state ESTIMATE (raw ingest rate x observed compression "
            f"ratio {ratio_text}, immune to compression-job timing -- check the 500 MiB/day "
            f"gate against THIS, not the directly-observed number above): {steady_state_rate_text}"
        )
        lines.append(
            f"rows {rows_sign}{delta['rows_delta']:,}, "
            f"swap pswpin +{delta['pswpin_delta']} pswpout +{delta['pswpout_delta']}"
        )
    return "\n".join(lines)


def _send_or_queue(
    pending: list[dict[str, Any]],
    message: str,
    now: datetime,
    *,
    persist: Callable[[], None],
) -> None:
    """Send an operational alert (interrupted/recovered/restart), or queue it for
    retry if Telegram delivery fails.

    Persist-before-send: the pending entry is written to disk via `persist`
    BEFORE the network call, not after. Without this, a crash or systemd kill
    during or right after _notify (a real risk: the whole rest of run_once --
    baseline collection, the checkpoint loop -- still runs before the normal
    end-of-run write) would lose the in-memory record that this alert was ever
    detected, and the next run would silently start over. The cost is a rare
    duplicate send if delivery actually succeeded moments before such a crash
    -- acceptable, since Telegram has no real idempotency key and at-least-once
    is the honest guarantee this runner gives everywhere, not just here.
    """
    pending.append({"message": message, "created_at": _utc(now), "last_error": None})
    persist()
    error = _notify(message)
    if error is None:
        pending.pop()
    else:
        pending[-1]["last_error"] = error


def _flush_pending_alerts(pending: list[dict[str, Any]]) -> list[dict[str, Any]]:
    remaining = []
    for entry in pending:
        error = _notify(entry["message"])
        if error is not None:
            entry["last_error"] = error
            remaining.append(entry)
    return remaining


def _new_epoch(started_at_ms: int) -> dict[str, Any]:
    epoch_start = datetime.fromtimestamp(started_at_ms / 1000, tz=UTC)
    return {
        "started_at_ms": started_at_ms,
        "status": "running",
        "baseline": None,
        "baseline_collected_at": None,
        "interrupted_since": None,
        "last_error": None,
        "checkpoints": {
            str(hours): {
                "state": "pending",
                "due_at": _utc(epoch_start + timedelta(hours=hours)),
            }
            for hours in CHECKPOINT_OFFSETS_HOURS
        },
        "late_snapshots": [],
        "diagnostic_snapshots": [],
    }


def run_once(
    *,
    now: datetime,
    snapshot_path: Path,
    sample_now_offset: int | None = None,
) -> dict[str, Any]:
    previous = _read_json(snapshot_path)
    if previous.get("version") != SNAPSHOT_VERSION:
        previous = {}

    archived_epochs: list[dict[str, Any]] = list(previous.get("archived_epochs", []))
    active_epoch: dict[str, Any] | None = previous.get("active_epoch")
    # Retry any operational alert (interrupted/recovered/restart) a prior run
    # failed to deliver, before anything else -- these are one-shot texts with
    # no underlying data to protect, so replaying the same message is correct.
    pending_operational_alerts: list[dict[str, Any]] = _flush_pending_alerts(
        list(previous.get("pending_operational_alerts", []))
    )

    def _persist() -> None:
        # Captures active_epoch/archived_epochs/pending_operational_alerts by
        # reference (closure over run_once's locals), so it always writes
        # whatever they currently hold -- used as the persist-before-send step
        # in _send_or_queue, not just the final write at the end of this
        # function.
        _atomic_json(
            snapshot_path,
            {
                "version": SNAPSHOT_VERSION,
                "generated_at": _utc(now),
                "active_epoch": active_epoch,
                "archived_epochs": archived_epochs,
                "pending_operational_alerts": pending_operational_alerts,
            },
        )

    try:
        health = _read_momentum_health()
        epoch_started_at_ms = int(health["started_at_ms"])
        health_error: str | None = None
    except (RuntimeError, KeyError, ValueError) as exc:
        health = None
        epoch_started_at_ms = None
        health_error = str(exc)

    if health_error is not None:
        if active_epoch is not None:
            if active_epoch.get("status") != "interrupted":
                active_epoch["status"] = "interrupted"
                active_epoch["interrupted_since"] = _utc(now)
                old_start = datetime.fromtimestamp(active_epoch["started_at_ms"] / 1000, tz=UTC)
                _send_or_queue(
                    pending_operational_alerts,
                    f"\U0001f534 Momentum-capture canary: health became unreadable mid-epoch "
                    f"(started {_utc(old_start)}).\nError: {health_error}\n"
                    f"Will keep checking for recovery; this alert will not repeat.",
                    now,
                    persist=_persist,
                )
            active_epoch["last_error"] = health_error
        _persist()
        raise RuntimeError(health_error)

    assert health is not None and epoch_started_at_ms is not None  # narrows for type checkers

    # Checked once, up front: a restart (started_at_ms changed) is handled
    # entirely separately from an ordinary recovery below. Without this, an
    # epoch that was interrupted and then restarted with a NEW started_at_ms
    # would first get a false "recovered" alert (the OLD epoch never actually
    # resumed -- it gets archived a few lines later) on top of the real
    # restart alert.
    epoch_restarted = (
        active_epoch is not None and active_epoch.get("started_at_ms") != epoch_started_at_ms
    )

    if (
        active_epoch is not None
        and active_epoch.get("status") == "interrupted"
        and not epoch_restarted
    ):
        active_epoch["status"] = "running"
        active_epoch["interrupted_since"] = None
        active_epoch["last_error"] = None
        _send_or_queue(
            pending_operational_alerts,
            "\U0001f7e2 Momentum-capture canary: health readable again after an interruption.",
            now,
            persist=_persist,
        )

    if active_epoch is None or epoch_restarted:
        if active_epoch is not None:
            old_start = datetime.fromtimestamp(active_epoch["started_at_ms"] / 1000, tz=UTC)
            ran_hours = (now - old_start).total_seconds() / 3600
            was_interrupted = active_epoch.get("status") == "interrupted"
            archived = dict(active_epoch)
            archived["archived_at"] = _utc(now)
            archived["archived_reason"] = "restart_detected"
            archived_epochs.append(archived)
            logger.info(
                "new momentum-capture epoch detected: started_at_ms=%s (previous=%s)",
                epoch_started_at_ms,
                active_epoch["started_at_ms"],
            )
            interruption_note = (
                " It was still marked interrupted at restart time -- no separate recovery "
                "alert was sent for it, since it never actually resumed."
                if was_interrupted
                else ""
            )
            _send_or_queue(
                pending_operational_alerts,
                f"⚠️ Momentum-capture restarted mid-canary. Previous epoch "
                f"(started {_utc(old_start)}, ran {ran_hours:.1f}h) archived, not discarded."
                f"{interruption_note}\n"
                f"New epoch started "
                f"{_utc(datetime.fromtimestamp(epoch_started_at_ms / 1000, tz=UTC))}.\n"
                f"Whether this counts as a fresh canary or a continuation is a human call; "
                f"the previous epoch's evidence is preserved in the snapshot either way.",
                now,
                persist=_persist,
            )
        active_epoch = _new_epoch(epoch_started_at_ms)

    if active_epoch["baseline"] is None:
        try:
            baseline = collect_checkpoint(
                health=health, now=now, epoch_started_at_ms=epoch_started_at_ms
            )
            active_epoch["baseline"] = baseline
            active_epoch["baseline_collected_at"] = _utc(now)
        except (RuntimeError, ValueError) as exc:
            active_epoch["last_error"] = f"baseline collection failed: {exc}"
            logger.error(active_epoch["last_error"])

    epoch_start = datetime.fromtimestamp(active_epoch["started_at_ms"] / 1000, tz=UTC)
    late_snapshot_taken_this_run = False

    for offset_hours in CHECKPOINT_OFFSETS_HOURS:
        key = str(offset_hours)
        cp = active_epoch["checkpoints"][key]

        if sample_now_offset == offset_hours:
            try:
                sample = collect_checkpoint(
                    health=health, now=now, epoch_started_at_ms=active_epoch["started_at_ms"]
                )
                message = _notification_text(
                    f"{offset_hours}h (diagnostic sample-now)",
                    sample,
                    baseline=active_epoch["baseline"],
                )
                notify_error = _notify(message)
                active_epoch["diagnostic_snapshots"].append(
                    {
                        "requested_offset_hours": offset_hours,
                        "notify_error": notify_error,
                        **sample,
                    }
                )
            except (RuntimeError, ValueError) as exc:
                logger.error("sample-now %sh failed: %s", offset_hours, exc)
            continue

        if cp["state"] in ("notified", "missed"):
            continue

        if cp["state"] == "collected":
            # Data already gathered; only the previous Telegram send failed.
            # Retry delivery of the SAME saved message rather than
            # re-collecting -- re-collecting here would silently replace this
            # checkpoint's true point-in-time numbers with much later ones.
            notify_error = _notify(cp["message"])
            cp["notify_error"] = notify_error
            if notify_error is None:
                cp["state"] = "notified"
            continue

        due_at = epoch_start + timedelta(hours=offset_hours)
        if now < due_at:
            continue

        if now - due_at > CHECKPOINT_GRACE:
            cp["state"] = "missed"
            cp["missed_at"] = _utc(now)
            if not late_snapshot_taken_this_run:
                try:
                    sample = collect_checkpoint(
                        health=health, now=now, epoch_started_at_ms=active_epoch["started_at_ms"]
                    )
                    message = _notification_text(
                        "late catch-up (a checkpoint window was missed)",
                        sample,
                        baseline=active_epoch["baseline"],
                    )
                    notify_error = _notify(message)
                    active_epoch["late_snapshots"].append({"notify_error": notify_error, **sample})
                    late_snapshot_taken_this_run = True
                except (RuntimeError, ValueError) as exc:
                    logger.error("late catch-up snapshot failed: %s", exc)
            continue

        try:
            data = collect_checkpoint(
                health=health, now=now, epoch_started_at_ms=active_epoch["started_at_ms"]
            )
            message = _notification_text(
                f"{offset_hours}h", data, baseline=active_epoch["baseline"]
            )
            notify_error = _notify(message)
            cp["data"] = data
            cp["message"] = message
            cp["collected_at"] = _utc(now)
            cp["notify_error"] = notify_error
            cp["state"] = "notified" if notify_error is None else "collected"
        except (RuntimeError, ValueError) as exc:
            active_epoch["last_error"] = (
                f"{offset_hours}h checkpoint due but collection failed: {exc}"
            )
            logger.error(active_epoch["last_error"])
            if not cp.get("collection_error_notified", False):
                alert_error = _notify(
                    f"⚠️ Momentum-capture canary checkpoint {offset_hours}h is due but "
                    f"data collection failed: {exc}\n"
                    f"Will keep retrying; this alert will not repeat once delivered."
                )
                cp["collection_error_notified"] = alert_error is None

    payload = {
        "version": SNAPSHOT_VERSION,
        "generated_at": _utc(now),
        "active_epoch": active_epoch,
        "archived_epochs": archived_epochs,
        "pending_operational_alerts": pending_operational_alerts,
    }
    _atomic_json(snapshot_path, payload)
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--snapshot",
        type=Path,
        default=Path("/opt/schurfer/runtime/momentum-canary-checkpoints.json"),
    )
    parser.add_argument(
        "--sample-now",
        type=int,
        choices=CHECKPOINT_OFFSETS_HOURS,
        help=(
            "Take an out-of-band diagnostic reading labeled as this offset and send it, "
            "without touching official checkpoint state (the real 24/48/72h checkpoint "
            "still fires normally later)."
        ),
    )
    return parser


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = build_parser().parse_args()
    lock_path = args.snapshot.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("momentum canary checkpoint runner is already active") from None
        run_once(
            now=datetime.now(UTC), snapshot_path=args.snapshot, sample_now_offset=args.sample_now
        )


if __name__ == "__main__":
    main()
