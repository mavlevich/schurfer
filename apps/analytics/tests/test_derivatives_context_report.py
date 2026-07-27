import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from schurfer_analytics.derivatives_context import (
    DerivativesContextProbeResult,
    DerivativesContextTarget,
)
from schurfer_analytics.derivatives_context_report import (
    DERIVATIVES_CONTEXT_REPORT_VERSION,
    DerivativesContextFilters,
    ProbeConfigurationError,
    build_parser,
    build_report,
    render_json,
    render_markdown,
)
from schurfer_analytics.derivatives_history import METHODS

GENERATED_AT = datetime(2026, 7, 27, 12, tzinfo=UTC)
SINCE = GENERATED_AT - timedelta(days=7)
TARGET = DerivativesContextTarget(
    event_id=42,
    exchange="binance",
    base="ERA",
    unified_symbol="ERA/USDT:USDT",
    market_id="ERAUSDT",
    identity_key="era",
    anchor_at=GENERATED_AT - timedelta(hours=12),
)


def _filters(
    *,
    exchanges: tuple[str, ...] = ("binance",),
    methods: tuple[str, ...] = ("funding_rate_history",),
) -> DerivativesContextFilters:
    return DerivativesContextFilters(
        since=SINCE,
        until=GENERATED_AT,
        exchanges=exchanges,
        methods=methods,
        before_minutes=240,
        after_minutes=480,
        fetch_limit=200,
        max_pages=10,
        timeout_seconds=15,
    )


def _result(
    *,
    exchange: str = "binance",
    method: str = "funding_rate_history",
    status: str = "sampled",
) -> DerivativesContextProbeResult:
    contract = next(item for item in METHODS if item.name == method)
    has_target = status != "no_target"
    return DerivativesContextProbeResult(
        exchange=exchange,
        method=method,
        capability=contract.capability,
        declared_support=status not in {"no_target", "unsupported"},
        status=status,  # type: ignore[arg-type]
        event_id=TARGET.event_id if has_target else None,
        base=TARGET.base if has_target else None,
        unified_symbol=TARGET.unified_symbol if has_target else None,
        market_id=TARGET.market_id if has_target else None,
        identity_key=TARGET.identity_key if has_target else None,
        anchor_at=TARGET.anchor_at if has_target else None,
        requested_since=TARGET.anchor_at - timedelta(minutes=240) if has_target else None,
        requested_until=TARGET.anchor_at + timedelta(minutes=480) if has_target else None,
        fetched_at=GENERATED_AT,
        returned_rows=1 if status == "sampled" else 0,
        valid_timestamp_rows=1 if status == "sampled" else 0,
        in_window_rows=1 if status == "sampled" else 0,
        invalid_rows=0,
        first_source_at=TARGET.anchor_at if status == "sampled" else None,
        last_source_at=TARGET.anchor_at if status == "sampled" else None,
    )


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"since": GENERATED_AT}, "earlier"),
        ({"exchanges": ()}, "one exchange"),
        ({"exchanges": ("binance", "binance")}, "unique"),
        ({"exchanges": ("unknown",)}, "unknown exchange"),
        ({"methods": ()}, "one method"),
        ({"methods": ("funding_rate_history", "funding_rate_history")}, "unique"),
        ({"methods": ("unknown",)}, "unknown derivatives"),
        ({"before_minutes": -1}, "non-negative"),
        ({"after_minutes": 0}, "positive"),
        ({"before_minutes": 10_081}, "cannot exceed"),
        ({"after_minutes": 10_081}, "cannot exceed"),
        ({"fetch_limit": 0}, "between 1 and 1000"),
        ({"max_pages": 0}, "between 1 and 50"),
        ({"timeout_seconds": 121}, "in \\(0, 120\\]"),
    ],
)
def test_filters_fail_closed(change: dict[str, object], message: str) -> None:
    values = {
        "since": SINCE,
        "until": GENERATED_AT,
        "exchanges": ("binance",),
        "methods": ("funding_rate_history",),
        "before_minutes": 240,
        "after_minutes": 480,
        "fetch_limit": 200,
        "max_pages": 10,
        "timeout_seconds": 15,
    }
    values.update(change)

    with pytest.raises(ProbeConfigurationError, match=message):
        DerivativesContextFilters(**values)  # type: ignore[arg-type]


