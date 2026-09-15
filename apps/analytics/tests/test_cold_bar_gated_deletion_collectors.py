"""Coverage for the pure helpers of the gated-deletion collectors.

The Borg/DB wrappers themselves need a real repo and Postgres (the runbook's
integration test); here we test the parts that are pure: command construction,
Borg-output parsing, newest-archive selection, and env-file parsing.
"""

from __future__ import annotations

from schurfer_analytics.cold_bar_gated_deletion_collectors import (
    borg_extract_args,
    borg_list_archives_args,
    borg_list_members_args,
    newest_bars_archive,
    parse_env_file,
    parse_short_list,
)


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
