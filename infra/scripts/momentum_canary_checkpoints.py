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

Each run:
1. Reads the momentum-capture service's own started_at_ms from its Redis
   health snapshot (market:momentumcapture:health) -- never a value this
   script computes or is told, so a restart is detected and the checkpoint
   clock resets automatically rather than needing a human to re-point it.
2. Computes the 24h/48h/72h due times from that real start.
3. For each due-but-not-yet-fired checkpoint, collects health, container
   RSS/CPU, host swap counters, and Timescale-hypertable storage (chunk-aware:
   hypertable_detailed_size and hypertable_compression_stats, not a bare
   pg_relation_size on the parent relation, since hypertable rows live in
   per-chunk child tables), writes an atomic JSON snapshot, and sends one
   Telegram summary.

This never stops or restarts momentum-capture. The 48-to-72h checkpoint in
ROADMAP.md item 6 is a human go/no-go decision; this runner's only job is to
make sure that decision has real, timely numbers in front of it instead of
someone remembering to run `make prod-momentum-capture-health` by hand.

Swap is reported two ways because a single point-in-time sample cannot show
whether swapping was "sustained" (the ROADMAP gate's actual wording): current
swap used (a snapshot) plus cumulative pswpin/pswpout page counters from
/proc/vmstat (since-boot totals) -- a human diffing those counters across the
24h/48h/72h snapshots sees real swap activity during the canary window, which
a single sample cannot.
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
from typing import Any

SNAPSHOT_VERSION = "momentum_canary_checkpoints_v1"

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


def _notification_text(offset_hours: int, snapshot: dict[str, Any]) -> str:
    health = snapshot["health"]
    storage = snapshot["timescale_storage"]
    mc = snapshot["momentum_capture_container"]
    coll = snapshot["collector_container"]
    return (
        f"\U0001f3af Momentum-capture canary checkpoint: {offset_hours}h\n"
        f"Elapsed: {snapshot['elapsed_hours']:.1f}h\n"
        f"Bars completed: {health.get('bars_completed_total', 'n/a')}\n"
        f"Missing ticker/trades symbols: {health.get('symbols_missing_ticker_count', 'n/a')}"
        f"/{health.get('symbols_missing_trades_count', 'n/a')}\n"
        f"Persist retries: {health.get('persist_retries_total', 'n/a')}  "
        f"NATS drops: {health.get('nats_dropped_total', 'n/a')}  "
        f"Late events: {health.get('late_events_total', 'n/a')}\n"
        f"Writer queue depth: {health.get('writer_queue_depth', 'n/a')}\n"
        f"RSS momentum-capture: {mc.get('MemUsage', 'n/a')}  CPU: {mc.get('CPUPerc', 'n/a')}\n"
        f"RSS collector: {coll.get('MemUsage', 'n/a')}  CPU: {coll.get('CPUPerc', 'n/a')}\n"
        f"Host swap used: {snapshot['host_swap_used_mb']} MiB "
        f"(pswpin={snapshot['host_swap_counters'].get('pswpin', 'n/a')} "
        f"pswpout={snapshot['host_swap_counters'].get('pswpout', 'n/a')} since boot)\n"
        f"Timescale storage: {_mib(storage['total_bytes']):.0f} MiB total, "
        f"{storage['compressed_chunks']}/{storage['total_chunks']} chunks compressed "
        f"(before={_mib(storage['before_compression_total_bytes']):.0f} MiB, "
        f"after={_mib(storage['after_compression_total_bytes']):.0f} MiB)\n"
        f"Rows: {storage['row_count']:,}"
    )


def _utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def run_once(
    *,
    now: datetime,
    snapshot_path: Path,
    force_offset: int | None = None,
) -> dict[str, Any]:
    previous = _read_json(snapshot_path)
    if previous.get("version") != SNAPSHOT_VERSION:
        previous = {}

    try:
        health = _read_momentum_health()
        epoch_started_at_ms = int(health["started_at_ms"])
    except (RuntimeError, KeyError, ValueError) as exc:
        payload = {
            **previous,
            "version": SNAPSHOT_VERSION,
            "generated_at": _utc(now),
            "last_error": str(exc),
        }
        _atomic_json(snapshot_path, payload)
        raise

    if previous.get("epoch_started_at_ms") != epoch_started_at_ms:
        # New epoch: momentum-capture started (or restarted) since we last
        # looked. The checkpoint clock resets to this real start automatically
        # -- no human needs to re-point anything.
        logger.info(
            "new momentum-capture epoch detected: started_at_ms=%s (previous=%s)",
            epoch_started_at_ms,
            previous.get("epoch_started_at_ms"),
        )
        previous = {
            "epoch_started_at_ms": epoch_started_at_ms,
            "fired_offsets_hours": [],
            "error_notified_offsets_hours": [],
            "history": [],
        }

    epoch_start = datetime.fromtimestamp(epoch_started_at_ms / 1000, tz=UTC)
    fired = set(previous.get("fired_offsets_hours", []))
    error_notified = set(previous.get("error_notified_offsets_hours", []))
    history = list(previous.get("history", []))
    last_error: str | None = None

    for offset_hours in CHECKPOINT_OFFSETS_HOURS:
        if offset_hours in fired:
            continue
        due_at = epoch_start + timedelta(hours=offset_hours)
        if now < due_at and force_offset != offset_hours:
            continue
        try:
            checkpoint = collect_checkpoint(
                health=health, now=now, epoch_started_at_ms=epoch_started_at_ms
            )
            message = _notification_text(offset_hours, checkpoint)
            alert_error = _notify(message)
            history.append(
                {"offset_hours": offset_hours, **checkpoint, "notify_error": alert_error}
            )
            fired.add(offset_hours)
            error_notified.discard(offset_hours)
        except (RuntimeError, ValueError) as exc:
            last_error = f"{offset_hours}h checkpoint due but collection failed: {exc}"
            logger.error(last_error)
            if offset_hours not in error_notified:
                _notify(
                    f"⚠️ Momentum-capture canary checkpoint {offset_hours}h is due but "
                    f"data collection failed: {exc}\n"
                    f"Will keep retrying; this alert will not repeat."
                )
                error_notified.add(offset_hours)
        if force_offset == offset_hours:
            break

    payload = {
        "version": SNAPSHOT_VERSION,
        "generated_at": _utc(now),
        "epoch_started_at_ms": epoch_started_at_ms,
        "fired_offsets_hours": sorted(fired),
        "error_notified_offsets_hours": sorted(error_notified),
        "last_error": last_error,
        "history": history,
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
        "--force",
        type=int,
        choices=CHECKPOINT_OFFSETS_HOURS,
        help="Collect and notify this checkpoint immediately, even if not yet due.",
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
        run_once(now=datetime.now(UTC), snapshot_path=args.snapshot, force_offset=args.force)


if __name__ == "__main__":
    main()
