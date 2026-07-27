import argparse
import asyncio

import redis.asyncio as aioredis
import structlog

from .config import Config
from .funding import fetch_funding_rates_for_pumps
from .oi import fetch_oi_for_pumps
from .persistence import (
    close_retrace,
    get_open_episode_ids,
    get_tracked_bases,
    insert_funding_rate_snapshots,
    insert_oi_snapshots,
    update_last_pct,
    upsert_pumps,
)
from .scanner import publish, run_once
from .snapshots import take_due_snapshots

log = structlog.get_logger()


async def _run(once: bool) -> None:
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ]
    )

    cfg = Config()
    log.info(
        "scanner.starting",
        exchanges=cfg.exchanges,
        measurement_min_pct=cfg.measurement_min_pct,
        entry_min_pct=cfg.entry_min_pct,
        interval=cfg.interval,
        db=bool(cfg.db_url),
    )

    rdb: aioredis.Redis = aioredis.from_url(f"redis://{cfg.redis_addr}")
    try:
        while True:
            extra_bases: frozenset[str] = frozenset()
            if cfg.db_url:
                extra_bases = await get_tracked_bases(cfg.db_url)

            batch = await run_once(
                cfg.exchanges,
                cfg.measurement_min_pct,
                extra_bases,
            )
            if batch is None:
                if once:
                    break
                await asyncio.sleep(cfg.interval)
                continue

            pumps = batch.pumps
            scan_errors = batch.errors
            below_updates = batch.below_updates
            tracked_pumps = batch.tracked_pumps
            publish_ready = True

            if cfg.db_url and pumps:
                episode_ids = await upsert_pumps(
                    cfg.db_url,
                    pumps,
                    cfg.entry_min_pct,
                )
                expected_bases = {pump["base"] for pump in pumps}
                publish_ready = episode_ids.keys() == expected_bases
                if publish_ready:
                    for pump in pumps:
                        pump["pump_event_id"] = episode_ids[pump["base"]]
                else:
                    log.error(
                        "scanner.publish_skipped",
                        reason="pump event persistence incomplete",
                        pumps=len(pumps),
                        attributed=len(episode_ids),
                    )

            # Publish only after all live pumps have durable episode ids. This closes
            # the race where api-gateway and execution saw a pump before its DB event.
            if publish_ready:
                await publish(
                    batch,
                    cfg.measurement_min_pct,
                    cfg.entry_min_pct,
                    rdb,
                )

            if cfg.db_url:
                if below_updates:
                    await update_last_pct(cfg.db_url, below_updates)

                # OI + funding rates for live + still-tracked (faded) pumps, so the
                # retrace phase keeps accumulating data — not just while the pump is live.
                oi_targets = pumps + tracked_pumps
                if oi_targets:
                    oi_rows, fr_rows = await asyncio.gather(
                        fetch_oi_for_pumps(oi_targets),
                        fetch_funding_rates_for_pumps(oi_targets),
                    )
                    all_bases = {row["base"] for row in oi_rows} | {row["base"] for row in fr_rows}
                    episode_ids = await get_open_episode_ids(cfg.db_url, all_bases)
                    for row in oi_rows:
                        row["event_id"] = episode_ids.get(row["base"])
                    for row in fr_rows:
                        row["event_id"] = episode_ids.get(row["base"])
                    await asyncio.gather(
                        insert_oi_snapshots(cfg.db_url, oi_rows),
                        insert_funding_rate_snapshots(cfg.db_url, fr_rows),
                    )

                # Snapshots before close: a token that disappears this cycle can
                # still get its due snapshot recorded before the episode is closed.
                await take_due_snapshots(cfg.db_url)

                # Skip retrace-close when any exchange failed: partial data can make
                # live tokens look absent and incorrectly close their episodes.
                if not scan_errors:
                    live_bases = {p["base"] for p in pumps}
                    await close_retrace(cfg.db_url, live_bases, cfg.close_after_misses)
                else:
                    log.warning(
                        "scanner.retrace_skip",
                        reason="exchange errors present",
                        failed=list(scan_errors.keys()),
                    )

            if once:
                break
            await asyncio.sleep(cfg.interval)
    finally:
        await rdb.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Multi-exchange pump scanner")
    parser.add_argument("--once", action="store_true", help="Run one scan then exit")
    args = parser.parse_args()
    asyncio.run(_run(args.once))