def test_build_report_pins_provenance_and_coverage() -> None:
    filters = _filters(
        exchanges=("binance", "bybit"),
        methods=("funding_rate_history", "open_interest_history"),
    )
    results = (
        _result(),
        _result(method="open_interest_history", status="partial"),
        _result(exchange="bybit", status="unsupported"),
        _result(exchange="bybit", method="open_interest_history", status="no_target"),
    )

    report = build_report(
        filters,
        (TARGET,),
        results,
        generated_at=GENERATED_AT,
        code_revision=" abc123 ",
        working_tree_dirty=True,
    )

    assert report.manifest.report_version == DERIVATIVES_CONTEXT_REPORT_VERSION
    assert report.manifest.code_revision == "abc123"
    assert report.manifest.working_tree_dirty is True
    assert len(report.manifest.target_fingerprint) == 64
    assert len(report.manifest.result_fingerprint) == 64
    assert report.target_count == 1
    assert report.methods[0].declared_supported == 1
    assert report.methods[0].sampled == 1
    assert report.methods[0].unsupported == 1
    assert report.methods[1].partial == 1
    assert report.methods[1].incomplete == 0


def test_build_report_rejects_blank_revision_and_result_gaps_or_duplicates() -> None:
    with pytest.raises(ValueError, match="code revision"):
        build_report(
            _filters(),
            (TARGET,),
            (_result(),),
            generated_at=GENERATED_AT,
            code_revision=" ",
            working_tree_dirty=False,
        )
    with pytest.raises(ValueError, match="one row"):
        build_report(
            _filters(),
            (TARGET,),
            (),
            generated_at=GENERATED_AT,
            code_revision="abc123",
            working_tree_dirty=False,
        )
    with pytest.raises(ValueError, match="one row"):
        build_report(
            _filters(),
            (TARGET,),
            (_result(), replace(_result(), returned_rows=2)),
            generated_at=GENERATED_AT,
            code_revision="abc123",
            working_tree_dirty=False,
        )


def test_renderers_expose_contract_status_and_errors() -> None:
    report = build_report(
        _filters(),
        (TARGET,),
        (replace(_result(), declared_support="emulated"),),
        generated_at=GENERATED_AT,
        code_revision="abc123",
        working_tree_dirty=False,
    )

    markdown = render_markdown(report)
    payload = json.loads(render_json(report))

    assert "# Derivatives Context Coverage Probe" in markdown
    assert "declared CCXT capability is not evidence" in markdown
    assert "| funding_rate_history | 1 | 1 | 1 | 1 | 0 | 0 | 0 | 0 | 0 | 0 |" in markdown
    assert (
        "| binance | funding_rate_history | ERA (event 42) | sampled | emulated | event | 0 |"
    ) in markdown
    assert "| event | n/a | n/a / 0 | n/a |" in markdown
    assert payload["manifest"]["code_revision"] == "abc123"
    assert payload["results"][0]["status"] == "sampled"


def test_parser_supports_bounded_repeated_exchange_and_method_filters() -> None:
    args = build_parser().parse_args(
        [
            "--exchange",
            "binance",
            "--exchange",
            "bybit",
            "--method",
            "funding_rate_history",
            "--before-minutes",
            "60",
            "--after-minutes",
            "120",
            "--max-pages",
            "5",
            "--code-revision",
            "abc123",
            "--no-working-tree-dirty",
            "--format",
            "json",
        ]
    )

    assert args.exchanges == ["binance", "bybit"]
    assert args.methods == ["funding_rate_history"]
    assert args.before_minutes == 60
    assert args.after_minutes == 120
    assert args.max_pages == 5
    assert args.working_tree_dirty is False
    assert args.format == "json"
