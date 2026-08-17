from datetime import UTC, datetime, timedelta

from schurfer_analytics.momentum_universe_identity_classifier import (
    AMBIGUOUS_ONBOARD_DELTA,
    CLOSE_ONBOARD_DELTA,
    RECENT_LISTING_WINDOW,
    CandidateInstrument,
    classify,
)

RESOLVED_AT = datetime(2026, 8, 17, tzinfo=UTC)
MARKET_TYPE = "linear_usdt_perpetual"


def _instrument(
    exchange: str,
    base: str,
    onboarded_at: datetime,
    *,
    native_market_id: str | None = None,
    canonical_market_type: str = MARKET_TYPE,
) -> CandidateInstrument:
    market_id = native_market_id or f"{base}USDT"
    onboarded_at_ms = int(onboarded_at.timestamp() * 1000)
    return CandidateInstrument(
        exchange=exchange,
        native_market_id=market_id,
        base=base,
        canonical_market_type=canonical_market_type,
        identity_key=f"{exchange}:{canonical_market_type}:{market_id}:{onboarded_at_ms}",
        onboarded_at=onboarded_at,
    )


def test_single_venue_base_produces_no_cluster() -> None:
    clusters = classify(
        {"bybit": (_instrument("bybit", "BTC", RESOLVED_AT - timedelta(days=500)),)},
        resolved_at=RESOLVED_AT,
    )
    assert clusters == ()


def test_two_established_venues_confirmed_from_bare_ticker_match() -> None:
    # Real prod shape (BTC): both sides onboarded long ago, hundreds of days
    # apart from each other -- still confirmed, per the accepted v1 tradeoff.
    clusters = classify(
        {
            "bybit": (_instrument("bybit", "BTC", RESOLVED_AT - timedelta(days=2320)),),
            "binance": (_instrument("binance", "BTC", RESOLVED_AT - timedelta(days=2508)),),
        },
        resolved_at=RESOLVED_AT,
    )
    assert len(clusters) == 1
    cluster = clusters[0]
    assert cluster.cluster_key == f"BTC:{MARKET_TYPE}"
    assert cluster.base == "BTC"
    assert len(cluster.members) == 2
    assert {member.match_status for member in cluster.members} == {"confirmed"}


def test_recent_close_delta_is_candidate_not_confirmed() -> None:
    # Matches the foundation doc's own worked example verbatim: close
    # onboarding dates on a bare ticker match is a candidate, not proof.
    listed_at = RESOLVED_AT - timedelta(days=2)
    clusters = classify(
        {
            "bybit": (_instrument("bybit", "NEWTOK", listed_at),),
            "binance": (_instrument("binance", "NEWTOK", listed_at + timedelta(days=1)),),
        },
        resolved_at=RESOLVED_AT,
    )
    (cluster,) = clusters
    assert {member.match_status for member in cluster.members} == {"candidate"}
    assert all("close cross-listing" in member.match_reason for member in cluster.members)


def test_recent_delta_exactly_at_close_boundary_is_candidate() -> None:
    listed_at = RESOLVED_AT - timedelta(days=2)
    clusters = classify(
        {
            "bybit": (_instrument("bybit", "EDGE", listed_at),),
            "binance": (_instrument("binance", "EDGE", listed_at - CLOSE_ONBOARD_DELTA),),
        },
        resolved_at=RESOLVED_AT,
    )
    (cluster,) = clusters
    assert {member.match_status for member in cluster.members} == {"candidate"}


def test_recent_moderate_delta_is_insufficient_evidence() -> None:
    listed_at = RESOLVED_AT - timedelta(days=5)
    clusters = classify(
        {
            "bybit": (_instrument("bybit", "MIDTOK", listed_at),),
            "binance": (_instrument("binance", "MIDTOK", listed_at - timedelta(days=20)),),
        },
        resolved_at=RESOLVED_AT,
    )
    (cluster,) = clusters
    bybit_member = next(m for m in cluster.members if m.exchange == "bybit")
    assert bybit_member.match_status == "insufficient_evidence"


def test_recent_far_delta_is_conflict() -> None:
    listed_at = RESOLVED_AT - timedelta(days=5)
    clusters = classify(
        {
            "bybit": (_instrument("bybit", "SQUAT", listed_at),),
            "binance": (_instrument("binance", "SQUAT", listed_at - timedelta(days=60)),),
        },
        resolved_at=RESOLVED_AT,
    )
    (cluster,) = clusters
    bybit_member = next(m for m in cluster.members if m.exchange == "bybit")
    assert bybit_member.match_status == "conflict"
    assert "no corroborating" in bybit_member.match_reason


