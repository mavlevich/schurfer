"""Outcome-blind counts / calibration scan for the abnormal-flow screen.

STRICTLY OUTCOME-BLIND. This reads only frozen cold-bar inputs and a point-in-time
identity snapshot, and emits coverage, rejection reasons, feature distributions, and
counts (fires, independent episodes, assets, weeks). It reads NO forward price, PnL,
or verdict, and it never selects a window by "prettiest signal count": the window is
one continuous run from a fixed start to the last fully-verified day, chosen by data
quality alone. A gap is recorded and the run stops rather than silently narrowing.

Memory: bars are streamed one instrument at a time (see
``abnormal_flow_replay.iter_instrument_bars``), so a full month never materializes as
one list. The artifact lands under ``docs/research/evidence/abnormal-flow-v1/<run-id>/``
(README + manifest + JSON); raw Parquet is never written to the repo.
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .abnormal_flow_input_audit import AUDIT_VERSION, audit_directory, verified_input
from .abnormal_flow_replay import (
    _decision_eligible,
    ablation_cell_fires,
    form_episodes,
    is_eligible,
    iter_instrument_bars,
    oi_freshness_limit_for,
    oi_notional_usd,
    participation_frac,
    primary_cell_fires,
)
from .abnormal_flow_screen import AbnormalFlowContract

if TYPE_CHECKING:
    from collections.abc import Callable

    from .abnormal_flow_replay import DecisionFeatures, MinuteBar

SCAN_VERSION = "abnormal_flow_scan_v1"
DEFAULT_START = date(2026, 8, 14)  # first fidelity-provable day; earlier = unverifiable_legacy
DEFAULT_EVIDENCE_ROOT = Path("docs/research/evidence/abnormal-flow-v1")


# --- Feature distributions (fixed bins; outcome-blind) -----------------------------


@dataclass
class Histogram:
    """A fixed-edge histogram with under/overflow, so distributions are comparable
    across runs and no bin is chosen after seeing the data."""

    edges: tuple[float, ...]
    counts: list[int] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.counts:
            self.counts = [0] * (len(self.edges) + 1)

    def add(self, value: float) -> None:
        self.counts[bisect.bisect_right(self.edges, value)] += 1

    def as_json(self) -> dict[str, Any]:
        return {"edges": list(self.edges), "counts": list(self.counts)}

    def quantile(self, p: float) -> float | None:
        """Approximate p-quantile by linear interpolation within the containing bin.
        ``None`` when empty. Under/overflow bins interpolate to the nearest edge, so a
        quantile falling outside the declared edges returns that boundary edge (coarse
        by construction; the edges are pre-declared, never fit to the data)."""
        total = sum(self.counts)
        if total == 0:
            return None
        target = p * total
        cumulative = 0
        for i, count in enumerate(self.counts):
            if cumulative + count >= target and count > 0:
                lo = self.edges[i - 1] if i > 0 else self.edges[0]
                hi = self.edges[i] if i < len(self.edges) else self.edges[-1]
                frac = (target - cumulative) / count
                return lo + (hi - lo) * frac
            cumulative += count
        return self.edges[-1]


def _new_histograms() -> dict[str, Histogram]:
    return {
        "oi_growth_pct": Histogram((-50, -20, -10, -5, 0, 5, 10, 20, 50, 100, 200)),
        "buy_pressure": Histogram((0.5, 0.55, 0.6, 0.65, 0.7, 0.8, 0.9, 0.95)),
        "containment": Histogram((0.005, 0.01, 0.02, 0.03, 0.05, 0.1, 0.2, 0.5)),
        "participation_frac": Histogram((0.0005, 0.001, 0.005, 0.01, 0.05, 0.1, 0.25, 0.5)),
        "oi_notional_usd": Histogram((1e4, 5e4, 1e5, 5e5, 1e6, 5e6, 1e7, 5e7, 1e8)),
    }


# --- Inventory (read-only; window selection by data quality) -----------------------


@dataclass(frozen=True)
class DayInventory:
    day: str
    file_name: str
    sha256: str
    source_fingerprint: str
    fidelity_verified: bool
    borg_archive: str | None
    archive_verified: bool
    status: str


def _load_provenance(provenance_dir: Path | None, day: date) -> dict[str, Any]:
    if provenance_dir is None:
        return {}
    path = provenance_dir / f"bars-{day.isoformat()}.provenance.json"
    if not path.exists():
        return {}
    parsed = json.loads(path.read_text())
    return parsed if isinstance(parsed, dict) else {}


def borg_member_sha256(borg_repo: str, archive: str, member_path: str) -> str | None:
    """SHA-256 of one member as stored in a Borg archive (``borg extract --stdout``).
    ``None`` when Borg is unavailable. Used to prove the offsite copy really matches the
    manifest, rather than trusting a provenance field."""
    borg = shutil.which("borg")
    if borg is None:  # pragma: no cover - borg absence is environment-specific
        return None
    proc = subprocess.run(  # noqa: S603 -- fixed argv, resolved executable, no shell, no input
        [borg, "extract", "--stdout", f"{borg_repo}::{archive}", member_path],
        capture_output=True,
        check=True,
    )
    return hashlib.sha256(proc.stdout).hexdigest()


def make_borg_verifier(borg_repo: str, provenance_dir: Path | None) -> Callable[..., Any]:
    """Real archive verifier: recompute the offsite member's SHA-256 from Borg and
    require it to equal the manifest sha AND proven source fidelity. The provenance JSON
    only supplies the archive NAME as a hint; the proof is the recomputed SHA, never a
    ``receipt`` boolean."""

    def verify(day: date, manifest: Any) -> tuple[bool, str | None]:
        prov = _load_provenance(provenance_dir, day)
        archive = prov.get("borg_archive")
        member = prov.get("archive_member_path") or f"runtime/cold-bars/{manifest.file_name}"
        if not archive:
            return False, None
        sha = borg_member_sha256(borg_repo, str(archive), str(member))
        if sha is None:
            return False, str(archive)
        return (sha == manifest.sha256 and manifest.fidelity_verified is True), str(archive)

    return verify


def inventory_window(
    cold_bars_dir: Path,
    *,
    start: date,
    end: date,
    verify_archive: Callable[..., Any],
) -> tuple[list[DayInventory], str | None, date | None]:
    """Walk EVERY day in the explicit ``[start, end]`` window; for each, verify the
    manifest/file (bytes + sha via ``verified_input``), proven source fidelity, and the
    actual Borg archive member SHA via ``verify_archive``. Returns (inventory,
    gap_reason, chosen_end). Because the window end is explicit, reaching it cleanly is
    success (``chosen_end == end``), never a spurious gap. The FIRST day inside the
    window that is missing or not fully verified records a gap and stops -- the sample is
    never silently narrowed around a hole."""
    inventory: list[DayInventory] = []
    chosen_end: date | None = None
    gap: str | None = None
    day = start
    while day <= end:
        try:
            _path, manifest = verified_input(cold_bars_dir, day)
        except (FileNotFoundError, ValueError) as exc:
            gap = f"{day.isoformat()}: no verified cold copy ({exc})"
            break
        archive_verified, borg_archive = verify_archive(day, manifest)
        fidelity = manifest.fidelity_verified is True
        inventory.append(
            DayInventory(
                day=day.isoformat(),
                file_name=manifest.file_name,
                sha256=manifest.sha256,
                source_fingerprint=manifest.source_fingerprint or "",
                fidelity_verified=fidelity,
                borg_archive=borg_archive,
                archive_verified=bool(archive_verified),
                status="fully_verified" if (fidelity and archive_verified) else "not_verified",
            )
        )
        if fidelity and archive_verified:
            chosen_end = day
        else:
            gap = (
                f"{day.isoformat()}: not fully verified "
                f"(fidelity={fidelity}, archive_verified={bool(archive_verified)})"
            )
            break
        day += timedelta(days=1)
    return inventory, gap, chosen_end


# --- Point-in-time identity resolver from a snapshot -------------------------------


def load_identity_resolver(path: Path) -> tuple[Callable[..., str | None], str]:
    """Load a point-in-time identity snapshot and return (resolver, sha256). Each record
    is ``{exchange, market_type, native_market_id, valid_from, valid_to, canonical_asset}``
    (UTC ISO instants, ``valid_to`` exclusive; ``null`` means open-ended). The resolver
    returns the canonical asset live at the decision instant, or ``None`` if none covers
    it -- the current catalog is never used retroactively."""
    raw = path.read_bytes()
    records = json.loads(raw)
    index: dict[tuple[str, str, str], list[tuple[datetime, datetime | None, str]]] = {}
    for rec in records:
        key = (rec["exchange"], rec["market_type"], rec["native_market_id"])
        valid_from = datetime.fromisoformat(str(rec["valid_from"]).replace("Z", "+00:00"))
        valid_to_raw = rec.get("valid_to")
        valid_to = (
            datetime.fromisoformat(str(valid_to_raw).replace("Z", "+00:00"))
            if valid_to_raw
            else None
        )
        index.setdefault(key, []).append((valid_from, valid_to, str(rec["canonical_asset"])))
    for spans in index.values():
        spans.sort(key=lambda s: s[0])

    def resolve(
        exchange: str, market_type: str, native_market_id: str, _capture_version: str, at: datetime
    ) -> str | None:
        for valid_from, valid_to, canonical in index.get(
            (exchange, market_type, native_market_id), []
        ):
            if at >= valid_from and (valid_to is None or at < valid_to):
                return canonical
        return None

    return resolve, "sha256:" + hashlib.sha256(raw).hexdigest()


# --- Accumulated counts ------------------------------------------------------------


@dataclass
class ScanCounts:
    scanned: int = 0
    available: int = 0
    unavailable: int = 0
    ineligible: int = 0
    eligible: int = 0
    primary_fires: int = 0
    ablation_fires: int = 0
    rejection_reasons: dict[str, int] = field(default_factory=dict)

    def note(self, reason: str) -> None:
        self.rejection_reasons[reason] = self.rejection_reasons.get(reason, 0) + 1


def _accumulate_instrument(
    contract: AbnormalFlowContract,
    decisions: list[DecisionFeatures],
    counts: ScanCounts,
    histograms: dict[str, Histogram],
    primary_fires: list[DecisionFeatures],
    ablation_fires: list[DecisionFeatures],
    eligible_assets: set[str],
    eligible_weeks: set[str],
) -> None:
    for d in decisions:
        counts.scanned += 1
        if d.unavailable_reason is not None:
            counts.unavailable += 1
            counts.note(d.unavailable_reason)
            continue
        counts.available += 1
        if d.oi_growth_pct is not None:
            histograms["oi_growth_pct"].add(d.oi_growth_pct)
        if d.buy_pressure is not None:
            histograms["buy_pressure"].add(d.buy_pressure)
        if d.containment is not None:
            histograms["containment"].add(d.containment)
        oi_usd = oi_notional_usd(
            d.exchange, d.oi_native_amount, d.oi_native_value_usd, d.decision_price
        )
        part = participation_frac(contract.position_usd, d.pre_decision_turnover_usd)
        if oi_usd is not None:
            histograms["oi_notional_usd"].add(oi_usd)
        if part is not None:
            histograms["participation_frac"].add(part)
        if not is_eligible(contract, oi_usd, part):
            counts.ineligible += 1
            counts.note("below_eligibility_floor")
            continue
        counts.eligible += 1
        eligible_assets.add(d.canonical_asset)
        eligible_weeks.add(d.iso_week)
        if primary_cell_fires(contract, d):
            counts.primary_fires += 1
            primary_fires.append(d)
        if ablation_cell_fires(contract, d):
            counts.ablation_fires += 1
            ablation_fires.append(d)


# --- Orchestration -----------------------------------------------------------------


_OI_AGE_SQL = """
WITH b AS (
    SELECT exchange,
        open_interest_observed_at AS oi_at,
        date_diff('second', open_interest_observed_at, bucket_start) AS age_s
    FROM read_parquet(?)
    WHERE bucket_start >= ? AND bucket_start < ?
)
SELECT exchange,
    count(*) AS bars,
    count(*) FILTER (WHERE oi_at IS NULL) AS oi_null,
    round(median(age_s)) AS age_median_s,
    round(quantile_cont(age_s, 0.90)) AS age_p90_s,
    round(quantile_cont(age_s, 0.99)) AS age_p99_s,
    max(age_s) AS age_max_s,
    count(*) FILTER (
        WHERE oi_at IS NOT NULL
          AND age_s > (CASE WHEN exchange = 'bybit' THEN ? ELSE ? END)
    ) AS beyond_freshness
