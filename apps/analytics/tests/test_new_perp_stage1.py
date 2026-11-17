"""I25 stage 1: reducers, cut, planner, contract and fetcher. No network anywhere.

The fetcher runs against `new_perp_world`, an httpx mock transport. Every price, rate
and tick size in its bodies is a sentinel, and so is the one launch after the cut.
"""

from __future__ import annotations

import dataclasses
import gzip
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest
from schurfer_analytics import new_perp_stage1 as s1
from schurfer_analytics.catalyst_feasibility import asset

from .new_perp_world import (
    PRICE,
    SENTINELS,
    Launch,
    World,
    contract_without,
    fetcher,
    ms,
    rehash,
    run_world,
)

CUT = s1.CUT_MS
START = s1.WINDOW_START_MS
DAY = s1.DAY_MS
REPO = Path(__file__).resolve().parents[3]


def page(rows: list[dict[str, Any]], cursor: str = "") -> dict[str, Any]:
    return {"retCode": 0, "result": {"list": rows, "nextPageCursor": cursor}}


def row(symbol: str, launch: int, **extra: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "symbol": symbol,
        "contractType": "LinearPerpetual",
        "settleCoin": "USDT",
        "baseCoin": symbol[:-4],
        "status": "Trading",
        "launchTime": str(launch),
        "deliveryTime": "0",
        "priceFilter": {"tickSize": "0.00012345"},
    }
    base.update(extra)
    return base


# ---------------------------------------------------------------- the cut lives in the reducers


def test_catalog_rows_after_the_cut_leave_no_trace() -> None:
    kept = [row("AAAUSDT", START + DAY), row("BBBUSDT", CUT - 1)]
    extra = [row("POSTAUSDT", CUT), row("POSTBUSDT", CUT + 10 * DAY, deliveryTime=str(CUT + DAY))]
    plain = s1.reduce_catalog_page(page(kept))
    mixed = s1.reduce_catalog_page(page(kept + extra))
    assert plain == mixed
    assert "POST" not in json.dumps(mixed)
    assert [r["symbol"] for r in mixed["rows"]] == ["AAAUSDT", "BBBUSDT"]


def test_catalog_boundaries_of_the_window() -> None:
    rows = [
        row("OLDUSDT", START - 1, deliveryTime=str(START - 5)),
        row("LIVEUSDT", START - 1),
        row("INUSDT", START),
        row("LASTUSDT", CUT - 1, deliveryTime=str(CUT + 1)),
        row("CUTUSDT", CUT),
    ]
    out = s1.reduce_catalog_page(page(rows))
    assert [r["symbol"] for r in out["rows"]] == ["INUSDT", "LASTUSDT"]
    # a delivery on or after the cut is not recorded
    assert {r["symbol"]: r["delivery_ms"] for r in out["rows"]}["LASTUSDT"] is None
    # a row before the window keeps no date, only whether it was delivered before it
    assert out["prior"] == [
        {"symbol": "LIVEUSDT", "base": "LIVE", "delivered_before_window": False},
        {"symbol": "OLDUSDT", "base": "OLD", "delivered_before_window": True},
    ]


def test_catalog_keeps_only_listed_fields_and_no_price_filter() -> None:
    item = row(
        "AAAUSDT",
        START + DAY,
        lotSizeFilter={
            "minNotionalValue": "5",
            "qtyStep": "1",
            "minOrderQty": "1",
            "maxOrderQty": "9",
        },
        leverageFilter={"maxLeverage": "50"},
        fundingInterval=480,
        isPreListing=False,
        upperFundingRate="0.0123456789",
    )
    out = s1.reduce_catalog_page(page([item]))
    text = json.dumps(out)
    assert "0.00012345" not in text and "0.0123456789" not in text and "maxOrderQty" not in text
    assert out["rows"][0]["min_notional"] == "5"
    assert out["rows"][0]["funding_interval_min"] == 480
    assert out["rows"][0]["pre_listing_field"] is True


@pytest.mark.parametrize(
    "payload",
    [
        {"retCode": 10001, "result": {}},
        {"retCode": 0, "result": {"list": "x"}},
        page([{"symbol": "AAAUSDT", "contractType": "LinearPerpetual"}]),
        page([row("AAAUSDT", START).copy() | {"launchTime": "abc"}]),
        page([row("aaa", START)]),
    ],
)
def test_catalog_schema_mismatch_fails_closed(payload: Any) -> None:
    with pytest.raises(s1.SchemaMismatchError):
        s1.reduce_catalog_page(payload)


