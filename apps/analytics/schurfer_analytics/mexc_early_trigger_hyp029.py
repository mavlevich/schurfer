"""HYP-029: one MEXC early-trigger rule, entered on Bybit, re-checked on September 1m bars.

Registered before any September return was read: docs/research/mexc-early-trigger-hyp029-v1.md.

The August 5m exploration (`mexc_early_trigger_screen`, PR #462) found its best cell at a
MEXC 5m return of 8% or more with a Bybit 1h hold. That cell was chosen after viewing
three thresholds and several horizons, so this re-check fixes that single rule, before the
read, on the archived MEXC 1m bars for 2026-09-01..28, with a more realistic entry:

- **Trigger.** A 5m bar built from five MEXC 1m bars (aligned to 5-minute boundaries,
  all five present).
  - Return (close / open - 1) at least TRIGGER_RETURN.
  - Turnover at least VOLUME_MULTIPLE x the median 5m turnover of the prior 24h, and at
    least MIN_BAR_TURNOVER_USD.
  - A prior 24h change below MAX_PRIOR_24H_CHANGE.
  - At most one trigger per symbol per 24h.
- **Route, known at the signal.** Exactly one Bybit USDT linear perpetual for the base
  trading at the entry (catalogue of every status, delisted included), and the entry price
  within IDENTITY_BAND of the MEXC trigger close. A contract delisted during the hold stays
  in the routed denominator as the unresolved status `delisted_during_hold`.
- **Entry and exit.** Entry at the open of the Bybit 1m bar that starts ENTRY_DELAY after
  the MEXC bar closes; exit at the close of the Bybit 1m bar 59 minutes later (a 60-minute
  hold). A second entry delay is reported only, never used to choose anything.
- **Costs.** The primary cost is 0.4% per trade (not yet measured); 0.2% is reported.
- **Verdict**, by pooled mean net at the primary cost with an asset-clustered bootstrap. The
  floor is 30 legs, 20 assets and no week above 45%; unresolved legs must be at most 10%.
  1. `fail` when mature and non-positive;
  2. `insufficient_data` below the floor or over the ceiling;
  3. `candidate` when the mean and the interval's lower bound are both above zero;
  4. otherwise `fail`.

A `candidate` only allows a forward, registered websocket cohort with measured execution
costs. It never allows an order. Nothing on or after 2026-09-29T00:00Z is read: every exit
bar closes before it.

Phases: `prepare` builds the triggers from MEXC bars and freezes the Bybit catalogue and
the Bybit 1m bars of each leg (no return is computed). `read` takes the claim (inputs,
contract digest, reader revision) and computes the verdict from the stored inputs only.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from statistics import fmean, median
from typing import TYPE_CHECKING, Any

from .clustered_inference import ClusterObservation, cluster_bootstrap_mean, derived_seed
from .mexc_kline_archive import read_rows
from .source_lead_multi_source_report import (
    BYBIT_KLINE_URL,
    INPUTS_NAME,
    RESULT_NAME,
    AlreadyReadError,
    _body,
    _digest_path,
    _get_json,
    _sha,
    _write_digest,
    complete_digest,
    fetch_bybit_instruments,
    load_verified,
    parse_instruments,
    take_claim,
    write_once,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

FAMILY_VERSION = "hyp029_mexc_early_trigger_bybit_1h_v1"
HYPOTHESIS = "mexc_5m_ret8_bybit_1h"
WINDOW_START = datetime(2026, 9, 1, tzinfo=UTC)
BLIND_END = datetime(2026, 9, 29, tzinfo=UTC)  # nothing at or after it is read
READ_NOT_BEFORE = datetime(2026, 9, 29, 1, tzinfo=UTC)
MINUTE = 60
BAR = 300
DAY_BARS = 288
TRIGGER_RETURN = 0.08
VOLUME_MULTIPLE = 5.0
MIN_BAR_TURNOVER_USD = 20_000.0
MAX_PRIOR_24H_CHANGE = 0.10
ENTRY_DELAY = 60
SENSITIVITY_DELAY = 120
HOLD_MINUTES = 60
IDENTITY_BAND = 2.0
COST_PRIMARY_PCT = 0.4
COST_REPORTED_PCT = 0.2
FLOOR = {"min_legs": 30, "min_assets": 20, "max_week_share": 0.45}
MAX_UNRESOLVED_FRACTION = 0.10
BOOTSTRAP_ITERATIONS = 10_000
BOOTSTRAP_SEED = 20_260_928
CONTRACT: dict[str, Any] = {
    "family_version": FAMILY_VERSION,
    "hypothesis": HYPOTHESIS,
    "window": [WINDOW_START.isoformat(), BLIND_END.isoformat()],
    "trigger": {
        "return": TRIGGER_RETURN,
        "volume_multiple": VOLUME_MULTIPLE,
        "min_turnover_usd": MIN_BAR_TURNOVER_USD,
        "max_prior_24h_change": MAX_PRIOR_24H_CHANGE,
        "cooldown_bars": DAY_BARS,
    },
    "entry_delay_s": ENTRY_DELAY,
    "sensitivity_delay_s": SENSITIVITY_DELAY,
    "hold_minutes": HOLD_MINUTES,
    "identity_band": IDENTITY_BAND,
    "cost_primary_pct": COST_PRIMARY_PCT,
    "cost_reported_pct": COST_REPORTED_PCT,
    "floor": FLOOR,
    "max_unresolved_fraction": MAX_UNRESOLVED_FRACTION,
    "bootstrap": {"iterations": BOOTSTRAP_ITERATIONS, "seed": BOOTSTRAP_SEED},
}


def contract_sha256() -> str:
    return hashlib.sha256(json.dumps(CONTRACT, sort_keys=True).encode()).hexdigest()


def five_minute_bars(minutes: Sequence[dict[str, float]]) -> list[dict[str, float]]:
    """5m bars from 1m bars, aligned to 5-minute boundaries, only when all five exist."""
    by_t = {int(m["t"]): m for m in minutes}
    starts = sorted({t - t % BAR for t in by_t})
    out = []
    for s in starts:
        parts = [by_t.get(s + k * MINUTE) for k in range(5)]
        if any(p is None for p in parts):
            continue
        ps = [p for p in parts if p is not None]
        out.append(
            {
                "t": s,
                "o": ps[0]["o"],
                "h": max(p["h"] for p in ps),
                "l": min(p["l"] for p in ps),
                "c": ps[-1]["c"],
                "a": sum(p["a"] for p in ps),
            }
        )
    return out


def triggers_for(symbol: str, bars: list[dict[str, float]]) -> list[dict[str, Any]]:
    """Outcome-blind: the rule's triggers from MEXC 5m bars (pre-signal data only)."""
    lo, hi = int(WINDOW_START.timestamp()), int(BLIND_END.timestamp())
    # The latest exit bar (the sensitivity entry's) must close before the blind end.
    last_close = hi - (SENSITIVITY_DELAY + HOLD_MINUTES * MINUTE)
    ts = [int(b["t"]) for b in bars]
    out: list[dict[str, Any]] = []
    last = -(10**9)
    for i in range(DAY_BARS, len(bars)):
        b = bars[i]
        close_t = ts[i] + BAR
        if ts[i] < lo or close_t > last_close:
            continue
        if ts[i] - ts[i - DAY_BARS] != DAY_BARS * BAR or i - last < DAY_BARS:
            continue
        base = bars[i - DAY_BARS]["o"]
        if base <= 0 or b["o"] <= 0 or b["o"] / base - 1 >= MAX_PRIOR_24H_CHANGE:
            continue
        if b["c"] / b["o"] - 1 < TRIGGER_RETURN:
            continue
        med = median(x["a"] for x in bars[i - DAY_BARS : i])
        if b["a"] < MIN_BAR_TURNOVER_USD or med <= 0 or b["a"] < VOLUME_MULTIPLE * med:
            continue
        out.append({"symbol": symbol, "bar_t": ts[i], "close_t": close_t, "mexc_close": b["c"]})
        last = i
    return out


