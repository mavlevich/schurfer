from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from schurfer_analytics.reporting import (
    ReportWindowNotStartedError,
    format_number,
    format_percentage,
    normalize_code_revision,
    profit_factor,
    resolve_report_until,
)


def test_normalize_code_revision_strips_and_requires_a_value() -> None:
    assert normalize_code_revision(" abc123 ") == "abc123"
    with pytest.raises(ValueError, match="revision"):
        normalize_code_revision(" ")


def test_shared_numeric_formatters_support_report_specific_missing_values() -> None:
    assert format_number(12.345, suffix=" USD") == "12.35 USD"
    assert format_number(None) == "—"
    assert format_number(None, missing="n/a") == "n/a"
    assert format_percentage(12.345) == "12.35%"
    assert format_percentage(None, missing="n/a") == "n/a"


def test_profit_factor_handles_losses_and_no_loss_sample() -> None:
    assert profit_factor((3.0, -1.0, 0.0, -2.0)) == pytest.approx(1.0)
    assert profit_factor((1.0, 2.0, 0.0)) is None
    assert profit_factor(()) is None


def test_report_window_resolver_returns_cutoff_or_raises_named_error() -> None:
    cohort = datetime(2026, 7, 29, tzinfo=UTC)
    after = cohort + timedelta(seconds=1)

    assert (
        resolve_report_until(
            None,
            after,
            cohort_start=cohort,
            report_label="test",
        )
        == after
    )
    with pytest.raises(ReportWindowNotStartedError, match="registered test cohort"):
        resolve_report_until(
            cohort,
            after,
            cohort_start=cohort,
            report_label="test",
        )
