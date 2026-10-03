"""Real PostgreSQL/TimescaleDB checks for the LSR history archive pilot.

Every row these tests write lies before 2002 on the real `app.live_long_short_ratio`, and
each test drops those chunks before and after itself. Catalog rows are never deleted (the
migration forbids it), so each test archives under its own dataset name, except the
freeze-protocol test, which exercises the real `lsr_history` fence on 1995 data only.
Borg is a fake that keeps archives as directories, driven through the same argv.
"""

from __future__ import annotations

import os
import stat
import sys
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from schurfer_analytics import lsr_history_archive as archive
from schurfer_analytics.cold_bar_fetch import sha256_of
from schurfer_journal.testing_database import integration_database_url

DSN = integration_database_url()
NOW = datetime(2026, 10, 4, tzinfo=UTC)
W1 = datetime(2001, 1, 4, tzinfo=UTC)  # a Thursday: weekly chunks start here
WEEK = timedelta(days=7)
TEXT_COLUMNS = "ts, base, exchange, ratio::text, long_account::text, short_account::text"

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
    if conn.execute("SELECT to_regclass('app.history_archive_datasets')").fetchone() == (None,):
        conn.close()
        pytest.skip("migration 0058 is not applied")
    return conn


@pytest.fixture
def db() -> Any:
    conn = _connect()
    drop = (
        "SELECT drop_chunks('app.live_long_short_ratio', older_than => '2002-01-01'::timestamptz)"
    )
    conn.execute(drop)
    yield conn
    conn.execute(drop)
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


def _contract() -> archive.DatasetContract:
    return replace(archive.LSR_CONTRACT, dataset=f"lsr_it_{uuid.uuid4().hex[:10]}")


def _rows(start: datetime, n: int) -> list[tuple[Any, ...]]:
    """Values that a lossy path would change: a NULL, an empty base, trailing-zero and
    long-scale numerics, a negative zero, microseconds and a non-ASCII base."""
    special = [
        (start + timedelta(microseconds=1), "", "binance", "1.50", None, "0.000000000000000001"),
        (start + timedelta(seconds=1, microseconds=999999), "ÄÖ", "binance", "-0", "2", None),
        (
            start + timedelta(minutes=1),
            "BTC",
            "bybit",
            "123456789012345678901.123456789",
            "0.5",
            "0.5",
        ),
    ]
    regular = [
        (
            start + timedelta(minutes=5 * i + 7),
            f"B{i % 40}",
            "binance",
            f"{1 + i / 1000:.4f}",
            "0.6",
            "0.4",
        )
        for i in range(n)
    ]
    return special + regular


def _insert(conn: psycopg.Connection[Any], rows: list[tuple[Any, ...]]) -> None:
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO app.live_long_short_ratio "
            "(ts, base, exchange, ratio, long_account, short_account) "
            "VALUES (%s, %s, %s, %s::numeric, %s::numeric, %s::numeric)",
            rows,
        )


def _chunk(conn: psycopg.Connection[Any], start: datetime) -> archive.Chunk:
    (chunk,) = [
        c for c in archive.list_chunks(conn, archive.LSR_CONTRACT) if c.range_start == start
    ]
    return chunk


def _source_text(
    conn: psycopg.Connection[Any], start: datetime, end: datetime
) -> set[tuple[Any, ...]]:
    return set(
        conn.execute(
            f"SELECT {TEXT_COLUMNS} FROM app.live_long_short_ratio "  # noqa: S608
            "WHERE ts >= %s AND ts < %s",
            (start, end),
        ).fetchall()
    )


def test_export_and_restore_keep_every_value_exactly(db: Any, tmp_path: Path) -> None:
    _insert(db, _rows(W1, 300))
    chunk = _chunk(db, W1)
    assert (chunk.range_start, chunk.range_end) == (W1, W1 + WEEK)
    contract = _contract()
    manifest = archive.export_chunk(
        DSN, contract, chunk, tmp_path, revision=1, code_revision="test", reserve_bytes=0
    )
    parquet = tmp_path / manifest.file_name
    assert manifest.row_count == 303
    assert archive.parquet_fingerprint(contract, parquet) == (303, manifest.content_fingerprint)
    assert manifest.file_sha256 == sha256_of(parquet)
    assert {k["exchange"]: k["rows"] for k in manifest.data_keys} == {"binance": 302, "bybit": 1}
    assert not list(tmp_path.glob(".*partial"))
    db.execute("DROP SCHEMA IF EXISTS lsr_restore_it CASCADE")
    db.execute("CREATE SCHEMA lsr_restore_it")
    try:
        assert archive.restore_into(db, contract, parquet, "lsr_restore_it.lsr") == 303
        restored_sql = f"SELECT {TEXT_COLUMNS} FROM lsr_restore_it.lsr"  # noqa: S608
        restored = set(db.execute(restored_sql).fetchall())
        assert restored == _source_text(db, W1, W1 + WEEK)
        assert ("", "1.50") in {(r[1], r[3]) for r in restored}  # scale and '' kept
        assert any(r[4] is None for r in restored)  # NULL kept apart from ''
    finally:
        db.execute("DROP SCHEMA lsr_restore_it CASCADE")