def route_for(trigger: dict[str, Any], instruments: Sequence[Any]) -> tuple[str | None, str]:
    """The single Bybit USDT perpetual for the base trading at the entry (known at the
    signal), or a reason. A later delisting is not a selection criterion here."""
    base = trigger["symbol"].removesuffix("_USDT").upper()
    entry_ms = (trigger["close_t"] + ENTRY_DELAY) * 1000
    live = [x for x in instruments if x.base == base and x.live_over(entry_ms, entry_ms)]
    if not live:
        return None, "no_bybit_perp"
    if len(live) > 1:
        return None, "ambiguous_bybit_route"
    return live[0].native_id, "route"


def delivery_ms_for(native: str, instruments: Sequence[Any]) -> int:
    return next((int(x.delivery_ms) for x in instruments if x.native_id == native), 0)


async def prepare_inputs(archive_dir: Path, now: datetime, code_revision: str) -> dict[str, Any]:
    """Freeze everything the read needs. Computes no return."""
    import httpx

    manifest = json.loads((archive_dir / "manifest.json").read_text())
    lo, hi = (datetime.fromisoformat(x) for x in manifest["window"])
    if manifest.get("interval") != "Min1" or lo > WINDOW_START or hi < BLIND_END:
        raise ValueError("the MEXC Min1 archive does not cover the registered window")
    triggers: list[dict[str, Any]] = []
    archive_sha: dict[str, str] = {}
    for symbol, entry in sorted(manifest["symbols"].items()):
        if entry.get("status") not in ("complete", "empty"):
            raise ValueError(f"archive not usable: {symbol} is {entry.get('status')}")
        rows, digest = read_rows(archive_dir / f"{symbol}.jsonl.gz")
        if digest != entry["sha256"]:
            raise ValueError(f"archive not usable: {symbol} differs from its manifest")
        archive_sha[symbol] = digest
        if rows:
            triggers.extend(triggers_for(symbol, five_minute_bars(rows)))
    raw_catalogue = await fetch_bybit_instruments()
    instruments = parse_instruments(raw_catalogue)
    legs: list[dict[str, Any]] = []
    semaphore = asyncio.Semaphore(6)
    async with httpx.AsyncClient(timeout=30) as client:

        async def one(trigger: dict[str, Any]) -> None:
            native, reason = route_for(trigger, instruments)
            leg: dict[str, Any] = {**trigger, "route": native, "route_status": reason}
            if native is not None:
                leg["delivery_ms"] = delivery_ms_for(native, instruments)
                start_s = trigger["close_t"] + ENTRY_DELAY
                end_s = trigger["close_t"] + SENSITIVITY_DELAY + (HOLD_MINUTES - 1) * MINUTE
                params = {
                    "category": "linear",
                    "symbol": native,
                    "interval": "1",
                    "start": str(start_s * 1000),
                    "end": str(end_s * 1000),
                    "limit": "200",
                }
                # Bounded retries on HTTP errors, 429/5xx and API errors (inside _get_json).
                # When they are exhausted the exception aborts the whole prepare, so the
                # inputs are never published with a transient gap.
                async with semaphore:
                    payload = await _get_json(client, BYBIT_KLINE_URL, params)
                rows = (payload.get("result") or {}).get("list") or []
                leg["bybit_bars"] = sorted(
                    ([str(v) for v in r[:6]] for r in rows), key=lambda r: int(r[0])
                )
            legs.append(leg)

        await asyncio.gather(*(one(t) for t in triggers))
    legs.sort(key=lambda x: (x["close_t"], x["symbol"]))
    return {
        "family_version": FAMILY_VERSION,
        "prepared_at": now.isoformat(),
        "code_revision": code_revision,
        "archive_window": manifest["window"],
        "archive_sha256": hashlib.sha256(
            json.dumps(archive_sha, sort_keys=True).encode()
        ).hexdigest(),
        "bybit_catalogue": raw_catalogue,
        "legs": legs,
    }