def test_spot_catalog_drops_post_cut_rows_and_old_dates() -> None:
    items = [
        {"symbol": "AAAUSDT", "baseCoin": "AAA", "quoteCoin": "USDT", "launchTime": str(START + 5)},
        {"symbol": "OLDUSDT", "baseCoin": "OLD", "quoteCoin": "USDT", "launchTime": str(START - 5)},
        {"symbol": "NEWUSDT", "baseCoin": "NEW", "quoteCoin": "USDT", "launchTime": str(CUT)},
        {"symbol": "NOLAUNCHUSDT", "baseCoin": "NO", "quoteCoin": "USDT"},
        {"symbol": "AAAUSDC", "baseCoin": "AAA", "quoteCoin": "USDC"},
    ]
    out = s1.reduce_spot_page(page(items))
    assert [r["symbol"] for r in out["rows"]] == ["AAAUSDT", "NOLAUNCHUSDT", "OLDUSDT"]
    old = next(r for r in out["rows"] if r["symbol"] == "OLDUSDT")
    assert old["launch_ms"] is None and old["before_window"] is True
    assert out["with_launch_time"] == 2
    assert "NEWUSDT" not in json.dumps(out)


def test_root_listing_never_names_a_catalog_symbol_or_a_stale_directory() -> None:
    html = "\n".join(
        [
            '<a href="LIVEUSDT/">LIVEUSDT/</a> 2026-11-01 02:00 -',
            '<a href="POSTUSDT/">POSTUSDT/</a> 2026-11-01 02:00 -',
            '<a href="GONEUSDT/">GONEUSDT/</a> 2025-03-04 02:00 -',
            '<a href="STALEUSDT/">STALEUSDT/</a> 2024-03-04 02:00 -',
            '<a href="NODATEUSDT/">NODATEUSDT/</a>',
            '<a href="BTC-26DEC25/">BTC-26DEC25/</a> 2025-12-01 -',
        ]
    )
    out = s1.reduce_root_listing(html, {"LIVEUSDT", "POSTUSDT"})
    assert out["candidates"] == ["GONEUSDT", "NODATEUSDT"]
    assert out["dirs"] == 5 and out["dirs_with_modified"] == 4
    assert "POSTUSDT" not in json.dumps(out)
    with pytest.raises(s1.SchemaMismatchError):
        s1.reduce_root_listing("<html>nothing</html>", set())


def test_symbol_listing_drops_names_from_the_cut_uncounted() -> None:
    names = ["2024-09-20", "2024-10-01", "2024-10-02", "2025-09-30"]
    plain = "".join(f'<a href="AAAUSDT{d}.csv.gz">x</a>' for d in names)
    after = "".join(f'<a href="AAAUSDT{d}.csv.gz">x</a>' for d in ("2025-10-01", "2026-01-01"))
    other = '<a href="BBBUSDT2024-10-05.csv.gz">x</a>'
    a = s1.reduce_symbol_listing("AAAUSDT", plain)
    b = s1.reduce_symbol_listing("AAAUSDT", plain + after + other)
    assert a == b
    assert a["first_day"] == "2024-09-20"
    assert a["days_in_window"] == ["2024-10-01", "2024-10-02", "2025-09-30"]
    assert "2025-10" not in json.dumps(b)


def test_symbol_listing_records_gaps_longer_than_thirty_days() -> None:
    days = ["2024-06-01", "2024-06-02", "2024-08-15", "2024-10-01"]
    html = "".join(f'<a href="AAAUSDT{d}.csv.gz">x</a>' for d in days)
    out = s1.reduce_symbol_listing("AAAUSDT", html)
    assert out["gaps"] == [["2024-06-02", "2024-08-15", 74], ["2024-08-15", "2024-10-01", 47]]
    with pytest.raises(s1.SchemaMismatchError):
        s1.reduce_symbol_listing("AAAUSDT", "<html>no links</html>")
    empty = s1.reduce_symbol_listing("AAAUSDT", '<a href="x">x</a>')
    assert empty["first_day"] is None and empty["days_in_window"] == []


def csv_gz(rows: list[str], header: bool = True) -> bytes:
    lines = (["timestamp,symbol,side,size,price"] if header else []) + rows
    return gzip.compress(("\n".join(lines) + "\n").encode())


