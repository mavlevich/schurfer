from __future__ import annotations

import gzip
import json
import zipfile
from decimal import Decimal
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

import pytest
from schurfer_analytics import bybit_presettlement_entry_pilot as p
from schurfer_analytics.bybit_presettlement_book_scope import FIRST
from schurfer_analytics.bybit_presettlement_source_quality import InflatedBudget

D = FIRST + 10 * 60_000


def frames() -> list[dict[str, Any]]:
    return [
        {
            "ts": D + offset,
            "cts": D + offset - 20,
            "type": "snapshot" if i == 1 else "delta",
            "data": {
                "s": "TESTUSDT",
                "u": i,
                "seq": i,
                "b": [["100", "3"]] if i == 1 else [],
                "a": [["101", "3"]] if i == 1 else [],
            },
        }
        for i, offset in enumerate(range(-500, 3500, 500), 1)
    ]


def book_file(tmp_path: Path, rows: list[dict[str, Any]]) -> Path:
    path = tmp_path / "native.zip"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as out:
        out.writestr("native.data", "\n".join(json.dumps(r) for r in rows) + "\n")
    return path


def trial() -> p.Trial:
    return p.Trial("test", p.Config("test", wait=1000), D)


def reconstructed(tmp_path: Path) -> p.Trial:
    t = trial()
    p.reconstruct(book_file(tmp_path, frames()), "TESTUSDT", "2024-10-01", [t], InflatedBudget())
    assert t.limit == 100 and t.quantity == Decimal("0.5") and t.ahead == 3
    return t


def sell(price: str, size: str, *, bt: Any = "false", rpi: Any = "0") -> dict[str, Any]:
    return {
        "at": D + 1100,
        "price": Decimal(price),
        "size": Decimal(size),
        "side": "Sell",
        "BT": bt,
        "RPI": rpi,
    }


def test_native_zip_and_csv_produce_volume_limited_partial_fill(tmp_path: Path) -> None:
    t = reconstructed(tmp_path)
    tape = tmp_path / "tape.csv.gz"
    with gzip.open(tape, "wt") as out:
        out.write("timestamp,symbol,side,size,price,BT,RPI\n")
        out.write(f"{(D + 1100) / 1000},TESTUSDT,Sell,3.25,100,false,0\n")
    p.replay_tape(tape, "TESTUSDT", "2024-10-01", [t], InflatedBudget())
    r = t.outcome()
    assert r["primary"] == r["unknown_eligible_sensitivity"] == "partial"
    assert r["native_order_sizing"] == "unverified"
    assert "price" not in json.dumps(r) and "quantity" not in json.dumps(r)


def test_better_bid_does_not_advance_our_queue_and_trade_through_is_not_full(
    tmp_path: Path,
) -> None:
    t = reconstructed(tmp_path)
    t.consume(sell("100.1", "999"))
    assert t.assumed == 0 and t.counts["sell_above_limit"] == 1
    t.consume(sell("100", "3.1"))
    assert t.outcome()["primary"] == "partial"
    t.consume(sell("99.9", "0.1"))
    assert t.outcome()["primary"] == "partial"
    assert t.counts["sell_below_limit"] == 1
    t.consume(sell("100", "0.3"))
    assert t.outcome()["primary"] == "full"


def test_unknown_eligibility_is_unresolved_not_observed_no_fill(tmp_path: Path) -> None:
    t = reconstructed(tmp_path)
    t.consume(sell("100", "5", bt=None))
    assert t.outcome()["primary"] == "unidentified_trade_class"
    assert t.outcome()["excluded_unknown_case"] == "none"
    assert t.outcome()["unknown_eligible_sensitivity"] == "full"


def test_explicit_special_flag_is_excluded_even_when_other_flag_is_missing(tmp_path: Path) -> None:
    t = reconstructed(tmp_path)
    t.consume(sell("100", "999", bt=None, rpi="1"))
    assert t.outcome()["unknown_eligible_sensitivity"] == "none"
    assert t.counts["special"] == 1


