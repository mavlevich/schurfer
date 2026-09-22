import argparse
import hashlib
import json
import logging
import os
import sys
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

from .abnormal_flow_input_audit import verified_input
from .abnormal_flow_replay import (
    EpisodeRecord,
    Funnel,
    _decision_eligible,
    assemble_decisions,
    build_funnel,
    control_band_key,
    form_episodes,
    oi_freshness_limit_for,
    parquet_outcome_reader,
    primary_cell_fires,
    proxy_net_return,
    select_portfolio,
)
from .abnormal_flow_scan import load_identity_resolver
from .abnormal_flow_screen import AbnormalFlowContract

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


class GitStateProvider(Protocol):
    def get_revision(self) -> str: ...
    def is_dirty(self) -> bool: ...


class RealGitState(GitStateProvider):
    def get_revision(self) -> str:
        import subprocess

        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()  # noqa: S603, S607

    def is_dirty(self) -> bool:
        import subprocess

        return bool(subprocess.check_output(["git", "status", "--porcelain"], text=True).strip())  # noqa: S603, S607


def merge_funnels(a: Funnel, b: Funnel) -> Funnel:
    c = Funnel()
    c.scanned = a.scanned + b.scanned
    c.unavailable_feature = a.unavailable_feature + b.unavailable_feature
    c.ineligible = a.ineligible + b.ineligible
    c.eligible = a.eligible + b.eligible
    c.primary_fires = a.primary_fires + b.primary_fires
    c.no_oi_fires = a.no_oi_fires + b.no_oi_fires
    c.no_buy_fires = a.no_buy_fires + b.no_buy_fires
    c.no_containment_fires = a.no_containment_fires + b.no_containment_fires
    c.p99_fires = a.p99_fires + b.p99_fires
    c.sub_p99_fires = a.sub_p99_fires + b.sub_p99_fires
    c.primary_episodes = a.primary_episodes + b.primary_episodes
    c.no_oi_episodes = a.no_oi_episodes + b.no_oi_episodes
    c.no_buy_episodes = a.no_buy_episodes + b.no_buy_episodes
    c.no_containment_episodes = a.no_containment_episodes + b.no_containment_episodes
    c.p99_episodes = a.p99_episodes + b.p99_episodes
    c.sub_p99_episodes = a.sub_p99_episodes + b.sub_p99_episodes
    c.reasons = {
        k: a.reasons.get(k, 0) + b.reasons.get(k, 0) for k in set(a.reasons) | set(b.reasons)
    }
    return c


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return f"sha256:{h.hexdigest()}"


def iter_instrument_bars(paths: list[str], *, window_start: datetime, window_end: datetime):
    import duckdb

    connection = duckdb.connect()
    try:
        sql = """
        SELECT
            exchange, market_type, native_market_id, capture_version, symbol,
            CAST(bucket_start AT TIME ZONE 'UTC' AS TIMESTAMP) AS bucket_start,
            last_bid_price, last_ask_price, last_trade_price, last_trade_size,
            buy_total_notional_usd, sell_total_notional_usd,
            open_interest, open_interest_value,
            CAST(open_interest_observed_at
                AT TIME ZONE 'UTC' AS TIMESTAMP) AS open_interest_observed_at,
            CAST(last_trade_received_at
                AT TIME ZONE 'UTC' AS TIMESTAMP) AS last_trade_received_at,
            price_complete, trades_complete, open_interest_complete
        FROM read_parquet(?, filename=true)
        WHERE bucket_start >= ? AND bucket_start < ?
        ORDER BY exchange, market_type, native_market_id, capture_version, bucket_start
        """
        cursor = connection.execute(sql, [paths, window_start, window_end])
        current = None
        buffer = []
        from .abnormal_flow_replay import _row_to_bar

        while True:
            rows = cursor.fetchmany(20_000)
            if not rows:
                break
            for row in rows:
                bar = _row_to_bar(row)
                key = (bar.exchange, bar.market_type, bar.native_market_id, bar.capture_version)
                if key != current:
                    if buffer:
                        yield buffer
                    buffer = []
                    current = key
                buffer.append(bar)
        if buffer:
            yield buffer
    finally:
        connection.close()


