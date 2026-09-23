from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from schurfer_analytics.abnormal_flow_replay import (
    DecisionFeatures,
    RouteKey,
    control_band_key,
)
from schurfer_analytics.abnormal_flow_snapshots import SnapshotReader, SnapshotWriter

UTC = UTC


def test_legacy_snapshot_equivalence(tmp_path: Path) -> None:
    d1 = DecisionFeatures(
        "bybit",
        "linear",
        "A",
        "v1",
        "A",
        "A",
        datetime(2026, 1, 1, 12, tzinfo=UTC),
        0.1,
        0.1,
        0.1,
        100,
        1000,
        50,
        1000,
        "2026-W01",
        None,
    )
    d2 = DecisionFeatures(
        "bybit",
        "linear",
        "B",
        "v1",
        "B",
        "B",
        datetime(2026, 1, 1, 12, tzinfo=UTC),
        0.1,
        0.1,
        0.1,
        100,
        1000,
        50,
        1000,
        "2026-W01",
        None,
    )

    episodes = [d1]

    episodes_by_band = defaultdict(list)
    controls_legacy: dict[RouteKey, list[DecisionFeatures]] = {
        ep.route_key(): [] for ep in episodes
    }
    for episode in episodes:
        band = control_band_key(episode)
        if band:
            episodes_by_band[band].append(episode)

    for candidate in [d1, d2]:
        band = control_band_key(candidate)
        if not band:
            continue
        for episode in episodes_by_band.get(band, ()):
            if candidate.decision_at == episode.decision_at and candidate.symbol == episode.symbol:
                continue
            controls_legacy[episode.route_key()].append(candidate)

    writer = SnapshotWriter(tmp_path, "fp")
    writer.append_decisions([d1, d2])
    writer.append_episodes([d1])
    writer.append_controls(d1, [d2])
    writer.publish()

    reader = SnapshotReader(tmp_path, "fp")

    snap_decisions = list(reader.iter_decisions())
    assert snap_decisions == [d1, d2]

    snap_episodes = reader.load_episodes()
    assert snap_episodes == [d1]

    snap_controls = reader.load_controls()
    pid = writer._decision_id(d1)

    assert snap_controls[pid] == controls_legacy[d1.route_key()]
