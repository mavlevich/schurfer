from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta

import pytest
from schurfer_analytics.source_lead_identity_report import (
    COHORT_START,
    IdentityReviewFilters,
    SourceLeadIdentityReviewReport,
    build_parser,
    build_source_lead_identity_review,
    render_json,
    render_markdown,
)
from schurfer_analytics.source_lead_identity_repository import (
    SourceLeadIdentityObservation,
    map_source_lead_identity_row,
    source_lead_identity_statement,
)
from sqlalchemy.dialects import postgresql


def _row(
    capture_id: int,
    *,
    base: str = "ABC",
    source_identity_key: str | None = "gate:swap:ABC_USDT:1",
    target_exchange: str | None = "binance",
    target_identity_key: str | None = "binance:swap:ABCUSDT:1",
    target_status: str | None = "sampled",
    bid_filled: float = 50,
) -> SourceLeadIdentityObservation:
    observed_at = COHORT_START + timedelta(minutes=capture_id)
    instrument = (
        {
            "identity_key": target_identity_key,
            "market_id": f"{base}USDT",
            "unified_symbol": f"{base}/USDT:USDT",
            "market_type": "swap",
            "base_asset": base,
            "quote_asset": "USDT",
            "settle_asset": "USDT",
            "onboarded_at_ms": 1_700_000_000_000,
        }
        if target_exchange is not None
        else {}
    )
    return SourceLeadIdentityObservation(
        capture_id=capture_id,
        event_id=100 + capture_id,
        base=base,
        source_first_observed_at=observed_at,
        capture_status="complete",
        eligibility_reason="eligible",
        source_identity_key=source_identity_key,
        source_market_id=f"{base}_USDT",
        source_payload={"schema_version": 1, "identity_conflict": False},
        target_exchange=target_exchange,
        target_status=target_status,
        target_eligibility_reason="identity_unverified" if target_exchange else None,
        target_observed_at=(observed_at + timedelta(seconds=3) if target_exchange else None),
        requested_notional_usd=50 if target_exchange else None,
        target_instrument=instrument,
        target_liquidity=(
            {
                "bid_impact_bps": 2,
                "ask_impact_bps": 3,
                "bid_filled_notional_usd": bid_filled,
                "ask_filled_notional_usd": 50,
            }
            if target_status == "sampled"
            else {}
        ),
    )


def _filters() -> IdentityReviewFilters:
    return IdentityReviewFilters(
        since=COHORT_START,
        until=COHORT_START + timedelta(days=1),
    )


def _report(
    rows: tuple[SourceLeadIdentityObservation, ...],
) -> SourceLeadIdentityReviewReport:
    return build_source_lead_identity_review(
        rows,
        _filters(),
        generated_at=COHORT_START + timedelta(days=1),
        code_revision="abc123",
        working_tree_dirty=False,
    )


def test_clean_exact_route_becomes_evidence_candidate_not_approval() -> None:
    report = _report((_row(1), _row(2)))

    assert report.readiness == {
        "captures": 2,
        "eligible_complete_captures": 2,
        "review_groups": 1,
        "needs_authoritative_evidence": 1,
        "blocked_conflict": 0,
        "no_executable_target": 0,
        "registry_links_pending_review": 2,
        "approved_links_created": 0,
    }
    group = report.review_groups[0]
    assert group.review_state == "needs_authoritative_evidence"
    assert group.executable_target_exchanges == ("binance",)
    assert group.targets[0].median_round_trip_impact_bps == 5
    assert all(
        link["review_status"] == "unapproved"
        and link["evidence_url"] is None
        and link["evidence_sha256"] is None
        for link in report.registry_skeleton["links"]
    )
    assert report.manifest["registry_skeleton_is_loadable"] is False


def test_multiple_identity_versions_fail_closed_for_review() -> None:
    second = replace(
        _row(2),
        target_instrument={
            **_row(2).target_instrument,
            "identity_key": "binance:swap:ABCUSDT:2",
            "onboarded_at_ms": 1_800_000_000_000,
        },
    )
    report = _report((_row(1), second))

    assert report.review_groups[0].review_state == "blocked_conflict"
    assert "multiple_binance_identity_versions" in report.review_groups[0].review_flags
    assert report.registry_skeleton["links"] == []


