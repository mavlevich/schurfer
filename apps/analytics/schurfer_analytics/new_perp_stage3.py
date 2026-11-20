"""I25 stage 3 (new-perp short, features level): power calibration, dispersion only.

Contract: docs/research/new-perp-stage3-power-calibration-v1.md and the frozen JSON next to
it (docs/research/evidence/new-perp-stage3-v1/contract.json). Protocol:
docs/research/new-perp-short-lab-protocol-v1.md, sections 2, 7.4, 8, 10 and 11.

What it settles. The stage 4 power statement needs the dispersion of a weekly return of a
young perpetual and the dependence between launches. Both are measured here from the
**pre-entry** days of the 228 primary V1 episodes of the discovery year, so that no return
that a trade could earn is read. Per episode ONE number is formed: the close-to-close log
return from the close at the hour grid point A (the first one at or after T0 + 1 day) to the
close at the hour grid point B (the last one at or before the entry minute E). Of every
response only the start times of the bars and those two closes are parsed. The log return is
scaled to seven days, and its dispersion and week-block dependence give the minimum
detectable effect. No mean, sign, quantile, minimum, maximum or per-launch value is ever
reported; per-launch values stay in the sealed run root outside Git.

This module never runs from tests against a network: the fetcher takes an injected
``httpx.Client``, and the tests give it a mock transport. It reuses the stage 1 fetcher
(pacing, attempt journal, byte caps, error-envelope handling, persisted stop) by import.
"""

from __future__ import annotations

import argparse
import gzip
import heapq
import json
import math
import random
import re
import sys
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import httpx

from . import bybit_presettlement_bounded_fetch as transport
from . import bybit_presettlement_economics as b3
from . import new_perp_stage1 as s
from . import new_perp_stage1_verdict as v
from .bybit_carry_feasibility import publish_once

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

VERSION: Final = "new_perp_stage3_v1"
STUDY_ID: Final = s.STUDY_ID
PROTOCOL: Final = s.PROTOCOL
DOC: Final = "docs/research/new-perp-stage3-power-calibration-v1.md"
CONTRACT_PATH: Final = "docs/research/evidence/new-perp-stage3-v1/contract.json"
MODULE_PATH: Final = "apps/analytics/schurfer_analytics/new_perp_stage3.py"
STAGE1_EVIDENCE: Final = "docs/research/evidence/new-perp-stage1-v3"
PUBLISHED: Final = tuple(dict.fromkeys((*s.PUBLISHED, DOC, CONTRACT_PATH, MODULE_PATH)))

# The sealed prerequisite: the completed discovery Stage 1 run and its two reviewed reports.
STAGE1_REPORT_SHA256: Final = "400f635ea29ac52dc5999caa866b152fa14cea1b82381ce91270012821a853ca"
STAGE1_MANIFEST_SHA256: Final = "759305ceaf5730ff0b7f23963a2c17292590ecb590d0336376cc139a0b5ca76d"
M2II_V3_REPORT_SHA256: Final = "38b569aa5dc713179ded3db70f7cdf788cea560db7c0c99c65d9332a879070b8"
STAGE1_INPUT_MAP_SHA256: Final = "cc20f7d5b06208a18772d3df4f0a3427e1975750aea6f94a17845e5f627b4c5e"
M2II_V3_MAP_SHA256: Final = "532d101483c2691c422e1ddce2187423dfce5d460c4de1b719d626b66645ad03"
COHORT_EPISODES: Final = 228
COHORT_WEEKS: Final = 48
BLIND_WINDOW_START: Final = "2026-09-29T00:00:00+00:00"

HOUR_MS: Final = s.HOUR_MS
DAY_MS: Final = s.DAY_MS
MINUTE_MS: Final = s.MINUTE_MS
MIB: Final = s.MIB

# The estimand (protocol 7.4, Amendment 7): hour grid points A and B inside [T0 + 1 d, E].
PROXY_FIRST_DAY: Final = 1
HOLD_HOURS: Final = 168  # the V1 hold, the scale of the planning variance (protocol: 7/6 of 6 d)
MIN_WINDOW_HOURS: Final = 143  # B - A for every T0: 143 or 144 hours
MAX_WINDOW_HOURS: Final = 144
KLINE_WIDTH: Final = 7  # start, open, high, low, close, volume, turnover
CLOSE_INDEX: Final = 4
PROXY_LIMIT: Final = 200  # a legitimate page holds at most 145 rows
CLOSE_TEXT: Final = re.compile(r"\d{1,20}(?:\.\d{1,20})?")

# The request list: one class, one go.
REQUESTS: Final = COHORT_EPISODES
RESPONSE_CAP_BYTES: Final = 64 * 1024
CLASS_BYTE_CAP: Final = 48 * MIB
GO_ATTEMPT_CAP: Final = REQUESTS * s.ATTEMPTS_PER_REQUEST
GO_BYTE_CAP: Final = 48 * MIB

# Statistics, fixed before any price.
DEFINED_MIN_PERCENT: Final = 95
MIN_EPISODES: Final = 100
MIN_WEEKS: Final = 26
BOOTSTRAP_DRAWS: Final = 10_000
BLOCK_LENGTHS: Final = (2, 4, 8)
PRIMARY_BLOCK: Final = 4
SEED: Final = 25_030_001
GATE_MULTIPLIER: Final = 2.80  # protocol 7.4: 1.96 + 0.84
SENSITIVITY_MULTIPLIERS: Final = (2.487, 2.123)  # reported, gate nothing
MDE_LIMIT_BPS: Final = 400.0
DEFF_FLOOR: Final = 1.0
SLOT_SHARE_PERCENT: Final = 90
HURDLE: Final = 0.05
WINDOW_DAYS: Final = 365
CAPITALS_USD: Final = (50, 250, 3000, 5000)
VARIANT_AGES: Final = {"V1": 7, "V2": 3, "V3": 14}

VERDICTS: Final = (
    "power_gate_passed",
    "insufficient_power",
    "incomplete_coverage",
    "insufficient_events",
    "not_estimable",
    "stage3_incomplete",
    "stage3_stopped",
)
STOP_CODE_OF: Final = {
    "insufficient_power": "insufficient_power",
    "incomplete_coverage": "incomplete_coverage",
    "insufficient_events": "insufficient_events",
}

