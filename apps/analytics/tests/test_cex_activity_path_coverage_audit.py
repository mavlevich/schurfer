"""Unit coverage for cex_activity_path_coverage_audit's pure classification
logic -- no Postgres needed, these functions never touch the database
themselves (that happens once in audit_path_coverage, which this file does
not exercise; see the module's own docstring for how the one real run
against production was performed and verified)."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from schurfer_analytics.cex_activity_path_coverage_audit import (
    ArtifactVerificationError,
    PathCoverageAuditReport,
    _RawMinute,
    _unresolved_requests_from_artifact,
    audit_one_request,
    load_verified_artifact,
    render_markdown,
)

if TYPE_CHECKING:
    from pathlib import Path

_ENTRY_AT = datetime(2026, 8, 20, 15, 32, tzinfo=UTC)


def _complete_minute() -> _RawMinute:
    return _RawMinute(
        price_complete=True, open_price=1.0, high_price=1.1, low_price=0.9, close_price=1.0
    )


def test_audit_one_request_reports_all_clean_as_zero_missing() -> None:
    rows = {_ENTRY_AT + timedelta(minutes=i): _complete_minute() for i in range(1440)}
    reason_counts, longest_gap = audit_one_request(rows, _ENTRY_AT)
    assert reason_counts == {}
    assert longest_gap == 0


def test_audit_one_request_classifies_row_absent() -> None:
    rows = {_ENTRY_AT + timedelta(minutes=i): _complete_minute() for i in range(1440) if i != 5}
    reason_counts, longest_gap = audit_one_request(rows, _ENTRY_AT)
    assert reason_counts == {"row_absent": 1}
    assert longest_gap == 1


def test_audit_one_request_classifies_price_incomplete() -> None:
    rows = {_ENTRY_AT + timedelta(minutes=i): _complete_minute() for i in range(1440)}
    rows[_ENTRY_AT + timedelta(minutes=5)] = _RawMinute(
        price_complete=False, open_price=1.0, high_price=1.1, low_price=0.9, close_price=1.0
    )
    reason_counts, longest_gap = audit_one_request(rows, _ENTRY_AT)
    assert reason_counts == {"price_incomplete_or_null": 1}
    assert longest_gap == 1


def test_audit_one_request_classifies_price_complete_null_as_incomplete() -> None:
    """Rows written before migration 0030 have price_complete = NULL, not
    false -- both must count as incomplete, never as a clean minute (the
    module docstring's own reasoning: NULL means "never evaluated", not
    "known complete")."""
    rows = {_ENTRY_AT + timedelta(minutes=i): _complete_minute() for i in range(1440)}
    rows[_ENTRY_AT + timedelta(minutes=5)] = _RawMinute(
        price_complete=None, open_price=1.0, high_price=1.1, low_price=0.9, close_price=1.0
    )
    reason_counts, longest_gap = audit_one_request(rows, _ENTRY_AT)
    assert reason_counts == {"price_incomplete_or_null": 1}
    assert longest_gap == 1


def test_audit_one_request_classifies_invalid_ohlc_despite_price_complete_true() -> None:
    rows = {_ENTRY_AT + timedelta(minutes=i): _complete_minute() for i in range(1440)}
    rows[_ENTRY_AT + timedelta(minutes=5)] = _RawMinute(
        price_complete=True, open_price=0.0, high_price=1.1, low_price=0.9, close_price=1.0
    )
    reason_counts, longest_gap = audit_one_request(rows, _ENTRY_AT)
    assert reason_counts == {"invalid_or_missing_ohlc": 1}
    assert longest_gap == 1


def test_audit_one_request_tracks_longest_consecutive_gap_not_just_total() -> None:
    """Two separate isolated one-minute gaps must report longest_gap=1, not
    2 -- proves the gap tracker resets between non-adjacent bad minutes
    instead of accumulating across the whole request."""
    rows = {_ENTRY_AT + timedelta(minutes=i): _complete_minute() for i in range(1440)}
    del rows[_ENTRY_AT + timedelta(minutes=5)]
    del rows[_ENTRY_AT + timedelta(minutes=500)]
    reason_counts, longest_gap = audit_one_request(rows, _ENTRY_AT)
    assert reason_counts == {"row_absent": 2}
    assert longest_gap == 1


def test_audit_one_request_reports_a_real_consecutive_outage() -> None:
    rows = {_ENTRY_AT + timedelta(minutes=i): _complete_minute() for i in range(1440)}
    for i in range(100, 108):
        del rows[_ENTRY_AT + timedelta(minutes=i)]
    reason_counts, longest_gap = audit_one_request(rows, _ENTRY_AT)
    assert reason_counts == {"row_absent": 8}
    assert longest_gap == 8


def test_unresolved_requests_from_artifact_only_includes_incomplete_24h_path() -> None:
    episodes: list[dict[str, Any]] = [
        {
            "direction": "buy",
            "signal_path": {
                "request_id": "signal:1",
                "symbol": "AUSDT",
                "entry_at": "2026-08-20T15:32:00+00:00",
                "unresolved_reason": "incomplete_24h_path",
            },
            "control_paths": [
                {
                    "request_id": "control:1:a",
                    "symbol": "AUSDT",
                    "entry_at": "2026-08-21T15:32:00+00:00",
                    "unresolved_reason": None,
                },
                {
                    "request_id": "control:1:b",
                    "symbol": "AUSDT",
                    "entry_at": "2026-08-22T15:32:00+00:00",
                    "unresolved_reason": "incomplete_24h_path",
                },
            ],
        },
        {
            "direction": "sell",
            "signal_path": {
                "request_id": "signal:2",
                "symbol": "BUSDT",
                "entry_at": "2026-08-23T15:32:00+00:00",
                "unresolved_reason": None,
            },
            "control_paths": [],
        },
    ]

    requests = _unresolved_requests_from_artifact(episodes)

    assert [r.request_id for r in requests] == ["signal:1", "control:1:b"]
    assert requests[0].kind == "signal"
    assert requests[0].direction == "buy"
    assert requests[1].kind == "control"
    assert requests[1].symbol == "AUSDT"


class TestArtifactVerification:
    """ENG-024 / audit E-04: the audit read whatever file --artifact-path named
    and rendered the result under a hardcoded fingerprint, so the September
    audit fed it a synthetic `[]` and got a clean report carrying the frozen
    artifact's identity. The report's own header was therefore not evidence of
    what had been audited."""

    @staticmethod
    def _episode() -> dict[str, object]:
        return {
            "direction": "long",
            "signal_path": {
                "symbol": "BTCUSDT",
                "entry_at": "2026-08-20T00:00:00+00:00",
                "request_id": "req-1",
                "unresolved_reason": "incomplete_24h_path",
            },
            "control_paths": [],
        }

    def _write(self, tmp_path: Path, payload: object) -> tuple[Path, str]:
        raw = json.dumps(payload).encode()
        path = tmp_path / "data.json"
        path.write_bytes(raw)
        return path, hashlib.sha256(raw).hexdigest()

    def test_reports_the_fingerprint_of_the_file_it_actually_read(self, tmp_path: Path) -> None:
        path, digest = self._write(tmp_path, [self._episode()])

        artifact = load_verified_artifact(path, expected_fingerprint=digest)

        assert artifact.fingerprint == digest
        assert artifact.path == path
        assert len(artifact.episodes) == 1

    def test_a_different_artifact_fails_closed(self, tmp_path: Path) -> None:
        path, digest = self._write(tmp_path, [self._episode()])

        with pytest.raises(ArtifactVerificationError) as exc_info:
            load_verified_artifact(path, expected_fingerprint="0" * 64)

        message = str(exc_info.value)
        assert digest in message
        assert "does not match" in message

    def test_the_synthetic_empty_list_from_the_audit_is_rejected(self, tmp_path: Path) -> None:
        """The exact input the September audit used."""
        path, digest = self._write(tmp_path, [])

        with pytest.raises(ArtifactVerificationError, match="non-empty list"):
            load_verified_artifact(path, expected_fingerprint=digest)

    @pytest.mark.parametrize(
        "payload",
        [
            {"episodes": []},
            ["not-an-object"],
            [{"direction": "long"}],
            [{"signal_path": {}}],
            [{"direction": "long", "signal_path": "not-an-object"}],
        ],
    )
    def test_wrong_schema_fails_closed_even_with_a_matching_fingerprint(
        self, tmp_path: Path, payload: object
    ) -> None:
        """A matching hash proves the bytes, not that they are this artifact."""
        path, digest = self._write(tmp_path, payload)

        with pytest.raises(ArtifactVerificationError):
            load_verified_artifact(path, expected_fingerprint=digest)

    def test_invalid_json_fails_closed(self, tmp_path: Path) -> None:
        path = tmp_path / "data.json"
        path.write_bytes(b"{not json")
        digest = hashlib.sha256(b"{not json").hexdigest()

        with pytest.raises(ArtifactVerificationError, match="not valid JSON"):
            load_verified_artifact(path, expected_fingerprint=digest)

    def test_expecting_any_artifact_still_verifies_the_shape(self, tmp_path: Path) -> None:
        """Opting out of the fingerprint comparison is for auditing a different
        artifact deliberately. It never means accepting anything at all."""
        path, _ = self._write(tmp_path, [])

        with pytest.raises(ArtifactVerificationError, match="non-empty list"):
            load_verified_artifact(path, expected_fingerprint=None)

    def test_render_reports_the_real_identity_and_flags_a_mismatch(self) -> None:
        report = PathCoverageAuditReport(
            rows=(),
            reason_totals={},
            reason_totals_by_kind={},
            longest_gap_histogram={},
            missing_minutes_by_symbol={},
            candidate_global_outage_minutes={},
            artifact_fingerprint="a" * 64,
            artifact_path="/elsewhere/not-the-frozen-artifact/data.json",
        )

        rendered = render_markdown(report, code_revision="abc1234", working_tree_dirty=False)

        assert "a" * 64 in rendered
        assert "/elsewhere/not-the-frozen-artifact/data.json" in rendered
        assert "**Not** the artifact this audit was built against" in rendered