def test_first_file_reads_column_one_only() -> None:
    # every other column is garbage that would fail any parser: it is never parsed
    body = csv_gz(
        [
            f"{START // 1000 + 50}.123456,@@,##,not-a-number,{PRICE}",
            f"{START // 1000 + 10}.5,@@,##,,ÿ",
            f"{START // 1000 + 90},@@",
        ]
    )
    reducer = s1.FirstFileReducer()
    for i in range(0, len(body), 7):  # split inside lines and inside the gzip frame
        reducer.feed(body[i : i + 7])
    facts = reducer.finish()
    assert facts == {
        "rows": 3,
        "min_ms": START + 10_500,
        "max_ms": START + 90_000,
        "first_ms": START + 50_123,
        "last_ms": START + 90_000,
    }
    assert PRICE not in json.dumps(facts)


def test_first_file_fails_closed() -> None:
    def reduce(body: bytes) -> dict[str, Any]:
        r = s1.FirstFileReducer()
        r.feed(body)
        return r.finish()

    with pytest.raises(s1.SchemaMismatchError, match="timestamp_after_cut"):
        reduce(csv_gz([f"{CUT // 1000}.0,x"]))
    with pytest.raises(s1.SchemaMismatchError, match="bad:timestamp"):
        reduce(csv_gz(["abc,x"]))
    with pytest.raises(s1.SchemaMismatchError, match="no_rows"):
        reduce(csv_gz([]))
    with pytest.raises(s1.SchemaMismatchError, match="truncated_gzip"):
        reduce(csv_gz([f"{START // 1000}.0,x"])[:-9])
    with pytest.raises(s1.SchemaMismatchError, match="not_gzip"):
        reduce(b"plain text, not gzip at all")


def test_kline_keeps_start_times_and_drops_rows_from_the_cut() -> None:
    rows = [[str(START), PRICE, PRICE, PRICE, PRICE, "1", PRICE], [str(CUT), PRICE] * 3 + ["1"]]
    plain = s1.reduce_kline(
        {"retCode": 0, "result": {"symbol": "AAAUSDT", "list": rows[:1]}}, "AAAUSDT", 7
    )
    both = s1.reduce_kline(
        {"retCode": 0, "result": {"symbol": "AAAUSDT", "list": rows}}, "AAAUSDT", 7
    )
    assert plain == both == {"n": 1, "oldest_ms": START, "newest_ms": START}
    assert PRICE not in json.dumps(both)
    with pytest.raises(s1.SchemaMismatchError, match="symbol"):
        s1.reduce_kline({"retCode": 0, "result": {"symbol": "BBBUSDT", "list": []}}, "AAAUSDT", 7)
    with pytest.raises(s1.SchemaMismatchError, match="row"):
        s1.reduce_kline(
            {"retCode": 0, "result": {"symbol": "AAAUSDT", "list": [[str(START)]]}}, "AAAUSDT", 7
        )


def test_funding_keeps_timestamps_and_page_boundaries_only() -> None:
    def item(t: int) -> dict[str, str]:
        return {"symbol": "AAAUSDT", "fundingRate": "0.0123456789", "fundingRateTimestamp": str(t)}

    payload = {"retCode": 0, "result": {"list": [item(START + 200), item(START + 100), item(CUT)]}}
    out = s1.reduce_funding(payload, "AAAUSDT")
    assert out == {"n": 2, "oldest_ms": START + 100, "newest_ms": START + 200, "full": False}
    assert "0.0123456789" not in json.dumps(out)
    with pytest.raises(s1.SchemaMismatchError):
        s1.reduce_funding(
            {"retCode": 0, "result": {"list": [item(START) | {"symbol": "X"}]}}, "AAAUSDT"
        )


# ---------------------------------------------------------------- clock and eligibility


def test_entry_clock_rounds_up_to_the_next_minute() -> None:
    t0 = ms("2024-11-01T10:00:00") + 1
    entry, exit_ = s1.entry_clock(t0)
    assert entry == ms("2024-11-08T10:01:00")
    assert exit_ == ms("2024-11-15T10:01:00")
    assert s1.entry_clock(ms("2024-11-01T10:00:00"))[0] == ms("2024-11-08T10:00:00")


def test_v1_lookback_and_window_cuts_at_their_exact_edges() -> None:
    first = ms("2024-10-24T00:00:00")  # E = 2024-10-31, E - 30 d = window start
    assert not s1.v1_cuts(first)["lookback_censored"]
    # E rounds up to the minute, so a first trade a hair earlier has the same entry time
    assert not s1.v1_cuts(first - s1.MINUTE_MS + 1)["lookback_censored"]
    assert s1.v1_cuts(first - s1.MINUTE_MS)["lookback_censored"]
    last = CUT - 14 * DAY  # X == the cut: the last bar ends at the cut, inside the window
    assert not s1.v1_cuts(last)["window_censored"]
    assert s1.v1_cuts(last + 1)["window_censored"]