def leg_return(leg: dict[str, Any], delay: int) -> tuple[float | None, str]:
    """Gross % of one leg entered `delay` seconds after the MEXC bar closes; or a reason.
    Uses only the stored Bybit bars."""
    if leg["route"] is None:
        return None, leg["route_status"]
    rows = leg.get("bybit_bars")
    if rows is None:
        return None, "bybit_fetch_failed"
    bars = {int(r[0]): r for r in rows}
    entry_ms = (leg["close_t"] + delay) * 1000
    exit_ms = entry_ms + (HOLD_MINUTES - 1) * MINUTE * 1000
    delivery = int(leg.get("delivery_ms") or 0)
    if delivery and delivery <= exit_ms + MINUTE * 1000:
        return None, "delisted_during_hold"
    entry, exit_bar = bars.get(entry_ms), bars.get(exit_ms)
    if entry is None:
        return None, "missing_entry_bar"
    if exit_bar is None:
        return None, "missing_exit_bar"
    open_ = float(entry[1])
    if open_ <= 0 or not 1 / IDENTITY_BAND <= leg["mexc_close"] / open_ <= IDENTITY_BAND:
        return None, "price_level_mismatch"
    return (float(exit_bar[4]) / open_ - 1) * 100, "leg"


def _week(close_t: int) -> str:
    iso = datetime.fromtimestamp(close_t, UTC).isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def verdict(
    n: int,
    assets: int,
    week_share: float | None,
    unresolved: float,
    mean: float | None,
    ci_low: float | None,
) -> str:
    if mean is None:
        return "insufficient_data"
    if n >= FLOOR["min_legs"] and mean <= 0:
        return "fail"
    if ci_low is None:
        return "insufficient_data"
    if (
        n < FLOOR["min_legs"]
        or assets < FLOOR["min_assets"]
        or week_share is None
        or week_share > FLOOR["max_week_share"]
        or unresolved > MAX_UNRESOLVED_FRACTION
    ):
        return "insufficient_data"
    return "candidate" if mean > 0 and ci_low > 0 else "fail"


