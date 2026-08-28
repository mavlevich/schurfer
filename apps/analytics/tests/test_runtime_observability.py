from __future__ import annotations

from typing import TYPE_CHECKING

import schurfer_analytics.runtime_observability as runtime_observability

if TYPE_CHECKING:
    import pytest


def test_report_phase_writes_sanitized_memory_telemetry_to_stderr(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(runtime_observability, "peak_rss_mib", lambda: 321.25)

    runtime_observability.log_report_phase(
        "liquid_taker",
        "dataset_built",
        episodes=1152,
        decisions=45344,
    )

    assert capsys.readouterr().err == (
        "research_report_phase report=liquid_taker phase=dataset_built "
        "peak_rss_mib=321.2 decisions=45344 episodes=1152\n"
    )