def test_m1_classes_at_their_boundaries() -> None:
    launch = ms("2024-11-01T10:00:00")
    m = s1.MINUTE_MS
    assert s1.t0_class(launch - 5 * m, launch) == "aligned"
    assert s1.t0_class(launch - 5 * m - 1, launch) == "premarket_or_ambiguous"
    assert s1.t0_class(launch + 60 * m, launch) == "aligned"
    assert s1.t0_class(launch + 60 * m + 1, launch) == "delayed_open"
    assert s1.t0_class(launch + 24 * 60 * m, launch) == "delayed_open"
    assert s1.t0_class(launch + 24 * 60 * m + 1, launch) == "premarket_or_ambiguous"


def test_first_file_two_days_away_is_decided_without_a_download() -> None:
    launch = ms("2024-11-10T23:30:00")
    assert s1.class_from_first_day("2024-11-08", launch) == "premarket_or_ambiguous"
    assert s1.class_from_first_day("2024-11-12", launch) == "premarket_or_ambiguous"
    for near in ("2024-11-09", "2024-11-10", "2024-11-11"):
        assert s1.class_from_first_day(near, launch) is None
        assert s1.needs_first_file(near, launch)
    assert not s1.needs_first_file(None, launch)


# ---------------------------------------------------------------- funding pagination (HYP-036 rule)


def fpage(n: int, oldest: int | None, full: bool, outcome: str = "ok") -> dict[str, Any]:
    return {
        "outcome": outcome,
        "facts": {"n": n, "oldest_ms": oldest, "newest_ms": oldest, "full": full},
    }


def test_funding_short_first_page_covers_the_span() -> None:
    state = s1.funding_state([fpage(57, 1_500, False)], 1_000, 9_000)
    assert state == {"decided": True, "covered": True, "next_end": 9_000, "reason": "short_page"}


def test_funding_full_page_covers_only_from_its_oldest_row() -> None:
    pages = [fpage(200, 6_000, True)]
    state = s1.funding_state(pages, 1_000, 9_000)
    assert state["decided"] is False and state["next_end"] == 5_999
    pages.append(fpage(30, 3_000, False))
    assert s1.funding_state(pages, 1_000, 9_000)["covered"] is True
    # a full page whose oldest row reaches the span start decides it too
    assert s1.funding_state([fpage(200, 1_000, True)], 1_000, 9_000)["reason"] == "full_page"


def test_funding_gaps_are_never_covered() -> None:
    assert s1.funding_state([fpage(0, None, False)], 1_000, 9_000)["reason"] == "no_rows"
    assert (
        s1.funding_state([fpage(5, 2_000, False, "unavailable")], 1_000, 9_000)["covered"] is False
    )
    full = [fpage(200, 8_000 - 1_000 * i, True) for i in range(s1.FUNDING_MAX_PAGES)]
    state = s1.funding_state(full, 1_000, 9_000)
    assert state["reason"] == "pagination_exhausted" and state["covered"] is False


# ---------------------------------------------------------------- identity from the catalog