P1: Final = s.ClassSpec(
    "P1",
    "proxy_hourly_trade",
    1,
    "GET",
    s.KLINE_URL,
    "category=linear, symbol, interval=60, start=A-1h, end=B-1h, limit=200",
    "per row: column 1 (the bar start time) and the row length; of the two rows that start at "
    "A-1h and B-1h only: column 5 (the close)",
    "open, high, low, volume, turnover of every row, and the close of every other row, "
    "unparsed; the two closes are reduced to their log ratio and are not kept",
    REQUESTS,
    REQUESTS,
    RESPONSE_CAP_BYTES,
    CLASS_BYTE_CAP,
    go=1,
    note="one page per primary V1 episode; a row outside the requested range stops the run",
)
SPECS: Final = {"P1": P1}

REPORT_KEYS: Final = {
    "version",
    "verdict",
    "stop_code",
    "returns_read",
    "cohort",
    "window",
    "dispersion",
    "dependence",
    "power",
    "variants",
    "portfolio",
    "reason",
    "run",
}
FORBIDDEN_NAMES: Final = (
    "mean",
    "median",
    "average",
    "min",
    "max",
    "quantile",
    "percentile",
    "skew",
    "kurtosis",
    "sign",
    "net",
    "outcome",
    "pnl",
    "profit",
    "close",
    "price",
    "symbol",
    "raw_moment",
    "sum",
)