def parse_committed_scan_manifest(path: Path) -> dict[str, dict[str, str]]:
    with path.open() as f:
        d = json.load(f)
    return {day["day"]: day for day in d.get("days", [])}


class FormalRunner:
    def __init__(self, git_state: GitStateProvider):
        self.git_state = git_state

    def run(
        self,
        contract_path: Path,
        eval_manifest_path: Path,
        scan_manifest_path: Path,
        identity_snapshot_path: Path,
        funding_snapshot_path: Path,
        funding_settlements_path: Path,
        cold_bars_dir: Path,
        output_dir: Path,
        require_formal_run: bool = True,
    ) -> dict:
        if require_formal_run and not sys.argv[1:]:
            raise ValueError("--formal-run flag required")

        if self.git_state.is_dirty():
            raise ValueError("Dirty tree detected.")

        rev = self.git_state.get_revision()

        # 1. Load and hash contract
        contract_bytes = contract_path.read_bytes()
        contract_dict = json.loads(contract_bytes)
        contract_dict.pop("contract_hash", None)
        contract = AbnormalFlowContract(**contract_dict)
        contract.require_frozen()
        contract_hash = contract.compute_hash()

        # 2. Verify Eval Manifest
        eval_bytes = eval_manifest_path.read_bytes()
        eval_hash = hashlib.sha256(eval_bytes).hexdigest()
        eval_manifest = json.loads(eval_bytes)

        # 3. Verify exact artifacts against eval manifest
        id_hash = _hash_file(identity_snapshot_path)
        fund_hash = _hash_file(funding_snapshot_path)
        settle_hash = _hash_file(funding_settlements_path)
        if id_hash != eval_manifest["identity_snapshot_hash"]:
            raise ValueError("identity hash mismatch")
        if fund_hash != eval_manifest["funding_snapshot_hash"]:
            raise ValueError("funding snapshot hash mismatch")
        if settle_hash != eval_manifest["funding_settlements_hash"]:
            raise ValueError("funding settlements hash mismatch")

        scan_manifest_sha = _hash_file(scan_manifest_path)
        if scan_manifest_sha != contract.input_fingerprint:
            raise ValueError("scan manifest sha != contract.input_fingerprint")
        if scan_manifest_sha != eval_manifest.get("input_audit_fingerprint"):
            raise ValueError("scan manifest sha != eval_manifest.input_audit_fingerprint")

        # 4. Atomic Run Directory keyed by hashes
        run_key = f"{contract_hash}_{eval_hash}"
        run_dir = output_dir / run_key
        try:
            run_dir.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            if (run_dir / "failed").exists():
                raise ValueError(f"Terminal failed status exists in {run_dir}") from None
            raise ValueError(f"Run {run_key} already exists") from None

        try:
            resolver, _ = load_identity_resolver(identity_snapshot_path)

            assert contract.window_start_utc and contract.window_end_utc
            w_start = datetime.fromisoformat(contract.window_start_utc.replace("Z", "+00:00"))
            w_end = datetime.fromisoformat(contract.window_end_utc.replace("Z", "+00:00"))

            scan_lag_td = timedelta(minutes=contract.scan_lag_minutes)
            lookback_start = w_start - scan_lag_td
            dep_start_date = lookback_start.date()

            horizon_td = timedelta(minutes=contract.outcome_horizon_minutes)
            entry_window = timedelta(minutes=contract.entry_execution_window_minutes)
            dep_end = w_end + entry_window + horizon_td
            dep_end_date = (dep_end + timedelta(days=1)).date()  # outward

            scan_manifest = parse_committed_scan_manifest(scan_manifest_path)
            verified_paths = []
            d = dep_start_date
            while d < dep_end_date:
                sm_day = scan_manifest.get(d.isoformat())
                if not sm_day:
                    raise ValueError(f"Missing day {d} in scan manifest")
                path, manifest = verified_input(cold_bars_dir, d)
                if manifest.sha256 != sm_day["sha256"]:
                    raise ValueError(f"Fidelity sha256 mismatch for {d}")
                if manifest.source_fingerprint != sm_day["source_fingerprint"]:
                    raise ValueError(f"Fidelity source_fingerprint mismatch for {d}")
                verified_paths.append(str(path))
                d += timedelta(days=1)

            def _resolve(ex, mt, mid, cv, at):
                return resolver.identity_key(ex, mt, mid, cv, at)

            logging.info("Pass 1: funnel and primary episodes...")
            funnel = None
            primary_pool = []

            for bars_chunk in iter_instrument_bars(
                verified_paths, window_start=lookback_start, window_end=dep_end
            ):
                if not bars_chunk:
                    continue
                freshness = oi_freshness_limit_for(contract, bars_chunk[0].exchange)
                if freshness is None:
                    continue
                decs = assemble_decisions(
                    bars_chunk,
                    scan_lag_minutes=contract.scan_lag_minutes,
                    entry_execution_window_minutes=contract.entry_execution_window_minutes,
                    oi_freshness_limit_seconds=freshness,
                    resolve_canonical=_resolve,
                )
                in_window = [d for d in decs if w_start <= d.decision_at < w_end]
                if not in_window:
                    continue

                chunk_funnel = build_funnel(contract, in_window)
                funnel = merge_funnels(funnel, chunk_funnel) if funnel else chunk_funnel

                p = [
                    d
                    for d in in_window
                    if _decision_eligible(contract, d) and primary_cell_fires(contract, d)
                ]
                primary_pool.extend(p)

            episodes = form_episodes(primary_pool, contract.cooldown_minutes)
            selected_episodes, skipped_portfolio_capacity = select_portfolio(contract, episodes)

            logging.info("Pass 2: bounded-memory controls...")
            episodes_by_band = defaultdict(list)
            for ep in episodes:
                key = control_band_key(ep)
                if key:
                    episodes_by_band[key].append(ep)
                    ep.controls = []

            for bars_chunk in iter_instrument_bars(
                verified_paths, window_start=lookback_start, window_end=w_end
            ):
                if not bars_chunk:
                    continue
                freshness = oi_freshness_limit_for(contract, bars_chunk[0].exchange)
                if freshness is None:
                    continue
                decs = assemble_decisions(
                    bars_chunk,
                    scan_lag_minutes=contract.scan_lag_minutes,
                    entry_execution_window_minutes=contract.entry_execution_window_minutes,
                    oi_freshness_limit_seconds=freshness,
                    resolve_canonical=_resolve,
                )
                in_window = [d for d in decs if w_start <= d.decision_at < w_end]
                for c in in_window:
                    if not _decision_eligible(contract, c) or primary_cell_fires(contract, c):
                        continue
                    k = control_band_key(c)
                    if not k or k not in episodes_by_band:
                        continue
                    for ep in episodes_by_band[k]:
                        if c.decision_at != ep.decision_at or c.symbol != ep.symbol:
                            ep.controls.append(c)
                            max_c = contract.controls_per_episode or 1
                            ep.controls.sort(
                                key=lambda x: (
                                    abs((x.decision_at - ep.decision_at).total_seconds()),
                                    x.symbol,
                                )
                            )
                            ep.controls = ep.controls[:max_c]

            import schurfer_analytics.abnormal_flow_replay as replay_mod

            replay_mod.FORMAL_RETURNS_RUN_ENABLED = True

            logging.info("Reading outcomes...")
            requested = {}
            for ep in episodes:
                requested[ep.route_key()] = ep
                for c in getattr(ep, "controls", []):
                    requested[c.route_key()] = c

            reader = parquet_outcome_reader(
                verified_paths, outcome_horizon_minutes=contract.outcome_horizon_minutes
            )
            outcomes = reader(list(requested.values()))

            def _return_for(d):
                out = outcomes.get(d.route_key())
                return (
                    proxy_net_return(contract, out.exchange, out.entry_price, out.exit_price)
                    if out
                    else None
                )

            net_returns, excesses, records = [], [], []
            unresolved_eps, unresolved_controls, resolved_controls = 0, 0, 0

            for ep in episodes:
                r = _return_for(ep)
                if r is None:
                    unresolved_eps += 1
                    continue

                net_returns.append(r)
                c_returns = []
                for c in getattr(ep, "controls", []):
                    cr = _return_for(c)
                    if cr is None:
                        unresolved_controls += 1
                    else:
                        resolved_controls += 1
                        c_returns.append(cr)

                excess = None
                if c_returns:
                    cmean = sum(c_returns) / len(c_returns)
                    excess = r - cmean
                    excesses.append(excess)

                records.append(
                    EpisodeRecord(
                        route_key=ep.route_key(),
                        canonical_asset=ep.canonical_asset,
                        iso_week=ep.iso_week,
                        decision_at=ep.decision_at,
                        net_return=r,
                        excess_return=excess,
                        portfolio_selected=ep in selected_episodes,
                    )
                )

            # Use build_report directly
            from .abnormal_flow_replay import build_report

            eco_report = build_report(
                contract,
                records,
                unresolved_episodes=unresolved_eps,
                resolved_controls=resolved_controls,
                requested_controls=len(episodes) * (contract.controls_per_episode or 1),
                skipped_portfolio_capacity=skipped_portfolio_capacity,
                selected_episodes=selected_episodes,
            )

            report = {
                "revision": rev,
                "clean_tree": not self.git_state.is_dirty(),
                "artifact_hashes": {
                    "contract": contract_hash,
                    "evaluation_manifest": eval_hash,
                    "identity_snapshot": id_hash,
                    "funding_snapshot": fund_hash,
                    "funding_settlements": settle_hash,
                    "scan_manifest": scan_manifest_sha,
                },
                "bounds": {
                    "dependency_start": dep_start_date.isoformat(),
                    "dependency_end": dep_end_date.isoformat(),
                    "evaluation_start": contract.window_start_utc,
                    "evaluation_end": contract.window_end_utc,
                },
                "funnel": asdict(funnel) if funnel else {},
                "economics": asdict(eco_report),
            }

            out_path = run_dir / "formal_run_report.json"
            tmp_path = run_dir / "formal_run_report.json.tmp"
            with tmp_path.open("w") as f:
                json.dump(report, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            tmp_path.rename(out_path)
            logging.info("Run finished.")
            return report

        except Exception as e:
            (run_dir / "failed").write_text(str(e))
            raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--formal-run", action="store_true")
    parser.add_argument("--contract", type=Path, required=True)
    parser.add_argument("--eval-manifest", type=Path, required=True)
    parser.add_argument("--scan-manifest", type=Path, required=True)
    parser.add_argument("--identity-snapshot", type=Path, required=True)
    parser.add_argument("--funding-snapshot", type=Path, required=True)
    parser.add_argument("--funding-settlements", type=Path, required=True)
    parser.add_argument("--cold-bars-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    # We must require --formal-run when called from CLI
    if not args.formal_run:
        logging.error("--formal-run flag required")
        sys.exit(1)

    runner = FormalRunner(RealGitState())
    runner.run(
        args.contract,
        args.eval_manifest,
        args.scan_manifest,
        args.identity_snapshot,
        args.funding_snapshot,
        args.funding_settlements,
        args.cold_bars_dir,
        args.output_dir,
        require_formal_run=False,  # We already checked it in CLI entrypoint
    )