def phase0_of(
    rows: list[dict[str, Any]], prior: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    return {"catalog": {"rows": rows, "prior": prior or []}, "archive": {"candidates": []}}


def catalog_row(symbol: str, base: str, launch: int, delivery: int | None = None) -> dict[str, Any]:
    return {"symbol": symbol, "base": base, "launch_ms": launch, "delivery_ms": delivery}


def test_relist_and_multiplier_ambiguity_come_from_the_catalog() -> None:
    launch = ms("2024-12-01T00:00:00")
    phase0 = phase0_of(
        [
            catalog_row("RELISTUSDT", "RELIST", launch),
            catalog_row("TWINUSDT", "TWIN", launch),
            catalog_row("FIRSTUSDT", "FIRST", launch),
            catalog_row("FIRSTAGAINUSDT", "FIRST", launch + 5 * DAY),
            catalog_row("CLOSEDUSDT", "CLOSED", launch),
            catalog_row("CLOSEDAGAINUSDT", "CLOSED", launch + 9 * DAY),
        ],
        [
            {"symbol": "RELISTUSDT", "base": "RELIST", "delivered_before_window": True},
            {"symbol": "OLDTWINUSDT", "base": "TWIN", "delivered_before_window": False},
        ],
    )
    # CLOSED's first symbol was delivered before its second launched; FIRST's was not
    phase0["catalog"]["rows"][4]["delivery_ms"] = launch + 2 * DAY
    out = s1.catalog_contradictions(phase0)
    assert out == {
        "TWINUSDT": "multiplier_ambiguous",
        "RELISTUSDT": "relist",
        "FIRSTAGAINUSDT": "multiplier_ambiguous",
        "CLOSEDAGAINUSDT": "relist",
    }


def test_multiplier_prefix_does_not_make_a_new_base() -> None:
    assert asset("1000PEPE") == "PEPE"
    phase0 = phase0_of(
        [catalog_row("1000PEPEUSDT", "PEPE", START + DAY)],
        [{"symbol": "PEPEUSDT", "base": "PEPE", "delivered_before_window": False}],
    )
    assert s1.catalog_contradictions(phase0) == {"1000PEPEUSDT": "multiplier_ambiguous"}


def test_ticker_reuse_needs_old_files_or_a_gap_before_the_launch() -> None:
    launch = ms("2024-12-01T00:00:00")
    clean = {"first_day": "2024-12-01", "gaps": []}
    assert not s1.ticker_reuse(clean, launch)
    early = {"first_day": "2024-09-30", "gaps": []}  # 62 days before
    assert s1.ticker_reuse(early, launch)
    gap = {"first_day": "2024-11-25", "gaps": [["2024-11-25", "2025-01-15", 51]]}
    assert s1.ticker_reuse(gap, launch)
    after = {"first_day": "2024-12-01", "gaps": [["2024-12-05", "2025-03-01", 86]]}
    assert not s1.ticker_reuse(after, launch)


# ---------------------------------------------------------------- planner


def test_planner_refuses_symbols_outside_the_contract_and_over_the_cap() -> None:
    with pytest.raises(ValueError, match="symbol"):
        s1.first_file_url("../../etcUSDT", "2024-11-01")
    rows = [catalog_row(f"S{i}USDT", f"S{i}", START + i) for i in range(s1.MAX_LAUNCHES + 1)]
    with pytest.raises(s1.FetchStoppedError, match="plan_exceeds_cap"):
        s1.listing_requests(phase0_of(rows))
    p = phase0_of([catalog_row("AAAUSDT", "AAA", START)])
    p["archive"]["candidates"] = [f"C{i}USDT" for i in range(s1.MAX_ARCHIVE_ONLY + 1)]
    with pytest.raises(s1.FetchStoppedError, match="plan_exceeds_cap"):
        s1.listing_requests(p)


def test_request_key_is_stable_and_meta_aware() -> None:
    a = s1.catalog_request("Trading")
    assert a.key == s1.catalog_request("Trading").key
    assert a.key != s1.catalog_request("Trading", round_no=2).key
    assert a.key != s1.catalog_request("Closed").key


def test_bar_requests_carry_exact_spans_and_never_reach_the_cut() -> None:
    t0 = CUT - 16 * DAY  # V1 X = T0 + 14 d is before the cut, T0 + 21 d is after it
    cuts = s1.v1_cuts(t0)
    c = {"symbol": "AAAUSDT", "launch_ms": t0, "t0_ms": t0, **cuts}
    d3, d4, d5 = s1.bar_requests([c])
    assert dict(d3.params)["start"] == dict(d3.params)["end"] == str(cuts["entry_ms"])
    for request in (d4, d5):
        params = dict(request.params)
        assert params["interval"] == "60" and params["start"] == str(t0 + 3 * DAY)
        assert int(params["end"]) == CUT - 1
    assert d4.url == s1.MARK_URL and d5.url == s1.KLINE_URL


def test_book_requests_try_ob200_only_after_ob500_is_absent() -> None:
    t0 = ms("2024-11-01T10:00:00")
    c = {"symbol": "AAAUSDT", "launch_ms": t0, "t0_ms": t0, **s1.v1_cuts(t0)}
    first = s1.book_requests([c], {})
    assert [dict(r.meta)["file"] for r in first] == ["ob500", "ob500"]
    seen = {
        "AAAUSDT": {"entry": {"ob500": {"outcome": "absent"}}, "exit": {"ob500": {"outcome": "ok"}}}
    }
    second = s1.book_requests([c], seen)
    assert [(dict(r.meta)["label"], dict(r.meta)["file"]) for r in second] == [("entry", "ob200")]
    seen["AAAUSDT"]["entry"]["ob200"] = {"outcome": "absent"}
    assert s1.book_requests([c], seen) == []


def test_preflight_sums_the_listed_sizes_and_sets_big_files_aside() -> None:
    heads: dict[str, dict[str, Any]] = {
        "AUSDT": {"outcome": "ok", "facts": {"content_length": 100}},
        "BUSDT": {"outcome": "ok", "facts": {"content_length": s1.PER_FILE_CAP_BYTES + 1}},
        "CUSDT": {"outcome": "absent", "facts": None},
        "DUSDT": {"outcome": "ok", "facts": {"content_length": None}},
    }
    plan = s1.preflight(heads)
    assert plan["bytes"] == 100 and plan["files"] == 1 and plan["within_cap"] is True
    assert plan["over_per_file_cap"] == ["BUSDT"] and plan["no_size"] == ["CUSDT", "DUSDT"]


# ---------------------------------------------------------------- contract


def test_contract_is_deterministic_and_hash_bound() -> None:
    a, b = s1.build_contract(), s1.build_contract()
    assert a == b and a["request_list_sha256"] == s1.contract_hash(a)
    other = json.loads(json.dumps(a))
    other["limits"]["max_attempts"] += 1
    assert s1.contract_hash(other) != a["request_list_sha256"]
    assert [r["cls"] for r in a["static_requests"]] == ["C1", "C1", "C1", "C2", "C3"]


def test_contract_counts_and_caps_are_consistent() -> None:
    contract = s1.build_contract()
    classes = {c["cls"]: c for c in contract["classes"]}
    core = [c for c in classes.values() if c["cls"] != "F"]
    assert sum(c["expected_requests"] for c in core) == 1773
    assert sum(c["expected_requests"] for c in classes.values()) == 2183
    assert contract["limits"]["max_attempts"] == 3200
    for c in classes.values():
        assert c["expected_requests"] <= c["max_requests"]
    assert classes["F"]["optional"] and classes["C2"]["optional"]
    assert classes["D2"]["max_class_bytes"] <= contract["limits"]["max_bytes_total"]
    assert s1.max_attempts(["C1"]) == 2600 and s1.max_attempts(["F"]) == 3200


def test_a_struck_optional_class_changes_the_hash_and_the_go() -> None:
    struck = contract_without(("F",))
    assert struck["request_list_sha256"] != s1.build_contract()["request_list_sha256"]
    assert "F" not in s1.enabled_classes(struck) and struck["limits"]["max_attempts"] == 2600


def test_checked_in_contract_and_request_list_match_the_code() -> None:
    saved = json.loads((REPO / s1.CONTRACT_PATH).read_text())
    assert saved == s1.build_contract()
    doc = (REPO / s1.REQUEST_LIST_DOC).read_text()
    assert saved["request_list_sha256"] in doc
    assert hashlib.sha256((REPO / s1.CONTRACT_PATH).read_bytes()).hexdigest() in doc
    assert "\u2014" not in doc  # no em dash anywhere


def test_the_ledger_charges_a_crashed_attempt_its_full_cap() -> None:
    attempts = [
        {"seq": 1, "cls": "D4", "key": "a"},
        {"seq": 2, "cls": "D4", "key": "b"},
    ]
    receipts = [{"seq": 1, "key": "a", "body_bytes": 1000}]
    count, used, by_n, by_b = s1.ledger_totals(attempts, receipts)
    assert (count, by_n["D4"]) == (2, 2)
    assert used == 1000 + s1.CLASS_BY_ID["D4"].max_response_bytes == by_b["D4"]


# ---------------------------------------------------------------- the fetcher, end to end


def small_world(count: int = 12) -> World:
    world = World.evenly(count)
    world.prior = [("OLDUSDT", "OLD", ms("2024-06-01T00:00:00"))]
    world.archive_only = ["GHOSTUSDT"]
    return world


def all_text(root: Path) -> str:
    return "\n".join(p.read_text() for p in sorted(root.rglob("*.json")))


def test_a_full_run_requests_only_the_contract_and_stores_no_value(tmp_path: Path) -> None:
    world = small_world()
    summary, report = run_world(world, tmp_path / "run")
    n = len(world.launches)
    # C1 3, C2 1, C3 1, D1 launches + one archive-only, D2h, D2, then D3 D4 D5, D6, F x2
    expected = {
        "C1": 3, "C2": 1, "C3": 1, "D1": n + 1, "D2h": n, "D2": n,
        "D3": n, "D4": n, "D5": n, "D6": n, "F": 2 * n,
    }  # fmt: skip
    assert summary["attempts_by_class"] == expected
    assert summary["attempts"] == sum(expected.values())
    # no sentinel (price, rate, tick size, post-cut symbol or day) in any file of the run
    text = all_text(tmp_path / "run")
    for sentinel in SENTINELS:
        assert sentinel not in text
    # nothing was requested for the post-cut launch, and no request reaches the cut
    for request in world.requests:
        url = str(request.url)
        assert request.url.host in {"api.bybit.com", "public.bybit.com", "quote-saver.bycsi.com"}
        assert "POSTCUT" not in url
        assert all(day < "2025-10-01" for day in re.findall(r"\d{4}-\d{2}-\d{2}", url))
        for key in ("start", "end", "startTime", "endTime"):
            value = dict(request.url.params).get(key)
            assert value is None or int(value) < CUT
    assert report["launches"] == n
    # every receipt names a class, a key and an outcome; bodies are never persisted
    for path in (tmp_path / "run" / "receipts").glob("*.json"):
        receipt = json.loads(path.read_text())
        assert {"cls", "key", "outcome", "body_sha256", "body_bytes"} <= set(receipt)


def test_the_first_json_response_of_each_class_leaves_a_key_only_schema(tmp_path: Path) -> None:
    run_world(small_world(3), tmp_path / "run")
    schemas = {}
    for path in sorted((tmp_path / "run" / "receipts").glob("*.json")):
        receipt = json.loads(path.read_text())
        if "schema" in receipt:
            schemas[receipt["cls"]] = receipt["schema"]
    assert {"C1", "C2", "D3", "D4", "D5", "D6"} <= set(schemas)
    assert all(
        re.fullmatch(r"[\w.\[\]]+:[a-z]+", entry) for tree in schemas.values() for entry in tree
    )


def test_a_429_is_retried_after_retry_after_and_slows_the_run(tmp_path: Path) -> None:
    world = small_world(2)
    world.fail["instruments-info"] = [429, 500]
    f, clock = fetcher(world, tmp_path / "run")
    s1.run(f)
    assert 7.0 in clock.sleeps  # the server's Retry-After
    assert 30.0 in clock.sleeps  # then the ladder: the second failed attempt of one request
    assert f.spacing == 2.0  # doubled once, by the 429 only
    assert f.summary()["attempts_by_class"]["C1"] == 5  # 3 requests + 2 retries


def test_three_failed_attempts_make_a_request_unavailable_not_a_stop(tmp_path: Path) -> None:
    world = small_world(3)
    world.fail["funding/history"] = [500, 500, 500]
    f, _ = fetcher(world, tmp_path / "run")
    summary = s1.run(f)
    assert summary["outcomes"]["D6"]["unavailable"] == 1
    assert summary["outcomes"]["D6"]["ok"] == 2


def test_a_core_class_that_stays_unavailable_stops_the_run(tmp_path: Path) -> None:
    world = small_world(2)
    world.fail["instruments-info"] = [500, 500, 500]
    f, _ = fetcher(world, tmp_path / "run")
    with pytest.raises(s1.FetchStoppedError, match="C1"):
        s1.run(f)


def test_403_stops_without_a_retry(tmp_path: Path) -> None:
    world = small_world(2)
    world.fail["instruments-info"] = [403]
    f, clock = fetcher(world, tmp_path / "run")
    with pytest.raises(s1.FetchStoppedError, match="403"):
        s1.run(f)
    assert f.summary()["attempts"] == 1 and clock.sleeps == []


def test_a_schema_mismatch_stops_and_names_a_reason_not_a_value(tmp_path: Path) -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"a": 1}))
    )
    f = s1.Fetcher(tmp_path / "run", s1.build_contract(), client)
    with pytest.raises(s1.FetchStoppedError, match=r"schema mismatch \(envelope\)"):
        s1.run(f)
    receipt = json.loads(next((tmp_path / "run" / "receipts").glob("*.json")).read_text())
    assert receipt["outcome"] == "schema_mismatch" and receipt["schema"] == ["a:int"]