class NotEstimableError(ValueError):
    """A calibration that cannot be computed, with a fixed reason code (never a value)."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"not estimable: {reason}")
        self.reason = reason


NOT_ESTIMABLE_REASONS: Final = (
    "zero_variance",
    "nonfinite_variance",
    "empty_draw",
    "block_longer_than_weeks",
    "nonfinite_bootstrap_variance",
)

# Small helpers.


def repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def ceil_hour(ms: int) -> int:
    return -(-ms // HOUR_MS) * HOUR_MS


def floor_hour(ms: int) -> int:
    return ms - ms % HOUR_MS


def week_ordinal(week: str) -> int:
    """Consecutive number of an ISO week (Mondays are 7 days apart)."""
    year, number = week.split("-W")
    return (date.fromisocalendar(int(year), int(number), 1).toordinal() - 1) // 7


# The estimand: windows and requests.


@dataclass(frozen=True)
class Episode:
    """One primary V1 episode. Held in the sealed cohort file, never published."""

    symbol: str
    week: str
    launch_ms: int
    t0_ms: int
    entry_ms: int
    exit_ms: int

    @property
    def grid_a_ms(self) -> int:
        """First hour grid point at or after T0 + 1 day: the start of the proxy window."""
        return ceil_hour(self.t0_ms + PROXY_FIRST_DAY * DAY_MS)

    @property
    def grid_b_ms(self) -> int:
        """Last hour grid point at or before E: the end of the proxy window."""
        return floor_hour(self.entry_ms)

    @property
    def hours(self) -> int:
        return (self.grid_b_ms - self.grid_a_ms) // HOUR_MS

    @property
    def first_start_ms(self) -> int:
        """Start of the bar whose close is the price at A."""
        return self.grid_a_ms - HOUR_MS

    @property
    def last_start_ms(self) -> int:
        """Start of the bar whose close is the price at B; it ends at B, never after E."""
        return self.grid_b_ms - HOUR_MS


def check_episode(ep: Episode) -> None:
    """Every guard of the cut and of the window, before any request."""
    entry, exit_ = s.entry_clock(ep.t0_ms)
    if (entry, exit_) != (ep.entry_ms, ep.exit_ms):
        raise ValueError("the entry clock differs from protocol 5.2")
    if ep.exit_ms > s.CUT_MS:
        raise ValueError("the planned exit is after the cut: window_censored, never priced")
    if ep.entry_ms - s.LOOKBACK_DAYS * DAY_MS < s.WINDOW_START_MS:
        raise ValueError("lookback_censored episode in the cohort")
    if not MIN_WINDOW_HOURS <= ep.hours <= MAX_WINDOW_HOURS:
        raise ValueError("the proxy window is not 143 or 144 hours")
    if ep.last_start_ms + HOUR_MS > ep.entry_ms:
        raise ValueError("the last bar would end after the entry minute")
    if ep.first_start_ms < ep.t0_ms + PROXY_FIRST_DAY * DAY_MS - HOUR_MS:
        raise ValueError("the first bar starts before T0 + 23 hours")
    if ep.grid_b_ms > s.CUT_MS - HOLD_HOURS * HOUR_MS:
        raise ValueError("the proxy window ends inside the last hold week before the cut")


def proxy_request(ep: Episode) -> s.Request:
    check_episode(ep)
    symbol = s._check_symbol(ep.symbol)
    params = s._kline_params(symbol, "60", ep.first_start_ms, ep.last_start_ms, PROXY_LIMIT)
    return s.Request("P1", "GET", s.KLINE_URL, params, symbol)


def _close(value: Any) -> float:
    if not isinstance(value, str) or CLOSE_TEXT.fullmatch(value) is None:
        raise s.SchemaMismatchError("bad:close")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise s.SchemaMismatchError("bad:close")
    return number


def reduce_proxy(payload: Any, symbol: str, first_start: int, last_start: int) -> dict[str, Any]:
    """The facts of one response: the log ratio of two closes and a row count.

    Every row is checked by its length and its start time alone. A row that starts outside
    [first_start, last_start] stops the run before any other field of it is read, so no bar
    at or after E is ever parsed. Only the rows that start at first_start and last_start have
    their close parsed.
    """
    result = s._envelope(payload)
    if result.get("symbol") != symbol:
        raise s.SchemaMismatchError("symbol")
    rows = result.get("list")
    if not isinstance(rows, list):
        raise s.SchemaMismatchError("list")
    if len(rows) >= PROXY_LIMIT:
        raise s.SchemaMismatchError("page_full")
    seen: dict[int, list[Any]] = {}
    for row in rows:
        if not isinstance(row, list) or len(row) < KLINE_WIDTH:
            raise s.SchemaMismatchError("row")
        try:
            start = s._int_ms(row[0])
        except (TypeError, ValueError) as exc:
            raise s.SchemaMismatchError("bad:start") from exc
        if not first_start <= start <= last_start or (start - first_start) % HOUR_MS:
            raise s.SchemaMismatchError("row_outside_range")
        if start in seen:
            raise s.SchemaMismatchError("duplicate_start")
        seen[start] = row
    first, last = seen.get(first_start), seen.get(last_start)
    log_return: float | None = None
    if first is not None and last is not None:
        log_return = math.log(_close(last[CLOSE_INDEX]) / _close(first[CLOSE_INDEX]))
    return {
        "log_return": log_return,
        "rows": len(seen),
        "expected_rows": (last_start - first_start) // HOUR_MS + 1,
    }


def proxy_reducer(ep: Episode) -> Callable[[bytes], dict[str, Any]]:
    def reduce(body: bytes) -> dict[str, Any]:
        try:
            payload = json.loads(body)
        except ValueError as exc:
            raise s.SchemaMismatchError("not_json") from exc
        return reduce_proxy(payload, ep.symbol, ep.first_start_ms, ep.last_start_ms)

    return reduce


# The cohort, from the sealed Stage 1 run.


def _stage1_rows(data: v.RunData) -> list[dict[str, Any]]:
    if not data.go2_done or data.stop is not None:
        raise ValueError("the Stage 1 prerequisite run is not complete")
    launches = s.in_window_launches(data.phase0)
    contradictions = s.catalog_contradictions(data.phase0)
    candidates = {
        c["symbol"]: c for c in s.entry_candidates(data.phase0, data.listings, data.firsts)
    }
    rows = [v._launch(data, row, contradictions, candidates) for row in launches]
    v._assign_reasons(data, rows, candidates, [])
    return rows


def episodes_from_rows(rows: Sequence[dict[str, Any]]) -> list[Episode]:
    out = [
        Episode(
            symbol=r["symbol"],
            week=r["week"],
            launch_ms=r["row"]["launch_ms"],
            t0_ms=r["t0_ms"],
            entry_ms=r["cuts"]["entry_ms"],
            exit_ms=r["cuts"]["exit_ms"],
        )
        for r in rows
        if r["reason"] == "primary"
    ]
    return sorted(out, key=lambda e: (e.launch_ms, e.symbol))


def reason_counts(rows: Sequence[dict[str, Any]]) -> dict[str, int]:
    counts = {name: 0 for name in v.REASONS}
    for r in rows:
        counts[r["reason"]] += 1
    return counts


def check_prerequisite(
    rows: Sequence[dict[str, Any]],
    episodes: Sequence[Episode],
    report: dict[str, Any],
    bound: tuple[int, int] | None = (COHORT_EPISODES, COHORT_WEEKS),
) -> None:
    """The cohort derived from the sealed root must equal the reviewed Stage 1 report.

    `bound` is the size the contract binds (228 episodes in 48 weeks); tests pass None for a
    synthetic world, whose consistency with its own report is still checked.
    """
    if (
        len(episodes) != report["n_primary"]
        or len({e.week for e in episodes}) != report["iso_weeks"]
        or reason_counts(rows) != report["reasons"]
        or report["stop_codes"]
        or report["stop"] is not None
    ):
        raise ValueError("the cohort differs from the reviewed Stage 1 report")
    if bound is not None and (len(episodes), report["iso_weeks"]) != bound:
        raise ValueError("the cohort is not the 228 episodes in 48 weeks this contract binds")
    for ep in episodes:
        check_episode(ep)


def load_reviewed_report(repo: Path | None = None) -> dict[str, Any]:
    """The committed Stage 1 report, verified against its pinned hash."""
    folder = (repo or repo_root()) / STAGE1_EVIDENCE
    raw = gzip.decompress((folder / "stage1-report.json.gz").read_bytes())
    manifest = gzip.decompress((folder / "manifest.json.gz").read_bytes())
    if s.digest(raw) != STAGE1_REPORT_SHA256 or s.digest(manifest) != STAGE1_MANIFEST_SHA256:
        raise ValueError("the committed Stage 1 evidence differs from its pinned hash")
    return json.loads(raw)  # type: ignore[no-any-return]


def derive_cohort(
    stage1_root: Path,
    report: dict[str, Any] | None = None,
    *,
    manifest_sha256: str = STAGE1_MANIFEST_SHA256,
    bound: tuple[int, int] | None = (COHORT_EPISODES, COHORT_WEEKS),
) -> list[Episode]:
    """Episodes of the sealed Stage 1 run. No request: it reads local receipts only."""
    if s.manifest_sha256(stage1_root) != manifest_sha256:
        raise ValueError("the Stage 1 manifest differs from the pinned one")
    data = v.load_run(stage1_root)
    rows = _stage1_rows(data)
    episodes = episodes_from_rows(rows)
    reviewed = report if report is not None else load_reviewed_report()
    check_prerequisite(rows, episodes, reviewed, bound)
    return episodes


def slots_needed(spans: Sequence[tuple[int, int]]) -> tuple[int, int]:
    """(k, entered): the smallest k such that at least 90% of the episodes find a free slot.

    Protocol 5.4, from entry and planned exit times alone: a slot is released at X and an
    exit at an instant is processed before an entry at the same instant. The count of
    entered episodes does not depend on the order of simultaneous entries.
    """
    count = len(spans)
    if count == 0:
        raise ValueError("no episode")
    for k in range(1, count + 1):
        open_exits: list[int] = []
        entered = 0
        for entry, exit_ in sorted(spans):
            while open_exits and open_exits[0] <= entry:
                heapq.heappop(open_exits)
            if len(open_exits) < k:
                heapq.heappush(open_exits, exit_)
                entered += 1
        if entered * 100 >= SLOT_SHARE_PERCENT * count:
            return k, entered
    raise AssertionError("unreachable: k = count enters every episode")


def source_fingerprint(stage1_root: Path) -> str:
    """SHA-256 over the Stage 1 files the cohort is actually derived from.

    The files are `phase0.json`, `manifest.json`, `go2_done.json` and every attempt and receipt;
    the map is path to file SHA-256, serialized with sorted keys and compact separators. A
    stop file in the root is refused. The map is not committed (it names sealed files), and no
    committed hash covers these files, so the value is recorded in the sealed cohort file,
    printed by the `cohort` command for the owner, and re-checked at report time.
    """
    if (stage1_root / "stop.json").exists():
        raise ValueError("the Stage 1 prerequisite run has a stop file")
    names = ["phase0.json", "manifest.json", "go2_done.json"]
    for kind in ("attempts", "receipts"):
        names += [f"{kind}/{p.name}" for p in sorted((stage1_root / kind).glob("*.json"))]
    entries = {}
    for name in names:
        path = stage1_root / name
        if not path.is_file():
            raise ValueError("a Stage 1 source file is missing")
        entries[name] = s.digest(path.read_bytes())
    return s.digest(json.dumps(entries, sort_keys=True, separators=(",", ":")).encode())


def cohort_summary(episodes: Sequence[Episode], fingerprint: str | None = None) -> dict[str, Any]:
    """Aggregates of a cohort for the owner's check before the go: no symbol, no date."""
    document = cohort_document(episodes, fingerprint)
    counts = {
        name: sum(1 for e in episodes if variant_eligible(e.t0_ms, age))
        for name, age in VARIANT_AGES.items()
    }
    return {
        "episodes": len(episodes),
        "iso_weeks": len({e.week for e in episodes}),
        "slots_k": document["slots"]["k"],
        "entered_at_k": document["slots"]["entered"],
        "variant_episodes_lower_bound": counts,
        "cohort_sha256": document["cohort_sha256"],
        "source_fingerprint": fingerprint,
    }


