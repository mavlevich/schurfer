"""Synthetic tests of the A2 title-shape classes. Every title and ticker is invented."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from schurfer_analytics import catalyst_a2_shape as shape

if TYPE_CHECKING:
    from pathlib import Path

PREFIX = "Binance Futures Will Launch USDⓈ-Margined"


def ms(text: str) -> int:
    return int(datetime.fromisoformat(text).replace(tzinfo=UTC).timestamp() * 1000)


IN_HOLDOUT = ms("2024-01-08T10:00:00") + 1_234
IN_DISCOVERY = ms("2025-03-03T10:00:00") + 1_234


@pytest.mark.parametrize(
    ("tail", "expected"),
    [
        ("EXAMPLEUSDT Perpetual Contract", "usdt_pair"),
        ("1000EXAMPLEUSDT and AAAUSDT Perpetual Contracts", "usdt_pair"),
        ("Example (EXAMPLE) Perpetual Contract", "paren_ticker"),
        ("Aaa (AAA) and Bbb (BBB) Perpetual Contracts", "paren_ticker"),
        ("AAA/USDT Perpetual Contract", "slash_pair"),
        ("AAA USDT Perpetual Contract", "spaced_pair"),
        ("AAAUSDC Perpetual Contract", "usdc_pair"),
        ("AAA/USDC Perpetual Contract", "usdc_pair"),
        ("Multiple Perpetual Contracts With up to 25x Leverage", "multiple_no_ticker"),
        ("ALL Composite Index Perpetual Contract", "index"),
        ("AAAUSD Perpetual Contract", "other"),
        ("Perpetual Contract (2025-06-16)", "other"),
        # Precedence: the first matching class wins.
        ("AAAUSDT and Bbb (BBB) Perpetual Contracts", "usdt_pair"),
        ("Bbb (BBB) AAA/USDT Perpetual Contracts", "paren_ticker"),
        ("Multiple AAA/USDT Perpetual Contracts", "slash_pair"),
        ("Multiple Index Perpetual Contracts", "multiple_no_ticker"),
    ],
)
def test_shape_class(tail: str, expected: str) -> None:
    assert shape.shape_class(f"{PREFIX} {tail}") == expected


def test_classes_are_the_fixed_eight_in_order() -> None:
    assert shape.CLASSES == (
        "usdt_pair",
        "paren_ticker",
        "slash_pair",
        "spaced_pair",
        "usdc_pair",
        "multiple_no_ticker",
        "index",
        "other",
    )


def write_run(root: Path, titles: list[tuple[int, str, int]]) -> None:
    (root / "inherited").mkdir(parents=True)
    (root / "receipts").mkdir()
    kept = [{"id": i, "title": t, "releaseDate": at} for i, t, at in titles]
    receipt = {"series": "binance:48", "outcome": "ok", "kept": kept}
    (root / "receipts" / "00001.json").write_text(json.dumps(receipt))
    (root / "inherited" / "binance_48.json").write_text(
        json.dumps({"series": "binance:48", "receipts": []})
    )


def test_counts_only_no_base_a2_articles_of_the_window(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_run(
        tmp_path,
        [
            (1, f"{PREFIX} Secret (SECRET) Perpetual Contract", IN_HOLDOUT),
            (2, f"{PREFIX} Multiple Perpetual Contracts", IN_HOLDOUT),
            (3, f"{PREFIX} FOOUSDT Perpetual Contract", IN_HOLDOUT),  # has a base: not counted
            (4, f"{PREFIX} Other (OTHER) Perpetual Contract", IN_DISCOVERY),  # other window
            (5, "Binance Will List Foo (FOO)", IN_HOLDOUT),  # not A2
        ],
    )
    before = sorted(p.name for p in tmp_path.rglob("*"))
    code = shape.main(["--root", str(tmp_path), "--window", "holdout", "--expect-total", "2"])
    assert code == 0
    out = capsys.readouterr().out
    assert json.loads(out) == {
        "total": 2,
        "classes": {
            "usdt_pair": 0,
            "paren_ticker": 1,
            "slash_pair": 0,
            "spaced_pair": 0,
            "usdc_pair": 0,
            "multiple_no_ticker": 1,
            "index": 0,
            "other": 0,
        },
    }
    for leaked in ("SECRET", "Secret", "Multiple", "FOO", "2024", "Binance"):
        assert leaked not in out
    assert sorted(p.name for p in tmp_path.rglob("*")) == before  # read-only


def test_total_mismatch_fails_with_counts_only(tmp_path: Path) -> None:
    write_run(tmp_path, [(1, f"{PREFIX} Secret (SECRET) Perpetual Contract", IN_HOLDOUT)])
    with pytest.raises(ValueError, match=r"total 1 differs from the expected 134") as err:
        shape.main(["--root", str(tmp_path), "--window", "holdout", "--expect-total", "134"])
    assert "SECRET" not in str(err.value)