FROM b
GROUP BY 1 ORDER BY 1
"""


def oi_age_summary(
    paths: list[str],
    *,
    window_start: datetime,
    window_end: datetime,
    bybit_freshness_s: int,
    binance_freshness_s: int,
) -> list[dict[str, Any]]:
    """Per-venue OI-age distribution and the share of bars whose OI is older than the
    per-venue freshness ceiling. Positive age = OI observed BEFORE the bar (stale);
    negative = observed within the minute. Outcome-blind (reads no forward price)."""
    import duckdb

    connection = duckdb.connect()
    try:
        rows = connection.execute(
            _OI_AGE_SQL,
            [paths, window_start, window_end, bybit_freshness_s, binance_freshness_s],
        ).fetchall()
    finally:
        connection.close()
    out: list[dict[str, Any]] = []
    for r in rows:
        bars = int(r[1])
        beyond = int(r[7])
        out.append(
            {
                "exchange": str(r[0]),
                "bars": bars,
                "oi_null": int(r[2]),
                "age_median_s": None if r[3] is None else float(r[3]),
                "age_p90_s": None if r[4] is None else float(r[4]),
                "age_p99_s": None if r[5] is None else float(r[5]),
                "age_max_s": None if r[6] is None else float(r[6]),
                "beyond_freshness": beyond,
                "pct_beyond_freshness": round(100.0 * beyond / bars, 3) if bars else None,
            }
        )
    return out


def _git_revision() -> str:
    git_executable = shutil.which("git")
    if git_executable is None:  # pragma: no cover - git absence is not a scan failure
        return "unknown"
    try:
        return subprocess.run(  # noqa: S603 -- fixed argv, resolved executable, no shell, no input
            [git_executable, "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parent,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except Exception:  # pragma: no cover - git absence is not a scan failure
        return "unknown"


@dataclass(frozen=True)
class FreezeKnobs:
    """Pre-declared percentile knobs for the deterministic threshold freeze. Not fit to
    the data: the percentiles are fixed here and applied to the calibration slice."""

    calibration_days: int = 14
    floor_pct: float = 0.25
    oi_growth_pct: float = 0.80
    buy_pressure_pct: float = 0.70
    containment_pct: float = 0.50


def _linspace_edges(lo: float, hi: float, n: int) -> tuple[float, ...]:
    return tuple(lo + (hi - lo) * i / n for i in range(1, n))


def _logspace_edges(lo_exp: float, hi_exp: float, n: int) -> tuple[float, ...]:
    return tuple(10 ** (lo_exp + (hi_exp - lo_exp) * i / n) for i in range(1, n))


def _iter_decisions(
    paths: list[str],
    *,
    window_start: datetime,
    window_end: datetime,
    contract: AbnormalFlowContract,
    resolver: Callable[..., str | None],
) -> Any:
    """Stream per-instrument, assemble decisions (memory-bounded), and yield them one by
    one using the contract's scan lag / execution window / per-venue OI freshness."""
    from .abnormal_flow_replay import assemble_decisions

    scan_lag = contract.scan_lag_minutes
    exec_window = contract.entry_execution_window_minutes
    if scan_lag is None or exec_window is None:
        raise ValueError("contract must set scan_lag_minutes and entry_execution_window_minutes")
    for bars in iter_instrument_bars(paths, window_start=window_start, window_end=window_end):
        freshness = oi_freshness_limit_for(contract, bars[0].exchange)
        if freshness is None:
            continue
        native = bars[0]

        def _resolve(at: datetime, _b: MinuteBar = native) -> str | None:
            return resolver(
                _b.exchange, _b.market_type, _b.native_market_id, _b.capture_version, at
            )

        yield from assemble_decisions(
            bars,
            scan_lag_minutes=scan_lag,
            entry_execution_window_minutes=exec_window,
            oi_freshness_limit_seconds=freshness,
            resolve_canonical=_resolve,
        )