def test_buy_print_and_arrival_expiry_ties_never_advance_buy_queue(tmp_path: Path) -> None:
    t = reconstructed(tmp_path)
    r = sell("100", "999")
    r["side"] = "Buy"
    t.consume(r)
    for at in (t.arrival, t.end):
        r = sell("100", "999")
        r["at"] = at
        t.consume(r)
    assert t.assumed == 0 and t.counts["clock_ties"] == 2


def test_engine_timestamp_cannot_make_future_publication_available_at_decision(
    tmp_path: Path,
) -> None:
    rows = frames()
    extra = {
        "ts": D + 10,
        "cts": D - 10,
        "type": "delta",
        "data": {"s": "TESTUSDT", "u": 3, "seq": 3, "b": [["100.1", "9"]], "a": []},
    }
    rows.insert(2, extra)
    for i, row in enumerate(rows, 1):
        row["data"]["u"] = row["data"]["seq"] = i
    t = trial()
    p.reconstruct(book_file(tmp_path, rows), "TESTUSDT", "2024-10-01", [t], InflatedBudget())
    assert t.limit == 100


@pytest.mark.parametrize(
    "bad", ["gap", "truncated", "reset", "same_id_conflict", "future_engine", "stale_engine_at_end"]
)
def test_bad_wait_path_remains_in_denominator_as_incomplete(tmp_path: Path, bad: str) -> None:
    rows = frames()
    if bad == "gap":
        rows = [*rows[:4], {**rows[-1], "ts": D + 4000, "cts": D + 3980}]
    elif bad == "truncated":
        rows = rows[:5]
    elif bad == "reset":
        rows[4]["type"] = "snapshot"
        rows[4]["data"]["b"], rows[4]["data"]["a"] = [["100", "3"]], [["101", "3"]]
    elif bad == "same_id_conflict":
        rows[4]["data"]["u"] = rows[3]["data"]["u"]
        rows[4]["data"]["seq"] = rows[3]["data"]["seq"]
        rows[4]["data"]["b"] = [["100", "1"]]
    elif bad == "future_engine":
        rows[4]["cts"] = rows[4]["ts"] + 1
    else:
        for row in rows:
            row["cts"] = D - 520
    t = trial()
    p.reconstruct(book_file(tmp_path, rows), "TESTUSDT", "2024-10-01", [t], InflatedBudget())
    t.consume(sell("100", "999"))
    assert t.outcome()["primary"] == "incomplete_book_data"


def test_post_only_rejection_does_not_need_the_later_wait_path(tmp_path: Path) -> None:
    rows = frames()[:4]
    rows[3]["data"]["b"] = [["100", "0"], ["99", "3"]]
    rows[3]["data"]["a"] = [["101", "0"], ["100", "3"]]
    rows += [{**rows[-1], "ts": D + 4000, "cts": D + 3980}]
    t = trial()
    p.reconstruct(book_file(tmp_path, rows), "TESTUSDT", "2024-10-01", [t], InflatedBudget())
    assert t.outcome()["primary"] == "post_only_rejected"


def test_unpublished_contract_is_rejected_before_any_native_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(p, "clean_revision", lambda: "synthetic")
    monkeypatch.setattr("subprocess.check_output", lambda *a, **k: b"different registration")
    with pytest.raises(ValueError, match="not published"):
        p.pilot(tmp_path / "nonexistent-native-files")


def test_inside_and_beyond_old_quotes_are_diagnostics_not_source_rejections(tmp_path: Path) -> None:
    from schurfer_analytics.bybit_presettlement_source_quality import inspect_pair

    book = book_file(tmp_path, frames())
    tape = tmp_path / "diagnostic.csv.gz"
    with gzip.open(tape, "wt") as out:
        out.write("timestamp,symbol,side,size,price\n")
        out.write(f"{(D + 50) / 1000},TESTUSDT,Buy,1,100.5\n")
        out.write(f"{(D + 60) / 1000},TESTUSDT,Buy,1,99\n")
    report = inspect_pair(book, tape, "TESTUSDT", InflatedBudget(), "2024-10-01")
    counts = report["strict_prior_quote_alignment"]
    assert counts["violates_prior_quote"] == 2
    assert counts["inside_prior_engine_spread"] == 1
    assert counts["opposite_or_at_prior_engine_quote"] == 1
