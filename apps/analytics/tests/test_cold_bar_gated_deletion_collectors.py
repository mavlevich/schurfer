"""Coverage for the pure helpers of the gated-deletion collectors.

The Borg/DB wrappers themselves need a real repo and Postgres (the runbook's
integration test); here we test the parts that are pure: command construction,
Borg-output parsing, newest-archive selection, and env-file parsing.
"""

from __future__ import annotations

from typing import Any

import pytest
from schurfer_analytics.cold_bar_gated_deletion_collectors import (
    MIN_EXECUTE_CUTOFF_DAYS,
    borg_extract_args,
    borg_list_archives_args,
    borg_list_members_args,
    newest_bars_archive,
    parse_env_file,
    parse_short_list,
    validate_execute_cutoff,
)


def test_execute_requires_the_full_40_day_cutoff() -> None:
    # Dry-run is unbounded (deletes nothing): any cutoff is fine.
    validate_execute_cutoff(execute=False, cutoff_days=25)
    validate_execute_cutoff(execute=False, cutoff_days=1)
    # Execute at/above the buffer is allowed.
    validate_execute_cutoff(execute=True, cutoff_days=MIN_EXECUTE_CUTOFF_DAYS)
    validate_execute_cutoff(execute=True, cutoff_days=MIN_EXECUTE_CUTOFF_DAYS + 5)
    # Execute below the buffer (e.g. inheriting the reconciliation default 25) is refused.
    with pytest.raises(ValueError, match="requires --cutoff-days >= 40"):
        validate_execute_cutoff(execute=True, cutoff_days=25)
    with pytest.raises(ValueError, match="requires --cutoff-days >= 40"):
        validate_execute_cutoff(execute=True, cutoff_days=39)


def test_borg_command_builders() -> None:
    assert borg_extract_args("repo", "arc", "bars-2026-08-01.parquet") == [
        "borg",
        "extract",
        "--stdout",
        "repo::arc",
        "bars-2026-08-01.parquet",
    ]
    assert borg_list_archives_args("repo") == ["borg", "list", "--short", "repo"]
    assert borg_list_members_args("repo", "arc") == ["borg", "list", "--short", "repo::arc"]


def test_parse_short_list_drops_blanks() -> None:
    assert parse_short_list("a\n\n b \n") == frozenset({"a", "b"})
    assert parse_short_list("") == frozenset()


def test_newest_bars_archive_is_chronological_by_name() -> None:
    names = frozenset(
        {
            "db-2026-09-14T04:00:00",
            "bars-2026-09-12T04:00:00",
            "bars-2026-09-14T08:00:00",
            "bars-2026-09-13T04:00:00",
            "research-2026-09-14T04:00:00",
        }
    )
    assert newest_bars_archive(names) == "bars-2026-09-14T08:00:00"


def test_newest_bars_archive_none_when_absent() -> None:
    assert newest_bars_archive(frozenset({"db-2026-09-14T04:00:00"})) is None


def test_parse_env_file() -> None:
    text = "\n".join(
        [
            "# a comment",
            "export BORG_REPO=ssh://x/./repo",
            'BORG_PASSPHRASE="se cret"',
            "  ",
            "EMPTYLINE_IGNORED",  # no '=' -> skipped
            "QUOTED='v'",
        ]
    )
    env = parse_env_file(text)
    assert env["BORG_REPO"] == "ssh://x/./repo"
    assert env["BORG_PASSPHRASE"] == "se cret"  # noqa: S105
    assert env["QUOTED"] == "v"
    assert "EMPTYLINE_IGNORED" not in env


def test_drop_chunk_closes_the_duckdb_session_before_dropping(monkeypatch: Any) -> None:
    """2026-09-26 canary: the long-lived DuckDB session must be closed (ending its open
    Postgres transaction) before drop_chunks, and the under-lock re-check uses a fresh one."""
    from datetime import UTC, datetime

    import psycopg
    from schurfer_analytics import cold_bar_gated_deletion_collectors as mod
    from schurfer_analytics.cold_bar_gated_deletion_job import ChunkCandidate

    events: list[str] = []

    class _Duck:
        def close(self) -> None:
            events.append("close_long_lived")

    class _Conn:
        def __enter__(self) -> _Conn:
            return self

        def __exit__(self, *_: Any) -> None:
            return None

    def fake_drop(_conn: Any, **kwargs: Any) -> str:
        events.append("drop")
        assert kwargs["verify_unchanged"]() is True
        return "chunk"

    def fake_fresh(_dsn: str, day: str) -> str:
        events.append(f"fresh:{day}")
        return "fp"

    monkeypatch.setattr(psycopg, "connect", lambda *_a, **_k: _Conn())
    monkeypatch.setattr(mod, "drop_one_chunk_under_lock", fake_drop)
    monkeypatch.setattr(mod, "fresh_source_fingerprint", fake_fresh)
    collectors = object.__new__(mod.BorgDbCollectors)
    collectors._dsn = "postgresql://x"
    collectors._connection_handle = _Duck()
    candidate = ChunkCandidate(
        day="2026-08-14",
        range_start=datetime(2026, 8, 14, tzinfo=UTC),
        range_end=datetime(2026, 8, 15, tzinfo=UTC),
    )
    collectors.drop_chunk(candidate, expected_source_fingerprint="fp")
    assert events == ["close_long_lived", "drop", "fresh:2026-08-14"]
    assert collectors._connection_handle is None