def test_export_reads_one_snapshot_and_a_later_row_is_detected(
    db: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _insert(db, _rows(W1, 50))
    chunk = _chunk(db, W1)
    contract = _contract()
    late = (W1 + timedelta(days=3), "LATE", "binance", "9", "9", "9")
    original = archive._copy_rows_gz

    def copy_after_a_concurrent_commit(conn: Any, *args: Any) -> None:
        # Committed by another session after the count and fingerprint were read but
        # before the COPY: one snapshot means the file must not contain it.
        with psycopg.connect(DSN, autocommit=True) as other:
            _insert(other, [late])
        original(conn, *args)

    monkeypatch.setattr(archive, "_copy_rows_gz", copy_after_a_concurrent_commit)
    manifest = archive.export_chunk(
        DSN, contract, chunk, tmp_path, revision=1, code_revision="test", reserve_bytes=0
    )
    assert manifest.row_count == 53
    assert archive.parquet_fingerprint(contract, tmp_path / manifest.file_name)[0] == 53
    now_fp = db.execute(archive.pg_fingerprint_sql(contract), (W1, W1 + WEEK)).fetchone()[0]
    assert f"{archive.FINGERPRINT_VERSION}:{now_fp}" != manifest.content_fingerprint


def test_full_flow_reruns_and_explains_every_blocker(
    db: Any, tmp_path: Path, borg: tuple[str, dict[str, str]]
) -> None:
    repo, env = borg
    _insert(db, _rows(W1, 100) + _rows(W1 + WEEK, 100))
    contract = _contract()
    out = tmp_path / "out"
    out.mkdir()
    crashed = out / ".live_long_short_ratio-2001-01-04-r1.parquet.partial"
    crashed.write_bytes(b"left behind by a killed run")

    first = archive.run_export(
        DSN, contract, out, code_revision="t", now=NOW, max_chunks=5, reserve_bytes=0
    )
    assert len(first.done) == 2 and not first.failed
    assert not crashed.exists()
    again = archive.run_export(
        DSN, contract, out, code_revision="t", now=NOW, max_chunks=5, reserve_bytes=0
    )
    assert again.done == [] and len(again.skipped) == 2

    # An export lost before it reached Borg is superseded and exported again (r2).
    rows = archive.live_rows(db, contract)
    lost = rows[W1 + WEEK]
    (out / lost.file_name).unlink()
    redo = archive.run_export(
        DSN, contract, out, code_revision="t", now=NOW, max_chunks=5, reserve_bytes=0
    )
    assert redo.done == [f"{(W1 + WEEK):%Y-%m-%d}: 103 rows, r2"]
    states = db.execute(
        "SELECT revision, state FROM app.history_archive_datasets WHERE dataset = %s "
        "AND range_start = %s ORDER BY revision",
        (contract.dataset, W1 + WEEK),
    ).fetchall()
    assert states == [(1, "superseded"), (2, "exported")]

    archived = archive.run_archive(DSN, contract, out, repo=repo, env=env, now=NOW)
    assert len(archived.done) == 2
    assert archive.run_archive(DSN, contract, out, repo=repo, env=env, now=NOW).skipped == [
        "nothing exported to archive"
    ]

    # A corrupted archive member never becomes verified, and the local copy stays.
    name = archive.ARCHIVE_PREFIX + NOW.strftime("%Y-%m-%dT%H:%M:%S")
    target = Path(repo) / name / archive.live_rows(db, contract)[W1].file_name
    good = target.read_bytes()
    target.write_bytes(good[:-10] + b"0123456789")
    bad = archive.run_verify(DSN, contract, out, repo=repo, env=env, now=NOW, reserve_bytes=0)
    assert len(bad.failed) == 1 and "sha256" in bad.failed[0]
    assert archive.live_rows(db, contract)[W1].state == "archived"
    assert (out / archive.live_rows(db, contract)[W1].file_name).exists()
    target.write_bytes(good)
    ok = archive.run_verify(DSN, contract, out, repo=repo, env=env, now=NOW, reserve_bytes=0)
    assert not ok.failed
    verified = archive.live_rows(db, contract)
    assert {r.state for r in verified.values()} == {"verified"}
    assert not list(out.glob("*.parquet")) and len(list(out.glob("*.manifest.json"))) == 3

    cache = tmp_path / "cache"
    fetched = archive.fetch(
        DSN, contract, W1, W1 + 2 * WEEK, cache, repo=repo, env=env, reserve_bytes=0
    )
    assert sorted(p.name for p in fetched) == sorted(r.file_name for r in verified.values())
    assert archive.fetch(
        DSN, contract, W1, W1 + WEEK, cache, repo=repo, env=env, reserve_bytes=0
    ) == [cache / verified[W1].file_name]

    verdicts = {
        v.range_start: v for v in archive.run_dry_run(DSN, contract, repo=repo, env=env, now=NOW)
    }
    assert verdicts[W1.isoformat()].blockers == (
        "fence not raised to the chunk end (a separate approved step)",
    )
    _insert(db, [(W1 + timedelta(days=2), "OLD", "binance", "1", None, None)])
    verdicts = {
        v.range_start: v for v in archive.run_dry_run(DSN, contract, repo=repo, env=env, now=NOW)
    }
    assert "source changed since export; re-export" in verdicts[W1.isoformat()].blockers
    assert (
        "source changed since export; re-export" not in verdicts[(W1 + WEEK).isoformat()].blockers
    )


def test_the_reserve_refuses_export_and_fetch(
    db: Any, tmp_path: Path, borg: tuple[str, dict[str, str]]
) -> None:
    repo, env = borg
    _insert(db, _rows(W1, 10))
    contract = _contract()
    report = archive.run_export(
        DSN, contract, tmp_path, code_revision="t", now=NOW, max_chunks=5, reserve_bytes=1 << 60
    )
    assert report.done == [] and "reserve" in report.failed[0]
    assert not list(tmp_path.glob("*.parquet")) and archive.live_rows(db, contract) == {}
    archive.run_export(
        DSN, contract, tmp_path, code_revision="t", now=NOW, max_chunks=5, reserve_bytes=0
    )
    archive.run_archive(DSN, contract, tmp_path, repo=repo, env=env, now=NOW)
    archive.run_verify(DSN, contract, tmp_path, repo=repo, env=env, now=NOW, reserve_bytes=0)
    with pytest.raises(archive.ArchiveError, match="reserve"):
        archive.fetch(
            DSN, contract, W1, W1 + WEEK, tmp_path / "c", repo=repo, env=env, reserve_bytes=1 << 60
        )


def test_a_second_run_cannot_overlap_the_first(db: Any, tmp_path: Path) -> None:
    contract = _contract()
    with (
        archive.archiver_session(DSN, contract, tmp_path),
        pytest.raises(archive.ArchiveError, match="holds"),
    ):
        archive.run_export(DSN, contract, tmp_path, code_revision="t", now=NOW, max_chunks=1)


def test_open_episode_floor_runs_against_the_real_table(db: Any) -> None:
    floor = archive.open_episode_floor(db, timedelta(hours=4))
    assert floor is None or floor.tzinfo is not None


# ---------- the freeze protocol on the real lsr_history fence ----------

F1 = datetime(1995, 1, 5, tzinfo=UTC)  # a Thursday; the fence moves to F1 + WEEK
MOVE_FENCE = (
    "UPDATE app.history_archive_fences SET closed_before = %s WHERE dataset = 'lsr_history'"
)


def _insert_error(conn: psycopg.Connection[Any], ts: datetime) -> str | None:
    try:
        _insert(conn, [(ts, f"X{ts.timestamp():.0f}", "binance", "1", None, None)])
    except psycopg.Error as exc:
        if not conn.autocommit:
            conn.rollback()
        return exc.sqlstate
    return None


def test_freeze_protocol_end_to_end(
    db: Any, tmp_path: Path, borg: tuple[str, dict[str, str]]
) -> None:
    """Lock, re-verify, drop and fence in one transaction; prove that an in-flight
    writer is waited for, a new writer is held off, a late old row and a writer with an
    old snapshot are refused, the fence never moves back, and read_lsr returns the
    archived and the live half without a gap or an overlap."""
    db.execute(
        "SELECT drop_chunks('app.live_long_short_ratio', older_than => '2002-01-01'::timestamptz)"
    )
    if db.execute(
        "SELECT closed_before >= %s FROM app.history_archive_fences WHERE dataset = 'lsr_history'",
        (F1 + WEEK,),
    ).fetchone() == (True,):
        pytest.skip("the lsr_history fence of this database was already moved past 1995 by a run")
    repo, env = borg
    _insert(db, _rows(F1, 120) + _rows(F1 + WEEK, 120))
    contract = archive.LSR_CONTRACT
    out = tmp_path / "out"
    original = _source_text(db, F1, F1 + 2 * WEEK)
    archive.run_export(
        DSN, contract, out, code_revision="t", now=NOW, max_chunks=1, reserve_bytes=0
    )
    archive.run_archive(DSN, contract, out, repo=repo, env=env, now=NOW)
    archive.run_verify(DSN, contract, out, repo=repo, env=env, now=NOW, reserve_bytes=0)
    row = archive.live_rows(db, contract)[F1]
    assert row.state == "verified"
    chunk = _chunk(db, F1)

    # An in-flight writer: the SHARE lock waits for it (and times out here).
    writer = psycopg.connect(DSN)
    _insert(writer, [(F1 + timedelta(days=1), "INFLIGHT", "binance", "1", None, None)])
    locker = psycopg.connect(DSN)
    locker.execute("SET lock_timeout = '300ms'")
    with pytest.raises(psycopg.errors.LockNotAvailable):
        locker.execute("LOCK TABLE app.live_long_short_ratio IN SHARE MODE")
    locker.rollback()
    writer.rollback()

    # A writer whose REPEATABLE READ snapshot predates the protocol transaction.
    stale = psycopg.connect(DSN)
    stale.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
    stale.execute("SELECT 1").fetchone()

    # The protocol: lock, re-verify, drop exactly the chunk, raise the fence; a new
    # writer is held off for the whole transaction.
    blocked = psycopg.connect(DSN, autocommit=True)
    blocked.execute("SET lock_timeout = '300ms'")
    with locker.transaction():
        locker.execute("SET LOCAL lock_timeout = '5s'")
        locker.execute("LOCK TABLE app.live_long_short_ratio IN SHARE MODE")
        assert _insert_error(blocked, F1 + WEEK + timedelta(days=1)) == "55P03"
        current = _one(
            locker.execute(
                archive.pg_fingerprint_sql(contract), (chunk.range_start, chunk.range_end)
            ).fetchone()
        )
        assert f"{archive.FINGERPRINT_VERSION}:{current}" == row.content_fingerprint
        dropped = locker.execute(
            "SELECT drop_chunks('app.live_long_short_ratio', older_than => %s, newer_than => %s)",
            (chunk.range_end, chunk.range_start),
        ).fetchall()
        assert len(dropped) == 1
        locker.execute(
            MOVE_FENCE,
            (chunk.range_end,),
        )

    assert _insert_error(stale, F1 + timedelta(days=2)) == "40001"  # could not serialize
    stale.close()
    assert _insert_error(blocked, F1 + timedelta(days=3)) == "SH001"  # fenced
    assert _insert_error(blocked, F1 + WEEK + timedelta(days=2)) is None  # above the fence
    assert _chunk_count(db, F1) == 0  # no chunk was recreated below the fence
    with pytest.raises(psycopg.errors.RaiseException, match="never moves back"):
        db.execute(
            MOVE_FENCE,
            (F1,),
        )
    with pytest.raises(psycopg.errors.RaiseException, match="never deleted"):
        db.execute("DELETE FROM app.history_archive_fences WHERE dataset = 'lsr_history'")

    cache = tmp_path / "cache"
    relation = archive.read_lsr(
        DSN,
        F1,
        F1 + 2 * WEEK,
        cache_dir=cache,
        fetcher=lambda s, e: archive.fetch(
            DSN, contract, s, e, cache, repo=repo, env=env, reserve_bytes=0
        ),
    )
    columns = TEXT_COLUMNS.replace("::text", "")
    read = relation.query("r", f"SELECT {columns} FROM r").fetchall()  # noqa: S608
    expected = original | _source_text(db, F1 + WEEK, F1 + 2 * WEEK)
    assert {_as_text(r) for r in read} == {_as_text(r) for r in expected}
    assert len(read) == len(expected)
    for conn in (locker, blocked):
        conn.close()


def _one(row: tuple[Any, ...] | None) -> Any:
    assert row is not None
    return row[0]


def _chunk_count(conn: psycopg.Connection[Any], start: datetime) -> int:
    return sum(1 for c in archive.list_chunks(conn, archive.LSR_CONTRACT) if c.range_start == start)


def _as_text(row: tuple[Any, ...]) -> tuple[Any, ...]:
    ts = row[0].astimezone(UTC).isoformat()
    return (ts, *[None if v is None else str(v) for v in row[1:]])