def compute_result(
    inputs: dict[str, Any], inputs_sha: str, claim: dict[str, Any]
) -> dict[str, Any]:
    """Pure: the verdict from the stored inputs."""
    statuses: Counter[str] = Counter()
    legs: list[tuple[dict[str, Any], float]] = []
    routed = 0
    for leg in inputs["legs"]:
        gross, status = leg_return(leg, ENTRY_DELAY)
        statuses[status] += 1
        if leg["route"] is not None:
            routed += 1
        if gross is not None:
            legs.append((leg, gross))
    net = [(leg["symbol"], g - COST_PRIMARY_PCT) for leg, g in legs]
    n = len(net)
    assets = len({s for s, _ in net})
    weeks = Counter(_week(leg["close_t"]) for leg, _ in legs)
    week_share = max(weeks.values()) / n if n else None
    unresolved = (routed - n) / routed if routed else 1.0
    mean = fmean(v for _, v in net) if net else None
    ci = None
    if assets >= 2:
        est = cluster_bootstrap_mean(
            tuple(ClusterObservation(s, v) for s, v in net),
            iterations=BOOTSTRAP_ITERATIONS,
            seed=derived_seed(BOOTSTRAP_SEED, HYPOTHESIS),
        ).estimate
        ci = [est.lower_bound, est.upper_bound]
    sensitivity = [
        g
        for g, _ in (leg_return(leg, SENSITIVITY_DELAY) for leg in inputs["legs"])
        if g is not None
    ]
    by_symbol: dict[str, list[float]] = defaultdict(list)
    for s, v in net:
        by_symbol[s].append(v)
    top = sorted(by_symbol.items(), key=lambda kv: -sum(kv[1]))[:5]
    return {
        "family_version": FAMILY_VERSION,
        "hypothesis": HYPOTHESIS,
        "contract_sha256": claim["contract_sha256"],
        "inputs_sha256": inputs_sha,
        "claimed_at": claim["claimed_at"],
        "reader_code_revision": claim["reader_code_revision"],
        "inputs_code_revision": inputs["code_revision"],
        "triggers": len(inputs["legs"]),
        "leg_status": dict(sorted(statuses.items())),
        "legs": n,
        "assets": assets,
        "max_week_share": week_share,
        "unresolved_fraction": unresolved,
        "gross_mean_pct": fmean(g for _, g in legs) if legs else None,
        "gross_median_pct": median(g for _, g in legs) if legs else None,
        "net_mean_pct_primary_cost": mean,
        "net_ci_primary_cost": ci,
        "net_mean_pct_reported_cost": (mean + COST_PRIMARY_PCT - COST_REPORTED_PCT)
        if mean is not None
        else None,
        "win_rate_net": fmean(v > 0 for _, v in net) if net else None,
        "sensitivity_delay_gross_mean_pct": fmean(sensitivity) if sensitivity else None,
        "top_symbols_by_net_sum": [[s, round(sum(v), 3), len(v)] for s, v in top],
        "mean_without_top_symbol": (
            fmean(v for s, v in net if s != top[0][0]) if len(by_symbol) > 1 else None
        ),
        "verdict": verdict(n, assets, week_share, unresolved, mean, ci[0] if ci else None),
    }


