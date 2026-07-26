from __future__ import annotations

import pytest
from schurfer_analytics.reporting import (
    format_number,
    format_percentage,
    normalize_code_revision,
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