def cohort_document(episodes: Sequence[Episode], fingerprint: str | None = None) -> dict[str, Any]:
    rows = [asdict(e) for e in episodes]
    k, entered = slots_needed([(e.entry_ms, e.exit_ms) for e in episodes])
    return {
        "version": VERSION,
        "episodes": rows,
        "slots": {"k": k, "entered": entered, "episodes": len(rows)},
        "source_fingerprint": fingerprint,
        "cohort_sha256": s.digest(s.canonical(rows)),
    }


# Statistics.


def scaled_bps(log_return: float, hours: int) -> tuple[float, float]:
    """(7-day equivalent log return, its simple return in bps): z = L sqrt(168 / h)."""
    z = log_return * math.sqrt(HOLD_HOURS / hours)
    return z, 1e4 * math.expm1(z)


def sample_variance(values: Sequence[float]) -> float:
    n = len(values)
    if n < 2:
        raise ValueError("variance needs two values")
    mean = math.fsum(values) / n
    return math.fsum((x - mean) ** 2 for x in values) / (n - 1)


def top5_share(values: Sequence[float]) -> float:
    """Share of the sum of squared deviations from the five largest absolute deviations."""
    mean = math.fsum(values) / len(values)
    squares = sorted(((x - mean) ** 2 for x in values), reverse=True)
    return math.fsum(squares[:5]) / math.fsum(squares)


