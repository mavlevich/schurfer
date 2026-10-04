"""Real PostgreSQL/TimescaleDB checks for the HYP-015 reader inputs archive.

Seeds the real tables with synthetic rows inside the registered cohort window, archives
the watch chunks, takes a snapshot set, and restores it into two scratch schemas of the
same disposable database, where the registered reader's readiness and health paths run.
Borg is the directory-backed fake of the LSR pilot tests. Every seeded row carries the
`ITX` marker and is removed, with the window's watch chunks and the HYP-015 catalog rows
and sets, before and after each test (catalog guards are bypassed for that session
only, as in the pilot's tests).
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import stat
import sys
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from schurfer_analytics import history_archive as engine
from schurfer_analytics import hyp015_inputs_archive as h
from schurfer_analytics.momentum_flow_hold12h_verdict import ACTUAL_FUNDING_VERSION
from schurfer_analytics.momentum_flow_paper_contract import (
    FROZEN_PAPER_CONTRACT,
    HOLD12H_PAPER_CONTRACT,
)
from schurfer_journal.testing_database import integration_database_url

DSN = integration_database_url()
NOW = datetime(2026, 11, 10, tzinfo=UTC)
D0 = datetime(2026, 10, 4, tzinfo=UTC)
DAYS = 30  # 2026-10-04 .. 11-02, the watch window with its margin
HOLD = HOLD12H_PAPER_CONTRACT
WV, EX, MT = HOLD.watch_version, HOLD.source_exchange, HOLD.market_type
FV = ACTUAL_FUNDING_VERSION
INPUT_HASH = b"\x00\x01'\\\n" + b"\x07" * 27  # 32 bytes, with a quote, backslash, newline

FAKE_BORG = """#!{python}
import pathlib, shutil, sys
args = sys.argv[1:]
def split(target):
    repo, _, name = target.partition("::")
    return pathlib.Path(repo), name
if args[0] == "create":
    repo, name = split(args[3])
    dest = repo / name
    dest.mkdir(parents=True)
    for member in args[4:]:
        shutil.copy(member, dest / member)
elif args[0] == "list":
    repo, name = split(args[-1])
    print("\\n".join(sorted(p.name for p in (repo / name if name else repo).iterdir())))
elif args[0] == "extract":
    repo, name = split(args[2])
    sys.stdout.buffer.write((repo / name / args[3]).read_bytes())
elif args[0] == "delete":
    repo, name = split(args[1])
    shutil.rmtree(repo / name)
