"""Pure parts of the LSR history archive pilot: the deletion dry-run's reasons, the
bounded cache, and the guard that keeps research off the live table."""

from __future__ import annotations

import os
import re
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from schurfer_analytics import lsr_history_archive as archive

T0 = datetime(2026, 8, 27, tzinfo=UTC)
NOW = datetime(2026, 10, 4, tzinfo=UTC)
WEEK = timedelta(days=7)
FP = archive.FINGERPRINT_VERSION + ":" + "a" * 64


def _chunk(i: int) -> archive.Chunk:
    return archive.Chunk(f"c{i}", T0 + i * WEEK, T0 + (i + 1) * WEEK, 1)


def _row(
    i: int, state: str = "verified", archive_name: str = "history-lsr-x"
) -> archive.CatalogRow:
    c = _chunk(i)
    return archive.CatalogRow(
        id=i,
        state=state,
        chunk_name=c.name,
        range_start=c.range_start,
        range_end=c.range_end,
        revision=1,
        row_count=10,
        file_name=f"f{i}.parquet",
        file_bytes=1,
        file_sha256="b" * 64,
        content_fingerprint=FP,
        borg_archive=archive_name if state in ("archived", "verified") else None,
    )


def _verdicts(**overrides: object) -> list[archive.DropVerdict]:
    chunks = [_chunk(i) for i in range(4)]
    kwargs: dict[str, object] = {
        "contract": archive.LSR_CONTRACT,
        "chunks": chunks,
        "catalog": {c.range_start: _row(i) for i, c in enumerate(chunks)},
        "live_fingerprint": lambda _c: FP,
        "archives": frozenset({"history-lsr-x"}),
        "fence": chunks[-1].range_end,
        "episode_floor": None,
        "now": NOW,
    }
    kwargs.update(overrides)
    return archive.drop_verdicts(**kwargs)  # type: ignore[arg-type]


def test_a_chunk_with_every_gate_passed_is_eligible() -> None:
    verdicts = _verdicts()
    assert [v.eligible for v in verdicts] == [True, True, True, False]
    assert verdicts[3].blockers == ("inside the 14-day hot window",)


def test_every_blocker_is_named() -> None:
    chunks = [_chunk(i) for i in range(3)]
    verdicts = _verdicts(
        chunks=chunks,
        catalog={
            chunks[0].range_start: _row(0, state="archived"),
            chunks[1].range_start: _row(1, archive_name="history-lsr-gone"),
        },
        live_fingerprint=lambda c: None if c.name == "c1" else FP,
        fence=chunks[0].range_end,
        episode_floor=chunks[1].range_start,
    )
    assert verdicts[0].blockers == ("catalog state is archived, not verified",)
    assert verdicts[1].blockers == (
        "source changed since export; re-export",
        "archive history-lsr-gone is missing",
        "history below it is not contiguously verified",
        "fence not raised to the chunk end (a separate approved step)",
        f"an open pump episode's API window starts {chunks[1].range_start.isoformat()}",
    )
    assert "not exported" in verdicts[2].blockers


def test_an_unknown_archive_list_and_a_protected_window_block() -> None:
    window = (T0 + timedelta(days=1), T0 + timedelta(days=2))
    contract = replace(archive.LSR_CONTRACT, protected_windows=(window,))
    verdicts = _verdicts(contract=contract, archives=None)
    assert "Borg archive list unavailable" in verdicts[0].blockers
    assert any(b.startswith("overlaps protected window") for b in verdicts[0].blockers)
    assert not any(b.startswith("overlaps") for b in verdicts[1].blockers)


def test_a_mismatched_catalog_range_is_not_the_chunk() -> None:
    chunks = [_chunk(0)]
    row = replace(_row(0), chunk_name="other")
    verdicts = _verdicts(chunks=chunks, catalog={chunks[0].range_start: row}, fence=None)
    assert "catalog range does not match the chunk" in verdicts[0].blockers


def _put(path: Path, size: int, age: float) -> None:
    path.write_bytes(b"x" * size)
    stamp = time.time() - age
    os.utime(path, (stamp, stamp))


def test_the_cache_evicts_least_recently_used_and_keeps_what_this_read_needs(
    tmp_path: Path,
) -> None:
    _put(tmp_path / "old.parquet", 40, 300)
    _put(tmp_path / "mid.parquet", 40, 200)
    _put(tmp_path / "new.parquet", 40, 100)
    # 120 cached + 50 needed against 130: the least recently used file that this read
    # does not need (mid; old is kept) goes, and nothing more.
    archive._make_room(tmp_path, 50, 130, keep={tmp_path / "old.parquet"})
    assert sorted(p.name for p in tmp_path.glob("*.parquet")) == ["new.parquet", "old.parquet"]
    with pytest.raises(archive.ArchiveError, match="exceeds"):
        archive._make_room(tmp_path, 101, 100, keep=set())
    with pytest.raises(archive.ArchiveError, match="do not fit"):
        archive._make_room(
            tmp_path, 30, 100, keep={tmp_path / "old.parquet", tmp_path / "new.parquet"}
        )


def test_row_text_is_the_same_construction_in_both_engines() -> None:
    pg = archive.pg_row_text(archive.LSR_CONTRACT)
    duck = archive.duck_row_text(archive.LSR_CONTRACT)
    assert pg.count("CASE WHEN") == duck.count("CASE WHEN") == len(archive.LSR_CONTRACT.columns)
    assert "extract(epoch FROM ts)" in pg and "epoch_us(ts)" in duck
    assert "octet_length" in pg and "strlen" in duck  # bytes in both, not characters


def test_no_direct_lsr_readers() -> None:
    """Research reads the table only through read_lsr, so a dropped chunk can never be
    silently missing from a report that queried PostgreSQL directly."""
    package = Path(archive.__file__).parent
    offenders = [
        str(path.relative_to(package))
        for path in package.rglob("*.py")
        if path.name != "lsr_history_archive.py"
        and re.search(r"live_long_short_ratio", path.read_text())
    ]
    assert offenders == []


def test_no_prune_rule_reaches_history_archives() -> None:
    """A history archive is the only copy once its chunk is dropped, so no `borg prune`
    in the offsite job may ever match its name."""
    from fnmatch import fnmatch

    script = Path(archive.__file__).parents[3] / "infra/scripts/offsite-backup.sh"
    globs = re.findall(r"borg prune --glob-archives '([^']+)'", script.read_text())
    assert globs  # the parse found the rules it guards against
    name = archive.ARCHIVE_PREFIX + "2026-10-04T00:00:00"
    assert [g for g in globs if fnmatch(name, g)] == []