def block_bootstrap(
    weeks: Sequence[tuple[float, int]], block: int, draws: int, seed: int
) -> tuple[float, float]:
    """Variance across draws of the cluster mean and of the episode mean.

    `weeks` is the calendar sequence of ISO weeks (sum of values, episode count), empty weeks
    included. A draw concatenates ceil(m / block) blocks of `block` consecutive weeks whose
    start is uniform over 0 to m - block, truncated to m weeks. The cluster mean is the mean
    of the weekly means over the non-empty drawn weeks (clusters weigh equally, protocol
    7.2); the episode mean weighs episodes. The draws themselves are never returned.
    """
    m = len(weeks)
    if block > m:
        raise NotEstimableError("block_longer_than_weeks")
    rng = random.Random(seed * 100 + block)  # noqa: S311 - a fixed-seed resampler, not secrets
    blocks = -(-m // block)
    cluster: list[float] = []
    episode: list[float] = []
    for _ in range(draws):
        picked: list[int] = []
        for _ in range(blocks):
            start = rng.randrange(m - block + 1)
            picked.extend(range(start, start + block))
        picked = picked[:m]
        means = [weeks[j][0] / weeks[j][1] for j in picked if weeks[j][1]]
        count = sum(weeks[j][1] for j in picked)
        if not means:
            raise NotEstimableError("empty_draw")
        cluster.append(math.fsum(means) / len(means))
        episode.append(math.fsum(weeks[j][0] for j in picked) / count)
    return sample_variance(cluster), sample_variance(episode)


def week_sequence(items: Sequence[tuple[int, float]]) -> list[tuple[float, int]]:
    """(week ordinal, value) pairs to the contiguous calendar sequence of (sum, count)."""
    first = min(w for w, _ in items)
    last = max(w for w, _ in items)
    sums = [0.0] * (last - first + 1)
    counts = [0] * (last - first + 1)
    for w, value in items:
        sums[w - first] += value
        counts[w - first] += 1
    return list(zip(sums, counts, strict=True))


def calibrate(
    items: Sequence[tuple[int, float, float]], draws: int | None = None
) -> dict[str, Any]:
    """Dispersion, dependence and the power gate from (week ordinal, bps, log return).

    Returns the public numbers only. A zero or non-finite variance, an empty draw or a block
    longer than the week sequence raises NotEstimableError with a fixed reason code, which the
    caller reports as `not_estimable`.
    """
    n = len(items)
    draws = BOOTSTRAP_DRAWS if draws is None else draws
    bps = [b for _, b, _ in items]
    logs = [z for _, _, z in items]
    variance = sample_variance(bps)
    if not math.isfinite(variance):
        raise NotEstimableError("nonfinite_variance")
    if variance <= 0:
        raise NotEstimableError("zero_variance")
    weeks = week_sequence([(w, b) for w, b, _ in items])
    independent = variance / n
    blocks: dict[str, Any] = {}
    deffs: list[float] = []
    for block in BLOCK_LENGTHS:
        cluster_var, episode_var = block_bootstrap(weeks, block, draws, SEED)
        if not (math.isfinite(cluster_var) and math.isfinite(episode_var)):
            raise NotEstimableError("nonfinite_bootstrap_variance")
        blocks[str(block)] = {
            "cluster_weighted_variance": cluster_var,
            "deff_cluster_weighted": cluster_var / independent,
            "deff_episode_weighted": episode_var / independent,
        }
        deffs.append(cluster_var / independent)
    deff_max = max(deffs)
    deff_used = max(deff_max, DEFF_FLOOR)
    se = math.sqrt(deff_used * independent)
    return {
        "dispersion": {
            "sd_bps": math.sqrt(variance),
            "sd_log_7d": math.sqrt(sample_variance(logs)),
            "top5_share_of_squared_deviations": top5_share(bps),
        },
        "dependence": {
            "blocks": blocks,
            "primary_block": PRIMARY_BLOCK,
            "deff_largest": deff_max,
            "deff_used": deff_used,
            "deff_floor_applied": deff_max < DEFF_FLOOR,
            "n_eff": n / deff_used,
        },
        "power": {
            "se_bps": se,
            "multiplier": GATE_MULTIPLIER,
            "mde_gate_bps": GATE_MULTIPLIER * se,
            "limit_bps": MDE_LIMIT_BPS,
            "passes": GATE_MULTIPLIER * se <= MDE_LIMIT_BPS,
            "sensitivity_bps": {str(m): m * se for m in SENSITIVITY_MULTIPLIERS},
        },
    }


def variant_eligible(t0_ms: int, age_days: int) -> bool:
    """The cuts of one variant (protocol 2 and 5.1): path inside the window, lookback inside."""
    entry = -(-(t0_ms + age_days * DAY_MS) // MINUTE_MS) * MINUTE_MS
    return entry + 7 * DAY_MS <= s.CUT_MS and entry - s.LOOKBACK_DAYS * DAY_MS >= s.WINDOW_START_MS


def variant_table(defined: Sequence[Episode], mde_v1: float) -> dict[str, Any]:
    """MDE of V1, V2, V3 at one calibrated standard error scaled by the variant's own count.

    V2 and V3 are counted on the V1 primary episodes that also pass their own cuts, a lower
    bound of their stage 4 count; they decide nothing (protocol 5.1).
    """
    n1 = len(defined)
    out: dict[str, Any] = {}
    for name, age in VARIANT_AGES.items():
        members = [e for e in defined if variant_eligible(e.t0_ms, age)]
        n = len(members)
        weeks = len({e.week for e in members})
        out[name] = {
            "basis": "calibrated" if name == "V1" else "conditional_extrapolation_from_v1_se",
            "age_days": age,
            "episodes": n,
            "weeks": weeks,
            "floors_met": n >= MIN_EPISODES and weeks >= MIN_WEEKS,
            "mde_bps": mde_v1 * math.sqrt(n1 / n) if n else None,
        }
    return out


def portfolio_table(slots: dict[str, Any], mde_bps: float) -> dict[str, Any]:
    """The 5% hurdle on all reserved capital against the minimum detectable effect.

    roc = mean * entered / (10,000 k) over a year of 365 days (protocol 6.4), so the hurdle
    needs a mean of 500 k / entered bps and the MDE is the same arithmetic at its value.
    """
    k, entered = int(slots["k"]), int(slots["entered"])
    hurdle_required = HURDLE * 1e4 * k / entered
    roc_at_mde = mde_bps / 1e4 * entered / k
    return {
        "slots_k": k,
        "entered_at_k": entered,
        "window_days": WINDOW_DAYS,
        "hurdle": HURDLE,
        "hurdle_required_bps": hurdle_required,
        "mde_over_hurdle": mde_bps / hurdle_required,
        "roc_annual_at_mde": roc_at_mde,
        "usd_per_year": {
            str(c): {"hurdle": HURDLE * c, "at_mde": roc_at_mde * c} for c in CAPITALS_USD
        },
    }


def decide(cohort: int, defined: int, weeks: int, power_ok: bool | None) -> str:
    """The pre-registered decision table, first applicable row (protocol 8 order)."""
    if defined * 100 < DEFINED_MIN_PERCENT * cohort:
        return "incomplete_coverage"
    if defined < MIN_EPISODES or weeks < MIN_WEEKS:
        return "insufficient_events"
    if power_ok is None:
        return "not_estimable"
    return "power_gate_passed" if power_ok else "insufficient_power"


# Run state: the sealed root.


def _load(root: Path, kind: str) -> list[dict[str, Any]]:
    return [json.loads(p.read_text()) for p in sorted((root / kind).glob("*.json"))]


def results_by_symbol(root: Path) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    receipts, attempts = _load(root, "receipts"), _load(root, "attempts")
    final = s.final_from_receipts(receipts, attempts)
    return {r["symbol"]: r for r in final.values() if r["cls"] == "P1"}, receipts


def _facts(result: dict[str, Any]) -> float | None:
    """The log return of a finished request, or None when the proxy is undefined."""
    if result["outcome"] != "ok":
        return None
    value = result["facts"]["log_return"]
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ValueError("a stored log return is not a finite number")
    return float(value)


def evaluate(
    root: Path, *, code: dict[str, Any] | None = None, stage1_root: Path | None = None
) -> dict[str, Any]:
    """The public report of a root, from its sealed files. It makes no request.

    The root must have been run under this implementation's contract hash. When `code` (the
    current binding) is given it must equal the binding stored in the root, and when
    `stage1_root` is given its sources must still match the cohort's recorded fingerprint: a
    root is never re-analysed by other code or against changed inputs under its old hash.
    """
    cohort = json.loads((root / "cohort.json").read_text())
    if cohort["cohort_sha256"] != s.digest(s.canonical(cohort["episodes"])):
        raise ValueError("the cohort file differs from its own hash")
    stored = json.loads((root / "run.json").read_text())
    if stored["request_list_sha256"] != build_contract()["request_list_sha256"]:
        raise ValueError("the root was run under another contract hash than this implementation")
    if code is not None and stored["code"] != json.loads(json.dumps(code)):
        raise ValueError("the root's code binding differs from this implementation")
    if stage1_root is not None and source_fingerprint(stage1_root) != cohort["source_fingerprint"]:
        raise ValueError("the Stage 1 sources differ from the cohort's recorded fingerprint")
    episodes = [Episode(**row) for row in cohort["episodes"]]
    results, receipts = results_by_symbol(root)
    stop_path = root / "stop.json"
    stop = json.loads(stop_path.read_text()) if stop_path.exists() else None
    count, used, _, _ = s.ledger_totals(_load(root, "attempts"), receipts, None, SPECS)
    bound = stored["code"]
    run = {
        "request_list_sha256": stored["request_list_sha256"],
        "binding": {
            "rules": bound.get("rules"),
            "revision": bound.get("revision"),
            "publication": bound.get("publication"),
            "clean_tree_at_run": bool(bound.get("revision")),
        },
        "cohort_sha256": cohort["cohort_sha256"],
        "source_fingerprint": cohort["source_fingerprint"],
        "attempts": count,
        "bytes": used,
        "requests_finished": len(results),
        "request_results": _outcome_counts(results),
        "api_error_codes": v._count_codes(receipts),
    }
    n_cohort = len(episodes)
    base: dict[str, Any] = {
        "version": VERSION,
        "returns_read": False,
        "cohort": {
            "episodes": n_cohort,
            "iso_weeks": len({e.week for e in episodes}),
            "slots_k": cohort["slots"]["k"],
            "entered_at_k": cohort["slots"]["entered"],
        },
        "run": run,
    }
    done = (root / "done.json").exists()
    if stop is not None or reserve_violation(root):
        return {**base, "verdict": "stage3_stopped", "stop_code": None}
    if not done or len(results) != n_cohort:
        return {**base, "verdict": "stage3_incomplete", "stop_code": None}
    items: list[tuple[Episode, float | None]] = [(e, _facts(results[e.symbol])) for e in episodes]
    defined = [(e, z) for e, z in items if z is not None]
    weeks = len({e.week for e, _ in defined})
    base["cohort"]["defined"] = len(defined)
    base["cohort"]["undefined"] = n_cohort - len(defined)
    base["window"] = _window_facts(items, results)
    estimable = len(defined) * 100 >= DEFINED_MIN_PERCENT * n_cohort
    estimable = estimable and len(defined) >= MIN_EPISODES and weeks >= MIN_WEEKS
    numbers: dict[str, Any] | None = None
    reason: str | None = None
    if estimable:
        prepared: list[tuple[int, float, float]] = []
        for e, log_return in defined:
            scaled, bps = scaled_bps(log_return, e.hours)
            prepared.append((week_ordinal(e.week), bps, scaled))
        try:
            numbers = calibrate(prepared)
        except NotEstimableError as exc:
            reason = exc.reason
    verdict = decide(
        n_cohort, len(defined), weeks, None if numbers is None else numbers["power"]["passes"]
    )
    out = {**base, "verdict": verdict, "stop_code": STOP_CODE_OF.get(verdict)}
    out["cohort"]["weeks_with_defined"] = weeks
    if verdict == "not_estimable":
        out["reason"] = reason
    if numbers is not None:
        mde = numbers["power"]["mde_gate_bps"]
        out.update(numbers)
        out["variants"] = variant_table([e for e, _ in defined], mde)
        out["portfolio"] = portfolio_table(cohort["slots"], mde)
    return out


def report_once(
    root: Path, *, code: dict[str, Any] | None = None, stage1_root: Path | None = None
) -> dict[str, Any]:
    """The immutable report of a root: computed once under the verified binding, then kept.

    A root that is still running (`stage3_incomplete`) gets no stored report. A later call, by
    any version of the code, returns the stored bytes; a new analysis of the same root is a
    separately registered version with its own contract, hash and root.
    """
    path = root / "report.json"
    if path.exists():
        return json.loads(path.read_text())  # type: ignore[no-any-return]
    report = evaluate(root, code=code, stage1_root=stage1_root)
    if report["verdict"] != "stage3_incomplete":
        publish_once(path, report)
    return report


def _outcome_counts(results: dict[str, dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in results.values():
        counts[r["outcome"]] = counts.get(r["outcome"], 0) + 1
    return dict(sorted(counts.items()))


def _window_facts(
    items: Sequence[tuple[Episode, float | None]], results: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    hours = {str(h): 0 for h in range(MIN_WINDOW_HOURS, MAX_WINDOW_HOURS + 1)}
    expected = present = 0
    for e, _ in items:
        hours[str(e.hours)] += 1
        facts = results[e.symbol].get("facts")
        if facts:
            expected += int(facts["expected_rows"])
            present += int(facts["rows"])
    return {
        "hours_between_grid_points": hours,
        "hour_rows_missing_share": None if expected == 0 else (expected - present) / expected,
    }


# The contract.


def budget() -> dict[str, Any]:
    """Counts, caps, attempts and time. Every number of the document comes from here."""
    sends = GO_ATTEMPT_CAP
    return {
        "requests": REQUESTS,
        "expected_attempts": REQUESTS,
        "attempts_per_request": s.ATTEMPTS_PER_REQUEST,
        "attempt_ceiling": GO_ATTEMPT_CAP,
        "response_cap_bytes": RESPONSE_CAP_BYTES,
        "class_byte_cap": CLASS_BYTE_CAP,
        "go_byte_cap": GO_BYTE_CAP,
        "worst_case_response_bytes": sends * RESPONSE_CAP_BYTES,
        "read_reserve_bytes": s.READ_RESERVE_BYTES,
        "expected_bytes_note": "about 3 MB: Stage 1 D5 averaged 34.6 KB for up to 433 rows",
        "seconds_expected": int(REQUESTS * s.SPACING_S),
        "seconds_all_retries": int(
            sends * s.SPACING_S + REQUESTS * (s.BACKOFF_S[0] + s.BACKOFF_S[1])
        ),
        "seconds_note": "at one request per second; a Retry-After of up to 300 s replaces "
        "the backoff of that request",
    }


def thresholds() -> dict[str, Any]:
    return {
        "defined_min_percent": DEFINED_MIN_PERCENT,
        "min_episodes": MIN_EPISODES,
        "min_weeks": MIN_WEEKS,
        "bootstrap_draws": BOOTSTRAP_DRAWS,
        "block_lengths": list(BLOCK_LENGTHS),
        "primary_block": PRIMARY_BLOCK,
        "seed": SEED,
        "gate_multiplier": GATE_MULTIPLIER,
        "sensitivity_multipliers": list(SENSITIVITY_MULTIPLIERS),
        "mde_limit_bps": MDE_LIMIT_BPS,
        "deff_floor": DEFF_FLOOR,
        "slot_share_percent": SLOT_SHARE_PERCENT,
        "hurdle": HURDLE,
        "window_days": WINDOW_DAYS,
        "hold_hours": HOLD_HOURS,
        "window_hours": [MIN_WINDOW_HOURS, MAX_WINDOW_HOURS],
        "proxy_limit": PROXY_LIMIT,
        "capitals_usd": list(CAPITALS_USD),
        "variant_ages_days": VARIANT_AGES,
    }


DECISION_TABLE: Final = (
    {
        "order": 1,
        "verdict": "incomplete_coverage",
        "condition": "defined proxies below 95% of the 228 episodes (defined * 100 < 95 * 228)",
        "consequence": "stop code incomplete_coverage; no statistic emitted; no return read",
    },
    {
        "order": 2,
        "verdict": "insufficient_events",
        "condition": "fewer than 100 defined episodes, or fewer than 26 ISO weeks with one",
        "consequence": "stop code insufficient_events; no statistic emitted; no return read",
    },
    {
        "order": 3,
        "verdict": "not_estimable",
        "condition": "zero or non-finite variance, a draw without an episode, or a block "
        "longer than the week sequence",
        "consequence": "no MDE statement; stage 4 closed under this contract; no return read",
    },
    {
        "order": 4,
        "verdict": "insufficient_power",
        "condition": "gate MDE = 2.80 * SE with DEFF = max(largest of blocks 2, 4, 8; 1.0) "
        "is above 400 bps (unrounded)",
        "consequence": "stop code insufficient_power; I25 stops; no return is read",
    },
    {
        "order": 5,
        "verdict": "power_gate_passed",
        "condition": "gate MDE at or below 400 bps",
        "consequence": "stage 4 may be proposed: its own contract, request list, hash and "
        "registry row, then the owner's separate go; nothing about an effect is implied",
    },
)


def build_contract() -> dict[str, Any]:
    """The frozen contract: one go, one class. `go_sha256` is what the owner approves."""
    shared: dict[str, Any] = {
        "rules": VERSION,
        "study": STUDY_ID,
        "protocol": PROTOCOL,
        "document": DOC,
        "level": "features",
        "windows": {
            "discovery_start": "2024-10-01T00:00:00+00:00",
            "cut": "2025-10-01T00:00:00+00:00",
            "blind_window_start": BLIND_WINDOW_START,
            "backward_holdout": "not touched",
            "note": "every request ends before E of its episode, and E + 7 d is at or before "
            "the cut; nothing dated from the cut or from the blind window is requested",
        },
        "prerequisites": {
            "verified_by_code": {
                "stage1_report_sha256": STAGE1_REPORT_SHA256,
                "stage1_manifest_sha256": STAGE1_MANIFEST_SHA256,
                "source_fingerprint": "SHA-256 of the sorted map path to file SHA-256 over "
                "phase0.json, manifest.json, go2_done.json and every attempt and receipt of the "
                "sealed Stage 1 root, no stop file; computed before the cohort is derived and "
                "again after, recorded in cohort.json, printed by the cohort command, matched "
                "to the owner's expected cohort SHA-256 before the first request, and re-checked "
                "against the root at report time",
            },
            "documentary_only": {
                "m2ii_v3_report_sha256": M2II_V3_REPORT_SHA256,
                "m2ii_v3_evidence_map_sha256": M2II_V3_MAP_SHA256,
                "stage1_input_hash_map_sha256": STAGE1_INPUT_MAP_SHA256,
                "note": "recorded for provenance; the code does not read the sealed files these "
                "hashes cover, so they are not checked here",
            },
            "cohort": {
                "episodes": COHORT_EPISODES,
                "iso_weeks": COHORT_WEEKS,
                "source": "primary V1 episodes of the sealed Stage 1 run, derived locally "
                "and checked against the reviewed report before any request",
            },
        },
        "estimand": {
            "window_start": "A = ceil_hour(T0 + 1 d)",
            "window_end": "B = floor_hour(E), E = ceil_minute(T0 + 7 d)",
            "window_hours": "h = (B - A) / 1 h, always 143 or 144",
            "closes": "C(t) is the close of the 1 h trade bar that starts at t - 1 h",
            "log_return": "L = ln(C(B) / C(A))",
            "scaled_log_return": "Z = L * sqrt(168 / h)",
            "planning_variable_bps": "Y = 10,000 * (exp(Z) - 1)",
            "never_read": "any bar that starts after B - 1 h, any bar at or after E, "
            "the entry bar, any exit, any bar from the cut",
        },
        "request": {
            "class": "P1",
            "endpoint": s.KLINE_URL,
            "params": "category=linear, symbol, interval=60, start=A-1h, end=B-1h, limit=200",
            "per_episode": 1,
            "row_rule": "a row starting outside [A-1h, B-1h], a duplicate start, a page of "
            "200 rows, a wrong symbol or a bad close stops the run as a schema mismatch",
            "undefined_proxy": "the bar at A-1h or at B-1h is absent; no imputation",
        },
        "limits": {
            "attempts_per_request": s.ATTEMPTS_PER_REQUEST,
            "spacing_s": s.SPACING_S,
            "max_spacing_s": s.MAX_SPACING_S,
            "backoff_s": list(s.BACKOFF_S),
            "max_retry_after_s": s.MAX_RETRY_AFTER_S,
            "read_reserve_bytes": s.READ_RESERVE_BYTES,
            "rate_limit_ret_codes": sorted(s.RET_RATE_LIMIT),
            "transient_ret_codes": sorted(s.RET_TRANSIENT),
            "api_error_rule": "an HTTP 200 envelope with retCode other than 0 records only its "
            "retCode and retMsg (200 characters at most); 10006 and 10018 are rate limits "
            "(retried, the spacing doubles), 10000 and 10016 are transient (retried), any "
            "other code stops the run naming it; retCode must be an integer, never a boolean "
            "or a float; a wrong envelope shape stops the run as a schema mismatch",
            "persisted_stop": "stop.json ends the root; nothing continues in it",
            "restart": "a fresh root per contract; a resumed root must carry the same binding "
            "(contract hash, prior usage, code revision and file hashes), else it is refused",
        },
        "thresholds": thresholds(),
        "decision_table": list(DECISION_TABLE),
        "stop_codes": list(STOP_CODE_OF.values()),
        "output_allowlist": sorted(REPORT_KEYS),
        "forbidden_output_names": list(FORBIDDEN_NAMES),
        "report": {
            "binding": "the report command refuses a root whose stored contract hash or code "
            "binding (revision, publication hashes) differs from the executing implementation, "
            "or whose Stage 1 sources differ from the recorded fingerprint",
            "immutable": "the first report of a finished or stopped root is stored once in the "
            "root and returned unchanged afterwards; a new analysis is a separately registered "
            "version with its own contract hash and root",
            "exported_binding": "revision, publication hashes, rules version and whether the "
            "tree was clean at the run",
            "not_estimable_reasons": list(NOT_ESTIMABLE_REASONS),
        },
        "retention": {
            "sealed_root": "0700 outside Git: attempts, receipts (the log return of each "
            "episode is the only price-derived value), cohort.json, run.json, stop.json",
            "public": "the allowlisted aggregate report only: no symbol, date, close, "
            "per-launch value, mean, sign, quantile, minimum or maximum",
        },
        "budget": budget(),
    }
    classes = [asdict(P1)]
    limits = {"1": {"max_attempts": GO_ATTEMPT_CAP, "max_bytes": GO_BYTE_CAP}}
    body: dict[str, Any] = {**shared, "go_limits": limits, "classes": classes}
    body["go_sha256"] = {"1": go_hash(body)}
    body["request_list_sha256"] = s.contract_hash(body)
    return body


def go_hash(contract: dict[str, Any]) -> str:
    """What the owner approves: every shared rule, the class and the caps of the one go."""
    shared = {
        k: contract[k]
        for k in contract
        if k not in ("go_limits", "classes", "go_sha256", "request_list_sha256")
    }
    return s.digest(
        s.canonical(
            {
                "shared": shared,
                "go": 1,
                "classes": contract["classes"],
                "limits": contract["go_limits"]["1"],
            }
        )
    )


# The run.


def binding() -> dict[str, Any]:
    published = b3.publication(list(PUBLISHED))
    if f"`{STUDY_ID}`" not in (repo_root() / s.REGISTRY).read_text():
        raise ValueError("no seen-windows registry row names this study")
    saved = json.loads((repo_root() / CONTRACT_PATH).read_text())
    if saved != build_contract():
        raise ValueError("the checked-in contract differs from this code")
    return {
        "rules": VERSION,
        "revision": transport.clean_revision(),
        "publication": published,
        "request_list_sha256": saved["request_list_sha256"],
    }


def verified_cohort(
    stage1_root: Path, expected_cohort_sha256: str | None = None, **derive: Any
) -> tuple[list[Episode], str]:
    """The cohort and the fingerprint of the sources it came from, checked twice.

    The fingerprint is taken before and after the derivation and must not change; when the
    owner's expected cohort SHA-256 is given it must equal the derived one.
    """
    before = source_fingerprint(stage1_root)
    episodes = derive_cohort(stage1_root, **derive)
    for ep in episodes:
        check_episode(ep)
    if source_fingerprint(stage1_root) != before:
        raise ValueError("the Stage 1 sources changed while the cohort was derived")
    if (
        expected_cohort_sha256 is not None
        and cohort_document(episodes)["cohort_sha256"] != expected_cohort_sha256
    ):
        raise ValueError("the derived cohort is not the one the owner approved")
    return episodes, before


def prepare_root(root: Path) -> None:
    """A fresh sealed root, or a root that already carries a run binding (a resume).

    The root holds per-launch values, so it must lie outside the repository.
    """
    if root.resolve().is_relative_to(repo_root()):
        raise ValueError("the sealed root must be outside the repository")
    if root.exists() and any(root.iterdir()) and not (root / "run.json").exists():
        raise ValueError("a root with files and no run binding is refused")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)


def reserve_violation(root: Path) -> bool:
    """A receipt with a transport chunk over the read reserve closes the root for good."""
    return any(r.get("reserve_exceeded") for r in _load(root, "receipts"))


def run_stage3(
    fetcher: s.Fetcher, episodes: Sequence[Episode], fingerprint: str | None = None
) -> dict[str, Any]:
    """One request per episode, in launch order. The cohort is written before the first one."""
    if len(episodes) != REQUESTS:
        raise ValueError("the cohort is not the size the contract binds")
    requests = [proxy_request(e) for e in episodes]  # every guard, before any request
    if reserve_violation(fetcher.root):
        fetcher.record_stop("this root holds a transport-limit violation")
        raise s.FetchStoppedError("this run was stopped: a transport limit was violated")
    document = cohort_document(episodes, fingerprint)
    path = fetcher.root / "cohort.json"
    if path.exists():
        if json.loads(path.read_text()) != json.loads(json.dumps(document)):
            raise ValueError("the root holds a different cohort")
    else:
        publish_once(path, document)
    for ep, request in zip(episodes, requests, strict=True):
        fetcher.run_request(request, proxy_reducer(ep), keep_facts=True)
    if not (fetcher.root / "done.json").exists():
        publish_once(fetcher.root / "done.json", {"requests": len(requests)})
    return fetcher.summary()


# Command line.


def _emit(text: str) -> None:
    sys.stdout.write(text + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("plan", help="print the contract summary and its hashes; no network")
    write = sub.add_parser("write-contract", help="write or check the frozen contract JSON")
    write.add_argument("--check", action="store_true")
    cohort = sub.add_parser(
        "cohort",
        help="derive the cohort from the sealed Stage 1 root and print aggregates; no network",
    )
    cohort.add_argument("--stage1-root", type=Path, required=True)
    fetch = sub.add_parser("fetch", help="the one go; needs the owner's go for the exact hash")
    fetch.add_argument("--approved-sha256", required=True, help="the go_sha256 the owner approved")
    fetch.add_argument("--stage1-root", type=Path, required=True)
    fetch.add_argument(
        "--expected-cohort-sha256", required=True, help="the cohort SHA-256 the owner approved"
    )
    fetch.add_argument("--root", type=Path, required=True)
    report = sub.add_parser("report", help="the public report of a finished root; no network")
    report.add_argument("--root", type=Path, required=True)
    report.add_argument("--stage1-root", type=Path, required=True)
    args = parser.parse_args(argv)
    contract = build_contract()
    if args.command == "plan":
        _emit(
            json.dumps(
                {
                    "rules": VERSION,
                    "request_list_sha256": contract["request_list_sha256"],
                    "go_sha256": contract["go_sha256"],
                    "go_limits": contract["go_limits"],
                    "budget": contract["budget"],
                },
                indent=2,
            )
        )
        return 0
    if args.command == "write-contract":
        path = repo_root() / CONTRACT_PATH
        if args.check:
            # Prettier reformats the file, so the check compares the parsed contract.
            return 0 if path.exists() and json.loads(path.read_text()) == contract else 1
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(s.canonical(contract))
        return 0
    if args.command == "cohort":
        episodes, fingerprint = verified_cohort(args.stage1_root)
        _emit(json.dumps(cohort_summary(episodes, fingerprint), indent=2, sort_keys=True))
        return 0
    if args.command == "report":
        done = report_once(args.root, code=binding(), stage1_root=args.stage1_root)
        _emit(json.dumps(done, indent=2, sort_keys=True))
        return 0
    if args.approved_sha256 != contract["go_sha256"]["1"]:
        raise ValueError("the approved hash is not this contract's go hash")
    bound = binding()
    episodes, fingerprint = verified_cohort(args.stage1_root, args.expected_cohort_sha256)
    prepare_root(args.root)
    with httpx.Client(timeout=60.0, follow_redirects=False) as client:
        fetcher = s.Fetcher(
            args.root, contract, client, go=1, code=bound, prior={}, specs=dict(SPECS)
        )
        _emit(json.dumps(run_stage3(fetcher, episodes, fingerprint), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