def test_same_exact_identity_cannot_map_to_multiple_bases() -> None:
    collision = replace(
        _row(2, base="XYZ"),
        source_identity_key="gate:swap:ABC_USDT:1",
        target_instrument={
            **_row(2, base="XYZ").target_instrument,
            "identity_key": "binance:swap:ABCUSDT:1",
        },
    )
    report = _report((_row(1), collision))

    assert all(group.review_state == "blocked_conflict" for group in report.review_groups)
    assert any(
        "source_identity_maps_multiple_bases" in group.review_flags
        for group in report.review_groups
    )
    assert any(
        "target_binance_requires_review" in group.review_flags for group in report.review_groups
    )


def test_incomplete_depth_is_not_an_executable_route() -> None:
    report = _report((_row(1, bid_filled=49),))

    assert report.review_groups[0].review_state == "no_executable_target"
    assert report.review_groups[0].targets[0].executable_quotes == 0
    assert report.readiness["registry_links_pending_review"] == 0


def test_unlisted_secondary_venue_does_not_block_executable_route() -> None:
    unlisted = replace(
        _row(1, target_exchange="bybit", target_identity_key=None),
        target_status="excluded",
        target_eligibility_reason="target_not_listed",
        target_instrument={},
        target_liquidity={},
    )
    report = _report((_row(1), unlisted))

    group = report.review_groups[0]
    assert group.review_state == "needs_authoritative_evidence"
    assert group.executable_target_exchanges == ("binance",)
    assert [target.exchange for target in group.targets] == ["binance"]


def test_noneligible_capture_stays_in_denominator_but_not_review_queue() -> None:
    excluded = replace(
        _row(1),
        capture_status="excluded",
        eligibility_reason="gate_not_unique_first_source",
        target_exchange=None,
        target_status=None,
        target_observed_at=None,
        requested_notional_usd=None,
        target_instrument={},
        target_liquidity={},
    )
    report = _report((excluded,))

    assert report.readiness["captures"] == 1
    assert report.readiness["eligible_complete_captures"] == 0
    assert report.exclusion_reasons == {"gate_not_unique_first_source": 1}
    assert report.review_groups == ()


def test_report_is_deterministic_and_renderers_keep_safety_boundary() -> None:
    first = _report((_row(1), _row(2)))
    second = _report((_row(1), _row(2)))
    payload = json.loads(render_json(first))
    markdown = render_markdown(first)

    assert first.manifest["input_fingerprint"] == second.manifest["input_fingerprint"]
    assert payload["manifest"]["interpretation"].endswith("no_strategy_change")
    assert "never approves canonical identity" in markdown
    assert "Equal tickers are not evidence" in markdown


def test_window_and_input_integrity_fail_closed() -> None:
    with pytest.raises(ValueError, match="left-censored"):
        IdentityReviewFilters(
            since=COHORT_START - timedelta(seconds=1),
            until=COHORT_START + timedelta(days=1),
        )
    with pytest.raises(ValueError, match="earlier"):
        IdentityReviewFilters(since=COHORT_START, until=COHORT_START)
    with pytest.raises(ValueError, match="outside"):
        _report((replace(_row(1), source_first_observed_at=COHORT_START - timedelta(1)),))
    inconsistent = replace(_row(1), base="XYZ")
    with pytest.raises(ValueError, match="inconsistent source identity"):
        _report((_row(1), inconsistent))


def test_parser_requires_explicit_dirty_state() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args([])
    assert build_parser().parse_args(["--no-working-tree-dirty"]).working_tree_dirty is False


def test_repository_uses_left_join_and_maps_json_defensively() -> None:
    statement = str(
        source_lead_identity_statement(*(_filters().since, _filters().until)).compile(
            dialect=postgresql.dialect(),  # type: ignore[no-untyped-call]
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "LEFT OUTER JOIN app.source_lead_target_observations" in statement
    assert "source_lead_prospective_capture_v1" in statement

    source = {
        "capture_id": 1,
        "event_id": 2,
        "base": "abc",
        "source_first_observed_at": COHORT_START,
        "capture_status": "complete",
        "eligibility_reason": "eligible",
        "source_identity_key": None,
        "source_market_id": None,
        "source_payload": [],
        "target_exchange": None,
        "target_status": None,
        "target_eligibility_reason": None,
        "target_observed_at": None,
        "requested_notional_usd": None,
        "target_instrument": None,
        "target_liquidity": None,
    }
    mapped = map_source_lead_identity_row(source)
    assert mapped.base == "ABC"
    assert mapped.source_payload == {}
    assert mapped.target_instrument == {}