def test_one_established_one_recent_far_delta_only_recent_member_conflicts() -> None:
    # A recent listing sharing a ticker with a long-established asset on the
    # other venue, with no timing correlation at all -- the established side
    # stays confirmed (its own history is real evidence on its own), only the
    # unexplained recent side is flagged.
    clusters = classify(
        {
            "bybit": (_instrument("bybit", "OLDCOIN", RESOLVED_AT - timedelta(days=900)),),
            "binance": (_instrument("binance", "OLDCOIN", RESOLVED_AT - timedelta(days=3)),),
        },
        resolved_at=RESOLVED_AT,
    )
    (cluster,) = clusters
    bybit_member = next(m for m in cluster.members if m.exchange == "bybit")
    binance_member = next(m for m in cluster.members if m.exchange == "binance")
    assert bybit_member.match_status == "confirmed"
    assert binance_member.match_status == "conflict"


def test_duplicate_base_within_one_exchange_forces_manual_review_for_whole_group() -> None:
    listed_at = RESOLVED_AT - timedelta(days=900)
    clusters = classify(
        {
            "bybit": (
                _instrument("bybit", "DUP", listed_at, native_market_id="DUPUSDT"),
                _instrument("bybit", "DUP", listed_at, native_market_id="DUPUSDT-2"),
            ),
            "binance": (_instrument("binance", "DUP", listed_at),),
        },
        resolved_at=RESOLVED_AT,
    )
    (cluster,) = clusters
    assert len(cluster.members) == 3
    assert {member.match_status for member in cluster.members} == {"manual_review_required"}
    # Includes the otherwise-unambiguous Binance side, not just the two Bybit rows.
    binance_member = next(m for m in cluster.members if m.exchange == "binance")
    assert binance_member.match_status == "manual_review_required"
    assert "bybit" in binance_member.match_reason


def test_three_venue_cluster_mixed_status_per_member() -> None:
    listed_at = RESOLVED_AT - timedelta(days=3)
    clusters = classify(
        {
            "bybit": (_instrument("bybit", "TRI", listed_at),),
            "binance": (_instrument("binance", "TRI", listed_at + timedelta(days=1)),),
            "okx": (_instrument("okx", "TRI", listed_at + timedelta(days=90)),),
        },
        resolved_at=RESOLVED_AT,
    )
    (cluster,) = clusters
    assert len(cluster.members) == 3
    by_exchange = {member.exchange: member for member in cluster.members}
    # bybit and binance land within CLOSE_ONBOARD_DELTA of each other.
    assert by_exchange["bybit"].match_status == "candidate"
    assert by_exchange["binance"].match_status == "candidate"
    # okx's nearest neighbor is 90 days away -- past AMBIGUOUS_ONBOARD_DELTA.
    assert by_exchange["okx"].match_status == "conflict"


def test_not_same_asset_is_never_produced() -> None:
    # Sweep a range of deltas across the established/recent boundary and
    # confirm the classifier never reaches for a status it has no evidence
    # to support.
    seen_statuses: set[str] = set()
    for offset_days in (0, 1, 7, 8, 29, 30, 31, 89, 90, 91, 500):
        bybit_onboarded_at = RESOLVED_AT - timedelta(days=offset_days)
        binance_onboarded_at = RESOLVED_AT - timedelta(days=95)
        clusters = classify(
            {
                "bybit": (_instrument("bybit", "SWEEP", bybit_onboarded_at),),
                "binance": (_instrument("binance", "SWEEP", binance_onboarded_at),),
            },
            resolved_at=RESOLVED_AT,
        )
        for cluster in clusters:
            seen_statuses.update(member.match_status for member in cluster.members)
    assert "not_same_asset" not in seen_statuses


def test_mismatched_exchange_key_raises() -> None:
    import pytest

    with pytest.raises(ValueError, match="does not match"):
        classify(
            {"bybit": (_instrument("binance", "BTC", RESOLVED_AT - timedelta(days=900)),)},
            resolved_at=RESOLVED_AT,
        )


def test_clusters_are_sorted_by_cluster_key() -> None:
    listed_at = RESOLVED_AT - timedelta(days=900)
    clusters = classify(
        {
            "bybit": (
                _instrument("bybit", "ZZZ", listed_at),
                _instrument("bybit", "AAA", listed_at),
            ),
            "binance": (
                _instrument("binance", "ZZZ", listed_at),
                _instrument("binance", "AAA", listed_at),
            ),
        },
        resolved_at=RESOLVED_AT,
    )
    assert [cluster.cluster_key for cluster in clusters] == [
        f"AAA:{MARKET_TYPE}",
        f"ZZZ:{MARKET_TYPE}",
    ]


def test_constants_are_ordered_sane() -> None:
    assert CLOSE_ONBOARD_DELTA < AMBIGUOUS_ONBOARD_DELTA < RECENT_LISTING_WINDOW