def test_the_optional_spot_catalog_never_stops_the_run(tmp_path: Path) -> None:
    world = small_world(3)
    world.spot = False  # the spot endpoint answers with an error envelope
    _, report = run_world(world, tmp_path / "run")
    assert report["spot"] == {"available": False}


def test_a_stopped_run_resumes_without_repeating_finished_requests(tmp_path: Path) -> None:
    world = small_world(5)
    world.fail["/v5/market/kline"] = [403]  # the first entry bar request
    root = tmp_path / "run"
    f, _ = fetcher(world, root)
    with pytest.raises(s1.FetchStoppedError):
        s1.run(f)
    done = len(world.requests)
    f2, _ = fetcher(world, root)
    summary = s1.run(f2)
    again = [str(r.url) for r in world.requests[done:]]
    first_pass = {
        str(r.url) + str(r.url.params) for r in world.requests[:done] if r.method == "GET"
    }
    # nothing finished before the stop is requested again, except the request that was refused
    assert len([u for u in again if "instruments-info" in u]) == 0
    assert (
        sum(
            1
            for r in world.requests[done:]
            if r.method == "GET" and (str(r.url) + str(r.url.params)) in first_pass
        )
        <= 1
    )
    assert summary["attempts"] == len(world.requests)


def test_an_interrupted_phase_zero_starts_a_new_round(tmp_path: Path) -> None:
    world = small_world(3)
    world.fail["public.bybit.com/trading/"] = [403]  # the root listing, after C1 and C2
    root = tmp_path / "run"
    f, _ = fetcher(world, root)
    with pytest.raises(s1.FetchStoppedError):
        s1.run(f)
    assert not (root / "phase0.json").exists()
    f2, _ = fetcher(world, root)
    s1.run(f2)
    assert len(list((root / "rounds").glob("*.json"))) == 2
    assert (root / "phase0.json").exists()
    assert f2.summary()["attempts_by_class"]["C1"] == 6  # the whole phase, twice