"""


def _connect() -> psycopg.Connection[Any]:
    try:
        conn = psycopg.connect(DSN, autocommit=True, connect_timeout=2)
    except psycopg.OperationalError as exc:
        if os.getenv("REQUIRE_INTEGRATION_DB") == "1":
            raise
        pytest.skip(f"no local postgres reachable: {exc}")
    if conn.execute("SELECT to_regclass('app.history_archive_snapshot_sets')").fetchone() == (
        None,
    ):
        conn.close()
        pytest.skip("migration 0059 is not applied")
    return conn


def _clean(conn: psycopg.Connection[Any]) -> None:
    """Test-only: remove the window's watch chunks, the seeded rows and the HYP-015
    catalog rows and sets (guards bypassed for this session on the disposable DB)."""
    conn.execute(
        "SELECT drop_chunks('timeseries.momentum_flow_watch_evaluations_1m', "
        "older_than => %s, newer_than => %s)",
        (D0 + timedelta(days=DAYS + 1), D0 - timedelta(days=1)),
    )
    with conn.transaction():
        conn.execute("SET LOCAL session_replication_role = replica")
        conn.execute("DELETE FROM app.history_archive_datasets WHERE dataset LIKE 'hyp015\\_%'")
        conn.execute(
            "DELETE FROM app.history_archive_snapshot_sets WHERE purpose = 'hyp015_inputs'"
        )
    conn.execute("DELETE FROM app.momentum_flow_paper_probes WHERE symbol LIKE 'ITX%'")
    conn.execute("DELETE FROM app.momentum_universe_snapshots WHERE universe_version LIKE 'itx%'")
    conn.execute("DELETE FROM app.hold12h_funding_coverage_runs WHERE native_market_id LIKE 'ITX%'")
    conn.execute("DELETE FROM app.hold12h_funding_settlements WHERE native_market_id LIKE 'ITX%'")
    conn.execute("DROP SCHEMA IF EXISTS hyp015_restore_ts CASCADE")
    conn.execute("DROP SCHEMA IF EXISTS hyp015_restore_app CASCADE")


@pytest.fixture
def db() -> Any:
    conn = _connect()
    _clean(conn)
    yield conn
    _clean(conn)
    conn.close()


@pytest.fixture
def borg(tmp_path: Path) -> tuple[str, dict[str, str]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "borg"
    fake.write_text(FAKE_BORG.replace("{python}", sys.executable))
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    repo = tmp_path / "repo"
    repo.mkdir()
    return str(repo), {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}


def _watch(conn: Any, bucket: datetime, symbol: str, *, eligible: bool, reasons: str = "{}") -> str:
    watch_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO timeseries.momentum_flow_watch_evaluations_1m (exchange, market_type, "
        "symbol, capture_version, watch_version, bucket_start, universe_version, quality_ready, "
        "raw_qualified, decision_status, reason_codes, price_return_60m_pct, oi_growth_60m_pct, "
        "cross_section_size, evaluator_started_at, evaluator_completed_at, decision_at, "
        "episode_id, watch_id, state_active_after, state_clear_streak_after, input_hash) "
        "VALUES (%s, %s, %s, 'cap1', %s, %s, 'itx-u1', true, %s, %s, %s::text[], "
        "'NaN'::float8, '-Infinity'::float8, 3, %s, %s, %s, %s, %s, false, 0, %s)",
        (
            EX,
            MT,
            symbol,
            WV,
            bucket,
            eligible,
            "watch" if eligible else "rejected_signal",
            reasons,
            bucket,
            bucket + timedelta(seconds=40),
            bucket + timedelta(seconds=30),
            str(uuid.uuid4()) if eligible else None,
            watch_id if eligible else None,
            INPUT_HASH,
        ),
    )
    return watch_id


def _runs(conn: Any) -> None:
    for version in (HOLD.paper_version, FROZEN_PAPER_CONTRACT.paper_version):
        conn.execute(
            "INSERT INTO app.momentum_flow_paper_runs (paper_version, contract_sha256, "
            "contract_json, cohort_started_at) VALUES (%s, %s, '{}', %s) "
            "ON CONFLICT (paper_version) DO NOTHING",
            (version, "c" * 64, D0),
        )


def _probe(
    conn: Any,
    watch_id: str,
    bucket: datetime,
    *,
    version: str = HOLD.paper_version,
    status: str = "closed",
    symbol: str = "ITX1",
) -> str:
    paper_id = str(uuid.uuid4())
    decided = bucket + timedelta(seconds=40)
    opened = status in ("closed", "open")
    conn.execute(
        "INSERT INTO app.momentum_flow_paper_probes (paper_id, paper_version, watch_version, "
        "watch_id, episode_id, exchange, market_type, symbol, market_id, watch_bucket_start, "
        "watch_decision_at, claimed_at, entry_status, position_status, entry_at, entry_vwap, "
        "entry_filled_notional_usd, exit_at, exit_vwap, exit_reason, accounting_status, "
        "last_error) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
        "%s, %s, %s, %s, %s, %s)",
        (
            paper_id,
            version,
            WV,
            watch_id,
            str(uuid.uuid4()),
            EX,
            MT,
            symbol,
            symbol,
            bucket,
            decided,
            decided + timedelta(seconds=1),
            "opened" if opened else "rejected_stale",
            status if opened else "not_open",
            decided + timedelta(seconds=2) if opened else None,
            1.5 if opened else None,
            50.0 if opened else None,
            decided + timedelta(hours=12) if status == "closed" else None,
            1.4 if status == "closed" else None,
            "max_hold" if status == "closed" else None,
            "complete" if status == "closed" else None,
            'line one\nline "two", three' if opened else None,
        ),
    )
    return paper_id


def _outcome(conn: Any, paper_id: str, horizon: int, entry: datetime) -> None:
    conn.execute(
        "INSERT INTO app.momentum_flow_paper_outcomes (paper_id, horizon_minutes, due_at, "
        "status, quote_observed_at, bid_vwap, accounting_status) "
        "VALUES (%s, %s, %s, 'complete', %s, 1.2, 'complete')",
        (paper_id, horizon, entry + timedelta(minutes=horizon), entry + timedelta(minutes=horizon)),
    )


def _seed(conn: Any, *, missing_720: bool = False) -> dict[str, Any]:
    """30 daily chunks (one ineligible row each), eligible WATCH on two days, a closed
    hold12h probe with full outcomes, accounting and funding, a rejected probe, a
    baseline probe, the universe and funding rows."""
    for day in range(DAYS):
        _watch(conn, D0 + timedelta(days=day, hours=6), f"ITXQ{day}", eligible=False)
    b1 = datetime(2026, 10, 6, 12, tzinfo=UTC)
    b2 = datetime(2026, 10, 20, 8, tzinfo=UTC)
    w1 = _watch(conn, b1, "ITX1", eligible=True, reasons='{"a,b","q\\"x"}')
    w2 = _watch(conn, b1 + timedelta(minutes=5), "ITX2", eligible=True)
    w3 = _watch(conn, b2, "ITX3", eligible=True, reasons="{}")
    _runs(conn)
    closed = _probe(conn, w1, b1)
    entry = b1 + timedelta(seconds=42)
    _outcome(conn, closed, 240, entry)
    if not missing_720:
        _outcome(conn, closed, 720, entry)
    _probe(conn, w2, b1 + timedelta(minutes=5), status="rejected", symbol="ITX2")
    _probe(conn, w3, b2, version=FROZEN_PAPER_CONTRACT.paper_version, symbol="ITX3")
    conn.execute(
        "INSERT INTO app.momentum_universe_snapshots (exchange, universe_version, "
        "catalog_version, capture_version, schema_version, captured_at, instrument_count, "
        "payload_hash) VALUES (%s, 'itx-u1', 'itx-c1', 'cap1', 's1', %s, 1, %s)",
        (EX, D0 - timedelta(days=1), b"\x11" * 32),
    )
    conn.execute(
        "INSERT INTO app.momentum_universe_instruments (exchange, universe_version, "
        "catalog_version, native_market_id, base, quote, settle, native_market_type, "
        "canonical_market_type, identity_status, identity_key, onboarded_at, metadata_hash) "
        "VALUES (%s, 'itx-u1', 'itx-c1', 'ITX1', 'ITX', 'USDT', 'USDT', 'linear', 'swap', "
        "'ready', 'itx:asset', %s, %s)",
        (EX, D0 - timedelta(days=30), b"\x22" * 32),
    )
    conn.execute(
        "INSERT INTO app.hold12h_funding_coverage_runs (exchange, native_market_id, "
        "unified_symbol, market_type, requested_since, requested_until, status, request_count, "
        "source_version) VALUES (%s, 'ITX1', 'ITX/USDT:USDT', %s, %s, %s, 'complete', 1, %s)",
        (EX, MT, entry - timedelta(hours=1), entry + timedelta(hours=13), FV),
    )
    conn.execute(
        "INSERT INTO app.hold12h_funding_settlements (exchange, native_market_id, "
        "unified_symbol, market_type, settlement_at, funding_rate, source_at, observed_at, "
        "fetched_at, native_payload, source_version) VALUES (%s, 'ITX1', 'ITX/USDT:USDT', %s, "
        "%s, 0.0001, %s, %s, %s, %s, %s)",
        (
            EX,
            MT,
            entry + timedelta(hours=4),
            entry,
            entry,
            entry,
            json.dumps({"rate": "0.0001", "note": "ünïcode"}),
            FV,
        ),
    )
    return {"eligible": 3, "closed": closed}


_TICKS = [0]


def _tick() -> datetime:
    """A later instant per archive run: one archive name per second, as in production."""
    _TICKS[0] += 1
    return NOW + timedelta(seconds=_TICKS[0])


def _archive_all(repo: str, env: dict[str, str], out: Path) -> None:
    archived = h.archive_inputs(DSN, out, repo=repo, env=env, now=_tick())
    _tick()  # the set-manifest archive took the next second
    assert not archived.failed, archived
    verified = engine.run_verify(
        DSN, h.ALL_CONTRACTS, out, repo=repo, env=env, now=_tick(), reserve_bytes=0
    )
    assert not verified.failed, verified


def _verified_set(repo: str, env: dict[str, str], out: Path) -> h.SetReport:
    made = h.take_snapshot_set(DSN, out, code_revision="test", reserve_bytes=0)
    _archive_all(repo, env, out)
    h.verify_set(DSN, made.set_id, now=NOW, repo=repo, env=env, work_dir=out)
    return made


def _export_watch(out: Path) -> None:
    report = engine.run_export(
        DSN, h.WATCH_CONTRACT, out, code_revision="test", now=NOW, max_chunks=40, reserve_bytes=0
    )
    assert len(report.done) == DAYS and not report.failed, report


def test_full_path_restores_inputs_the_registered_reader_can_read(
    db: Any, tmp_path: Path, borg: tuple[str, dict[str, str]]
) -> None:
    repo, env = borg
    seeded = _seed(db)
    out = tmp_path / "out"
    _export_watch(out)
    _archive_all(repo, env, out)
    made = _verified_set(repo, env, out)
    assert made.pinned_chunks == DAYS and made.formal_ready
    report = h.restore_check(
        DSN, DSN, made.set_id, tmp_path / "work", repo=repo, env=env, reserve_bytes=0
    )
    assert report.ok, report.failures
    assert report.restored[h.WATCH_CONTRACT.dataset] == seeded["eligible"]
    assert report.readiness["total_watches"] == seeded["eligible"]
    assert report.readiness["identity_resolved"] >= 1  # the universe was restored
    assert report.health["funding_covered"] == 1 and report.formal_ready
    restored = db.execute(
        "SELECT reason_codes::text, input_hash, price_return_60m_pct::text, "
        "oi_growth_60m_pct::text FROM hyp015_restore_ts.momentum_flow_watch_evaluations_1m "
        "WHERE symbol = 'ITX1'"
    ).fetchone()
    assert restored == ('{"a,b","q\\"x"}', INPUT_HASH, "NaN", "-Infinity")


def test_a_late_watch_row_forces_a_new_revision_before_a_set(
    db: Any, tmp_path: Path, borg: tuple[str, dict[str, str]]
) -> None:
    repo, env = borg
    _seed(db)
    out = tmp_path / "out"
    _export_watch(out)
    _archive_all(repo, env, out)
    late_day = datetime(2026, 10, 9, tzinfo=UTC)
    _watch(db, late_day + timedelta(hours=3), "ITXLATE", eligible=False)
    with pytest.raises(engine.ArchiveError, match="changed since export"):
        h.take_snapshot_set(DSN, out, code_revision="test", reserve_bytes=0)
    assert db.execute("SELECT count(*) FROM app.history_archive_snapshot_sets").fetchone() == (0,)
    row = engine.live_rows(db, h.WATCH_CONTRACT)[late_day]
    engine.supersede(db, row.id, "late insert after export")
    _export_watch_one(out)
    _archive_all(repo, env, out)
    made = _verified_set(repo, env, out)
    pinned = {p["range_start"]: p["id"] for p in _pinned(db, made.set_id)}
    assert pinned[late_day.isoformat()] != row.id


def _export_watch_one(out: Path) -> None:
    report = engine.run_export(
        DSN, h.WATCH_CONTRACT, out, code_revision="test", now=NOW, max_chunks=40, reserve_bytes=0
    )
    assert len(report.done) == 1, report


def _pinned(conn: Any, set_id: str) -> list[dict[str, Any]]:
    row = conn.execute(
        "SELECT pinned_chunks FROM app.history_archive_snapshot_sets WHERE set_id = %s",
        (set_id,),
    ).fetchone()
    assert row is not None
    return list(row[0])


def test_an_incomplete_set_is_never_verified_and_a_newer_one_replaces_the_old(
    db: Any, tmp_path: Path, borg: tuple[str, dict[str, str]]
) -> None:
    repo, env = borg
    _seed(db)
    out = tmp_path / "out"
    _export_watch(out)
    _archive_all(repo, env, out)
    first = h.take_snapshot_set(DSN, out, code_revision="test", reserve_bytes=0)
    # Members and the set manifest archived, members not yet verified by extraction.
    assert not h.archive_inputs(DSN, out, repo=repo, env=env, now=_tick()).failed
    _tick()
    with pytest.raises(psycopg.errors.RaiseException, match="lacks verified members"):
        h.verify_set(DSN, first.set_id, now=NOW, repo=repo, env=env, work_dir=out)
    verified = engine.run_verify(
        DSN, h.ALL_CONTRACTS, out, repo=repo, env=env, now=_tick(), reserve_bytes=0
    )
    assert not verified.failed
    assert h.verify_set(DSN, first.set_id, now=NOW, repo=repo, env=env, work_dir=out) == []
    second = _verified_set(repo, env, out)
    states = dict(
        db.execute(
            "SELECT set_id, state FROM app.history_archive_snapshot_sets ORDER BY set_id"
        ).fetchall()
    )
    assert states == {first.set_id: "superseded", second.set_id: "verified"}
    with pytest.raises(psycopg.errors.RaiseException, match="belongs to verified set"):
        member = engine.snapshot_rows(db, h.PLAIN_CONTRACTS[0])[-1]
        engine.supersede(db, member.id, "no")


def test_a_corrupted_archived_input_fails_the_restore_check(
    db: Any, tmp_path: Path, borg: tuple[str, dict[str, str]]
) -> None:
    repo, env = borg
    _seed(db)
    out = tmp_path / "out"
    _export_watch(out)
    _archive_all(repo, env, out)
    made = _verified_set(repo, env, out)
    member = engine.snapshot_rows(db, h.PLAIN_CONTRACTS[1])[-1]
    target = Path(repo) / str(member.borg_archive) / member.file_name
    target.write_bytes(target.read_bytes()[:-8] + b"corrupt!")
    report = h.restore_check(
        DSN, DSN, made.set_id, tmp_path / "work", repo=repo, env=env, reserve_bytes=0
    )
    assert not report.ok
    assert any("momentum_flow_paper_outcomes" in f and "sha256" in f for f in report.failures)


def test_unready_events_are_preserved_and_reported_not_required(
    db: Any, tmp_path: Path, borg: tuple[str, dict[str, str]]
) -> None:
    repo, env = borg
    _seed(db, missing_720=True)
    out = tmp_path / "out"
    _export_watch(out)
    _archive_all(repo, env, out)
    made = _verified_set(repo, env, out)
    assert made.formal_ready is False
    report = h.restore_check(
        DSN, DSN, made.set_id, tmp_path / "work", repo=repo, env=env, reserve_bytes=0
    )
    assert report.ok, report.failures
    assert report.formal_ready is False


def test_an_empty_snapshot_member_round_trips(db: Any, tmp_path: Path) -> None:
    contract = replace(h.PLAIN_CONTRACTS[-1], snapshot_filter="FALSE")
    with engine._snapshot(DSN) as conn:
        manifest = engine.export_snapshot_member(
            conn,
            contract,
            tmp_path,
            set_id="empty",
            revision=1,
            snapshot_at=NOW,
            code_revision="test",
        )
    assert manifest.row_count == 0
    rows, fingerprint = engine.parquet_fingerprint(contract, tmp_path / manifest.file_name)
    assert (rows, fingerprint) == (0, manifest.content_fingerprint)


def test_pinned_columns_detect_schema_drift(db: Any) -> None:
    contract = replace(h.PLAIN_CONTRACTS[0], columns=h.PLAIN_CONTRACTS[0].columns[:-1])
    with engine._snapshot(DSN) as conn, pytest.raises(engine.ArchiveError, match="columns differ"):
        engine.check_columns(conn, contract)


def test_the_formal_path_is_not_reachable_from_the_module() -> None:
    tree = ast.parse(Path(h.__file__).read_text())
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
    forbidden = {
        "load_cohort",
        "open_formal_claim",
        "complete_formal_claim",
        "publish_formal_result",
        "run_formal_for_test",
        "pinned_inputs",
        "load_snapshot_from_db",
    }
    assert names & forbidden == set()
    assert "--formal-run" not in Path(h.__file__).read_text()


# ---------- review 3 regressions ----------


def test_the_snapshot_export_keeps_the_disk_reserve(
    db: Any, tmp_path: Path, borg: tuple[str, dict[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, env = borg
    _seed(db)
    out = tmp_path / "out"
    _export_watch(out)
    _archive_all(repo, env, out)
    with pytest.raises(engine.ArchiveError, match="reserve"):
        h.take_snapshot_set(DSN, out, code_revision="test", reserve_bytes=1 << 60)
    assert not list(out.glob("*-hyp015-*"))  # no member file was written
    assert db.execute("SELECT count(*) FROM app.history_archive_snapshot_sets").fetchone() == (0,)

    # The upfront estimate can be wrong: free space is watched while writing too.
    free = [10**12, 10**12]

    def shrinking(_path: Any) -> Any:
        free.append(free[-1] // 1000)
        return type("Usage", (), {"free": free[-1]})()

    monkeypatch.setattr(shutil, "disk_usage", shrinking)
    monkeypatch.setattr(engine, "ensure_reserve", lambda *_a: None)
    monkeypatch.setattr(engine, "RESERVE_CHECK_EVERY", 1)
    with pytest.raises(engine.ArchiveError, match="fell below"):
        h.take_snapshot_set(DSN, out, code_revision="test", reserve_bytes=10**10)


def test_a_preliminary_set_pins_the_verified_prefix(
    db: Any, tmp_path: Path, borg: tuple[str, dict[str, str]]
) -> None:
    repo, env = borg
    seeded = _seed(db)
    out = tmp_path / "out"
    early = engine.run_export(
        DSN,
        h.WATCH_CONTRACT,
        out,
        code_revision="test",
        now=D0 + timedelta(days=4),
        max_chunks=40,
        reserve_bytes=0,
    )
    assert len(early.done) == 3  # 10-04, 10-05, 10-06 have closed
    _archive_all(repo, env, out)
    with pytest.raises(engine.ArchiveError, match="no verified archive covers"):
        h.take_snapshot_set(DSN, out, code_revision="test", reserve_bytes=0)  # final
    made = h.take_snapshot_set(DSN, out, code_revision="test", final=False, reserve_bytes=0)
    assert (made.kind, made.pinned_chunks) == ("preliminary", 3)
    assert made.coverage_end == (D0 + timedelta(days=3)).isoformat()
    _archive_all(repo, env, out)
    h.verify_set(DSN, made.set_id, now=NOW, repo=repo, env=env, work_dir=out)
    report = h.restore_check(
        DSN, DSN, made.set_id, tmp_path / "work", repo=repo, env=env, reserve_bytes=0
    )
    assert report.ok, report.failures
    assert report.readiness["total_watches"] == 2  # the two WATCH of 10-06; 10-20 is later
    assert seeded["eligible"] == 3


def test_the_set_manifest_is_offsite_and_checked(
    db: Any, tmp_path: Path, borg: tuple[str, dict[str, str]]
) -> None:
    repo, env = borg
    _seed(db)
    out = tmp_path / "out"
    _export_watch(out)
    _archive_all(repo, env, out)
    made = _verified_set(repo, env, out)
    archived = [json.loads(p.read_text()) for p in Path(repo).glob("*/*.set.manifest.json")]
    assert [m["set_id"] for m in archived] == [made.set_id]
    assert archived[0]["pinned_chunks"] and archived[0]["reference"]
    assert {m["dataset"] for m in archived[0]["members"]} == {c.dataset for c in h.PLAIN_CONTRACTS}
    copy = next(Path(repo).glob(f"*/{made.set_id}.set.manifest.json"))
    copy.write_text(copy.read_text().replace('"final"', '"other"'))
    with pytest.raises(engine.ArchiveError, match="archived manifest"):
        h.restore_check(
            DSN, DSN, made.set_id, tmp_path / "work", repo=repo, env=env, reserve_bytes=0
        )


def test_an_unarchived_set_manifest_blocks_verification(
    db: Any, tmp_path: Path, borg: tuple[str, dict[str, str]]
) -> None:
    repo, env = borg
    _seed(db)
    out = tmp_path / "out"
    _export_watch(out)
    _archive_all(repo, env, out)
    made = h.take_snapshot_set(DSN, out, code_revision="test", reserve_bytes=0)
    engine.run_archive(DSN, h.ALL_CONTRACTS, out, repo=repo, env=env, now=_tick())
    engine.run_verify(DSN, h.ALL_CONTRACTS, out, repo=repo, env=env, now=_tick(), reserve_bytes=0)
    with pytest.raises(engine.ArchiveError, match="no archived manifest"):
        h.verify_set(DSN, made.set_id, now=NOW, repo=repo, env=env, work_dir=out)