def compute_calibration_freeze(
    paths: list[str],
    *,
    calib_start: datetime,
    calib_end: datetime,
    contract: AbnormalFlowContract,
    resolver: Callable[..., str | None],
    knobs: FreezeKnobs,
) -> dict[str, Any]:
    """The proposed deterministic freeze (`fixed_percentiles_on_prestart_window_v1`),
    computed on the calibration slice ONLY and NON-CIRCULARLY:

    Pass 1 sets the eligibility floor from the ``floor_pct`` percentile of OI-notional
    over ALL available calibration decisions (never the already-eligible subset). Pass 2
    sets the OI-growth / buy-pressure / containment thresholds from percentiles over the
    decisions eligible under THAT floor plus the participation cap. Percentiles are
    approximate (pre-declared fine bins). Reads no returns."""
    oi_notional = Histogram(_logspace_edges(3, 10, 700))
    for d in _iter_decisions(
        paths, window_start=calib_start, window_end=calib_end, contract=contract, resolver=resolver
    ):
        if d.unavailable_reason is not None:
            continue
        oi_usd = oi_notional_usd(
            d.exchange, d.oi_native_amount, d.oi_native_value_usd, d.decision_price
        )
        if oi_usd is not None:
            oi_notional.add(oi_usd)
    floor = oi_notional.quantile(knobs.floor_pct)

    growth = Histogram(_linspace_edges(-100.0, 500.0, 600))
    buy = Histogram(_linspace_edges(0.5, 1.0, 500))
    contain = Histogram(_linspace_edges(0.0, 1.0, 1000))
    cap = contract.max_participation_frac
    for d in _iter_decisions(
        paths, window_start=calib_start, window_end=calib_end, contract=contract, resolver=resolver
    ):
        if d.unavailable_reason is not None:
            continue
        oi_usd = oi_notional_usd(
            d.exchange, d.oi_native_amount, d.oi_native_value_usd, d.decision_price
        )
        part = participation_frac(contract.position_usd, d.pre_decision_turnover_usd)
        if oi_usd is None or part is None:
            continue
        if floor is not None and oi_usd < floor:
            continue
        if cap is not None and part > cap:
            continue
        if d.oi_growth_pct is not None:
            growth.add(d.oi_growth_pct)
        if d.buy_pressure is not None:
            buy.add(d.buy_pressure)
        if d.containment is not None:
            contain.add(d.containment)

    return {
        "rule": "fixed_percentiles_on_prestart_window_v1",
        "calibration_window": {"start": calib_start.isoformat(), "end": calib_end.isoformat()},
        "knobs": asdict(knobs),
        "proposed_thresholds": {
            "min_oi_notional_usd": floor,
            "min_oi_growth_pct": growth.quantile(knobs.oi_growth_pct),
            "min_buy_pressure_ratio": buy.quantile(knobs.buy_pressure_pct),
            "max_price_containment": contain.quantile(knobs.containment_pct),
        },
        "note": (
            "PROPOSED, not registered. Floor from all-available OI-notional (non-circular); "
            "feature thresholds over floor+participation-eligible calibration decisions. "
            "Evaluation window is the disjoint remainder after the calibration slice."
        ),
    }