def test_the_run_binding_refuses_a_different_contract(tmp_path: Path) -> None:
    f, _ = fetcher(small_world(2), tmp_path / "run")
    del f
    with pytest.raises(ValueError, match="run binding differs"):
        fetcher(small_world(2), tmp_path / "run", contract_without(("F",)))


def test_a_class_outside_the_go_is_never_requested(tmp_path: Path) -> None:
    world = small_world(3)
    run_world(world, tmp_path / "run", contract_without(("F",)))
    assert not any(r.url.host == "quote-saver.bycsi.com" for r in world.requests)
    f, _ = fetcher(world, tmp_path / "other")
    f.enabled.discard("D5")
    with pytest.raises(s1.FetchStoppedError, match="not in the owner's go"):
        f.run_request(
            s1.bar_requests(
                [
                    {
                        "symbol": "AAAUSDT",
                        "launch_ms": 1,
                        "t0_ms": START,
                        "entry_ms": START,
                        "exit_ms": START + DAY,
                    }
                ]
            )[2]
        )


def test_the_attempt_cap_is_a_hard_stop(tmp_path: Path) -> None:
    tight = json.loads(json.dumps(s1.build_contract()))
    tight["limits"]["max_attempts"] = 4
    with pytest.raises(s1.BudgetExceededError, match="attempt cap"):
        run_world(small_world(2), tmp_path / "run", rehash(tight))


