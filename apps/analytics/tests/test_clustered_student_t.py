from datetime import UTC, datetime

from schurfer_analytics.abnormal_flow_replay import EpisodeRecord, build_report
from schurfer_analytics.abnormal_flow_screen import AbnormalFlowContract


def test_clustered_student_t_regression() -> None:
    records = [
        EpisodeRecord(
            route_key=("b", "s", "1", "v", datetime(2026, 8, 1, tzinfo=UTC)),
            canonical_asset="BTC",
            iso_week="2026W30",
            decision_at=datetime(2026, 8, 1, tzinfo=UTC),
            net_return=1.0,
            excess=0.0,
        ),
        EpisodeRecord(
            route_key=("b", "s", "2", "v", datetime(2026, 8, 8, tzinfo=UTC)),
            canonical_asset="ETH",
            iso_week="2026W31",
            decision_at=datetime(2026, 8, 8, tzinfo=UTC),
            net_return=-1.0,
            excess=0.0,
        ),
        EpisodeRecord(
            route_key=("b", "s", "3", "v", datetime(2026, 8, 15, tzinfo=UTC)),
            canonical_asset="XRP",
            iso_week="2026W32",
            decision_at=datetime(2026, 8, 15, tzinfo=UTC),
            net_return=2.0,
            excess=0.0,
        ),
        EpisodeRecord(
            route_key=("b", "s", "4", "v", datetime(2026, 8, 22, tzinfo=UTC)),
            canonical_asset="SOL",
            iso_week="2026W33",
            decision_at=datetime(2026, 8, 22, tzinfo=UTC),
            net_return=-2.0,
            excess=0.0,
        ),
    ]
    c = AbnormalFlowContract(inference_rule="student_t_df_weeks_minus_one_v1")
    rep = build_report(c, records, unresolved_episodes=0)
    se = rep.weekly_clustered_se
    assert se is not None
    assert rep.lower_95ci_net_return is not None
    assert abs(rep.lower_95ci_net_return - (-3.182 * se)) < 1e-4