def run_scan(
    *,
    cold_bars_dir: Path,
    identity_snapshot: Path,
    contract: AbnormalFlowContract,
    out_root: Path,
    end: date,
    verify_archive: Callable[..., Any],
    provenance_dir: Path | None = None,
    start: date = DEFAULT_START,
    freeze_knobs: FreezeKnobs | None = None,
    run_id: str | None = None,
) -> Path:
    """Run the outcome-blind scan and write the artifact. Returns the artifact dir."""
    knobs = freeze_knobs or FreezeKnobs()
    resolver, identity_sha = load_identity_resolver(identity_snapshot)
    inventory, gap, chosen_end = inventory_window(
        cold_bars_dir, start=start, end=end, verify_archive=verify_archive
    )
    revision = _git_revision()
    run_id = run_id or datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + revision[:8]
    artifact_dir = out_root / run_id
    artifact_dir.mkdir(parents=True, exist_ok=True)

    manifest: dict[str, Any] = {
        "scan_version": SCAN_VERSION,
        "revision": revision,
        "identity_snapshot": {"path": str(identity_snapshot), "sha256": identity_sha},
        "start_day": start.isoformat(),
        "requested_end_day": end.isoformat(),
        "chosen_end_day": chosen_end.isoformat() if chosen_end else None,
        "gap": gap,
        "days": [asdict(d) for d in inventory],
    }
    (artifact_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    )

    if chosen_end is None or gap is not None or start > chosen_end:
        # No clean, continuous, fully-verified window: stop with the gap recorded.
        _write_readme(artifact_dir, revision, start, chosen_end, gap, stopped=True)
        (artifact_dir / "scan.json").write_text(
            json.dumps(
                {"scan_version": SCAN_VERSION, "stopped": True, "gap": gap},
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        return artifact_dir

    window_end_exclusive = chosen_end + timedelta(days=1)
    audit = audit_directory(cold_bars_dir, start=start, end=window_end_exclusive)
    paths = [
        str((cold_bars_dir / d.file_name).resolve())
        for d in inventory
        if d.day <= chosen_end.isoformat()
    ]
    window_start = datetime(start.year, start.month, start.day, tzinfo=UTC)
    window_end = datetime(
        window_end_exclusive.year, window_end_exclusive.month, window_end_exclusive.day, tzinfo=UTC
    )

    counts = ScanCounts()
    histograms = _new_histograms()
    primary_fires: list[DecisionFeatures] = []
    ablation_fires: list[DecisionFeatures] = []
    eligible_assets: set[str] = set()
    eligible_weeks: set[str] = set()
    per_venue: dict[str, dict[str, Any]] = {}

    def _pv(exchange: str) -> dict[str, Any]:
        return per_venue.setdefault(
            exchange, {"scanned": 0, "available": 0, "eligible": 0, "reasons": {}}
        )

    for d in _iter_decisions(
        paths,
        window_start=window_start,
        window_end=window_end,
        contract=contract,
        resolver=resolver,
    ):
        pv = _pv(d.exchange)
        pv["scanned"] += 1
        if d.unavailable_reason is not None:
            pv["reasons"][d.unavailable_reason] = pv["reasons"].get(d.unavailable_reason, 0) + 1
        else:
            pv["available"] += 1
            if _decision_eligible(contract, d):
                pv["eligible"] += 1
            else:
                pv["reasons"]["below_eligibility_floor"] = (
                    pv["reasons"].get("below_eligibility_floor", 0) + 1
                )
        _accumulate_instrument(
            contract,
            [d],
            counts,
            histograms,
            primary_fires,
            ablation_fires,
            eligible_assets,
            eligible_weeks,
        )

    cooldown = contract.cooldown_minutes
    primary_episodes = form_episodes(primary_fires, cooldown)
    ablation_episodes = form_episodes(ablation_fires, cooldown)

    # Deterministic threshold-freeze proposal, computed on the calibration slice only.
    calib_end_day = min(
        start + timedelta(days=knobs.calibration_days), chosen_end + timedelta(days=1)
    )
    calib_end = datetime(calib_end_day.year, calib_end_day.month, calib_end_day.day, tzinfo=UTC)
    proposed_freeze = compute_calibration_freeze(
        paths,
        calib_start=window_start,
        calib_end=calib_end,
        contract=contract,
        resolver=resolver,
        knobs=knobs,
    )

    oi_age = oi_age_summary(
        paths,
        window_start=window_start,
        window_end=window_end,
        bybit_freshness_s=contract.oi_freshness_limit_seconds_bybit or 0,
        binance_freshness_s=contract.oi_freshness_limit_seconds_binance or 0,
    )

    scan_json = {
        "scan_version": SCAN_VERSION,
        "audit_version": AUDIT_VERSION,
        "stopped": False,
        "window": {"start": window_start.isoformat(), "end": window_end.isoformat()},
        "provisional_contract": {k: v for k, v in asdict(contract).items() if v is not None},
        "coverage": [asdict(c) for c in audit.coverage],
        "per_venue": per_venue,
        "oi_age": oi_age,
        "counts": asdict(counts),
        "distributions": {name: hist.as_json() for name, hist in histograms.items()},
        "primary_episodes": len(primary_episodes),
        "ablation_episodes": len(ablation_episodes),
        "distinct_eligible_assets": len(eligible_assets),
        "distinct_eligible_weeks": len(eligible_weeks),
        "proposed_freeze": proposed_freeze,
    }
    (artifact_dir / "scan.json").write_text(json.dumps(scan_json, indent=2, sort_keys=True) + "\n")
    _write_readme(artifact_dir, revision, start, chosen_end, gap, stopped=False)
    return artifact_dir


def _write_readme(
    artifact_dir: Path,
    revision: str,
    start: date,
    chosen_end: date | None,
    gap: str | None,
    *,
    stopped: bool,
) -> None:
    end_str = chosen_end.isoformat() if chosen_end else "(none)"
    lines = [
        "# Abnormal-flow outcome-blind scan",
        "",
        "> Outcome-blind counts / calibration only. No forward price, PnL, or verdict.",
        "",
        f"- Revision: `{revision}`",
        f"- Window: `{start.isoformat()}` .. `{end_str}` (inclusive last fully-verified day)",
        f"- Stopped on gap: {'yes -- ' + str(gap) if stopped else 'no'}",
        "",
        "## Reproduce",
        "",
        "```bash",
        "abnormal-flow-scan \\",
        "  --cold-bars-dir <restored-cold-bars-dir> \\",
        "  --provenance-dir <provenance-dir> \\",
        "  --identity-snapshot <point-in-time-identity.json> \\",
        "  --contract-json <provisional-contract.json> \\",
        f"  --start-day {start.isoformat()}",
        "```",
        "",
        "`manifest.json` records each day's archive/hash/fidelity/receipt and the identity",
        "snapshot hash; `scan.json` holds coverage, rejection reasons, feature distributions,",
        "and counts (fires, independent episodes, assets, weeks). Raw Parquet is not stored here.",
    ]
    (artifact_dir / "README.md").write_text("\n".join(lines) + "\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cold-bars-dir", type=Path, required=True)
    parser.add_argument("--provenance-dir", type=Path, default=None)
    parser.add_argument("--identity-snapshot", type=Path, required=True)
    parser.add_argument("--contract-json", type=Path, required=True)
    verifier_group = parser.add_mutually_exclusive_group(required=True)
    verifier_group.add_argument(
        "--staging-json",
        type=Path,
        help="Staging artifact from abnormal-flow-stage (verify provenance without local borg)",
    )
    verifier_group.add_argument(
        "--borg-repo", help="Local Borg repo (only when borg runs on this host)"
    )
    parser.add_argument("--out-root", type=Path, default=DEFAULT_EVIDENCE_ROOT)
    parser.add_argument("--start-day", type=date.fromisoformat, default=DEFAULT_START)
    parser.add_argument("--end-day", type=date.fromisoformat, required=True)
    parser.add_argument("--calibration-days", type=int, default=FreezeKnobs.calibration_days)
    return parser


def main() -> None:
    from .abnormal_flow_stage import make_staging_verifier

    args: Any = build_parser().parse_args()
    contract = AbnormalFlowContract(**json.loads(Path(args.contract_json).read_text()))
    if args.staging_json is not None:
        verify_archive = make_staging_verifier(json.loads(Path(args.staging_json).read_text()))
    else:
        verify_archive = make_borg_verifier(args.borg_repo, args.provenance_dir)
    artifact_dir = run_scan(
        cold_bars_dir=args.cold_bars_dir,
        provenance_dir=args.provenance_dir,
        identity_snapshot=args.identity_snapshot,
        contract=contract,
        out_root=args.out_root,
        start=args.start_day,
        end=args.end_day,
        verify_archive=verify_archive,
        freeze_knobs=FreezeKnobs(calibration_days=args.calibration_days),
    )
    sys.stdout.write(f"{artifact_dir}\n")


if __name__ == "__main__":
    main()