def test_the_preflight_stops_before_any_first_file_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(
        s1.CLASS_BY_ID, "D2", dataclasses.replace(s1.CLASS_BY_ID["D2"], max_class_bytes=10)
    )
    world = small_world(3)
    f, _ = fetcher(world, tmp_path / "run")
    with pytest.raises(s1.FetchStoppedError, match="preflight"):
        s1.run(f)
    assert not any(r.method == "GET" and r.url.path.endswith(".csv.gz") for r in world.requests)
    assert json.loads((tmp_path / "run" / "preflight.json").read_text())["within_cap"] is False


def test_a_file_over_the_per_file_cap_is_never_downloaded(tmp_path: Path) -> None:
    world = small_world(3)
    world.sizes_scale = 10**9  # every HEAD reports a huge Content-Length
    summary, report = run_world(world, tmp_path / "run")
    assert "D2" not in summary["attempts_by_class"]
    assert report["stop_codes"][0] == "not_reconstructible"


def test_the_first_file_stream_is_cut_at_the_response_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(
        s1.CLASS_BY_ID, "D2", dataclasses.replace(s1.CLASS_BY_ID["D2"], max_response_bytes=40)
    )
    world = small_world(2)
    summary, _ = run_world(world, tmp_path / "run")
    assert summary["outcomes"]["D2"] == {"too_large": 2}


def test_the_checked_in_stage1_pipeline_makes_no_request_outside_the_mock(tmp_path: Path) -> None:
    # a real client would resolve the host; the mock transport has no fallback, so this
    # fails closed if any request bypassed it
    world = small_world(2)
    run_world(world, tmp_path / "run")
    assert world.requests, "the run made no request through the mock"
    assert PRICE not in all_text(tmp_path / "run")


def test_launch_defaults_are_a_healthy_launch() -> None:
    launch = Launch("AAAUSDT", "AAA", ms("2024-11-01T10:00:00"))
    assert launch.entry_bar and launch.funding_rows and launch.min_notional == "5"