def read(stage_dir: Path, now: datetime, reader_code_revision: str, dirty: bool) -> dict[str, Any]:
    if now < READ_NOT_BEFORE:
        raise ValueError(f"the read opens at {READ_NOT_BEFORE.isoformat()}")
    complete_digest(stage_dir / INPUTS_NAME)
    inputs, inputs_sha = load_verified(stage_dir / INPUTS_NAME)
    if inputs.get("family_version") != FAMILY_VERSION:
        raise ValueError("inputs of another family")
    expected = {
        "family_version": FAMILY_VERSION,
        "stage": "september_recheck",
        "inputs_sha256": inputs_sha,
        "tested_family": [HYPOTHESIS],
        "discovery_sha256": None,
        "contract_sha256": contract_sha256(),
        "reader_code_revision": reader_code_revision,
        "reader_working_tree_dirty": dirty,
    }
    result_path = stage_dir / RESULT_NAME
    if result_path.exists():
        if _digest_path(result_path).exists():
            raise AlreadyReadError(f"{stage_dir} was already read")
        claim = take_claim(stage_dir, expected, now)
        payload = compute_result(inputs, inputs_sha, claim)
        if _body(payload) != result_path.read_bytes():
            raise ValueError("the stored result differs from its replay; refusing")
        _write_digest(result_path, _sha(result_path.read_bytes()))
        return payload
    claim = take_claim(stage_dir, expected, now)
    payload = compute_result(inputs, inputs_sha, claim)
    write_once(result_path, payload)
    return payload


def verified_revision(claimed: str | None) -> str:
    """The formal phases run only from a clean checkout of an explicit commit: the claimed
    revision must equal HEAD and the working tree must have no change."""
    import shutil
    import subprocess

    if not claimed:
        raise SystemExit("--code-revision is required (the checked-out commit)")
    git = shutil.which("git")
    if git is None:
        raise SystemExit("git is required to verify the revision")
    head = subprocess.run(  # noqa: S603 -- resolved git binary, fixed argv
        [git, "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    if claimed != head:
        raise SystemExit(f"--code-revision {claimed} is not HEAD {head}")
    dirty = subprocess.run(  # noqa: S603 -- resolved git binary, fixed argv
        [git, "status", "--porcelain"], capture_output=True, text=True, check=True
    ).stdout.strip()
    if dirty:
        raise SystemExit("the working tree is not clean; commit or stash first")
    return head


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--phase", choices=("prepare", "read"), required=True)
    parser.add_argument("--archive-dir", type=Path, help=".../mexc_klines/Min1")
    parser.add_argument("--stage-dir", type=Path, required=True)
    parser.add_argument("--code-revision", default=None, help="required: the checked-out commit")
    args = parser.parse_args(argv)
    revision = verified_revision(args.code_revision)
    now = datetime.now(UTC)
    inputs_path = args.stage_dir / INPUTS_NAME
    if args.phase == "prepare":
        if now < BLIND_END:
            raise SystemExit(f"prepare opens at {BLIND_END.isoformat()} (window not complete)")
        if inputs_path.exists():
            raise SystemExit(f"{inputs_path} exists: inputs are prepared once")
        inputs = asyncio.run(prepare_inputs(args.archive_dir, now, revision))
        args.stage_dir.mkdir(parents=True, exist_ok=True)
        digest = write_once(inputs_path, inputs)
        routed = sum(1 for leg in inputs["legs"] if leg["route"] is not None)
        sys.stdout.write(
            f"inputs sha256 {digest}; triggers {len(inputs['legs'])}, routed {routed}\n"
        )
        return
    payload = read(args.stage_dir, now, revision, dirty=False)
    sys.stdout.write(json.dumps(payload, indent=1, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
