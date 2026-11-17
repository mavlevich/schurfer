"""I25 stage 1: reducers, cut, planner, contract and fetcher. No network anywhere.

The fetcher runs against `new_perp_world`, an httpx mock transport. Every price, rate
and tick size in its bodies is a sentinel, and so is the one launch after the cut.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from schurfer_analytics import new_perp_stage1 as s1
from schurfer_analytics.catalyst_feasibility import asset

from .new_perp_world import (
    PRICE,
    ms,
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


# the cut lives in the reducers


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
        {
            "symbol": "LIVEUSDT",
            "base": "LIVE",
            "delivered_before_window": False,
            "delivery_ms": None,
        },
        {
            "symbol": "OLDUSDT",
            "base": "OLD",
            "delivered_before_window": True,
            "delivery_ms": None,
        },
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


def test_root_listing_keeps_only_directories_proven_inside_the_window() -> None:
    html = "\n".join(
        [
            '<a href="LIVEUSDT/">LIVEUSDT/</a> 2026-11-01 02:00 -',
            '<a href="POSTUSDT/">POSTUSDT/</a> 2026-11-01 02:00 -',
            '<a href="GONEUSDT/">GONEUSDT/</a> 2025-03-04 02:00 -',
            '<a href="STALEUSDT/">STALEUSDT/</a> 2024-03-04 02:00 -',
            '<a href="NODATEUSDT/">NODATEUSDT/</a>',
            '<a href="LATEUSDT/">LATEUSDT/</a> 2025-10-01 02:00 -',
            '<a href="BTC-26DEC25/">BTC-26DEC25/</a> 2025-12-01 -',
        ]
    )
    out = s1.reduce_root_listing(html, {"LIVEUSDT", "POSTUSDT"})
    # an unknown date is not allowed, and a date from the cut is not allowed
    assert out == {"candidates": ["GONEUSDT"], "modified_dates_present": True}
    with pytest.raises(s1.SchemaMismatchError):
        s1.reduce_root_listing("<html>nothing</html>", set())


def test_root_listing_output_does_not_change_when_post_cut_directories_are_added() -> None:
    """P1-1 regression: no directory count or name of the test year is kept."""
    base = [
        '<a href="GONEUSDT/">GONEUSDT/</a> 2025-03-04 02:00 -',
        '<a href="LIVEUSDT/">LIVEUSDT/</a> 2026-11-01 02:00 -',
    ]
    extra = [
        '<a href="NEWAUSDT/">NEWAUSDT/</a> 2026-02-01 02:00 -',  # not in the catalog, post-cut
        '<a href="NEWBUSDT/">NEWBUSDT/</a>',  # not in the catalog, no date
    ]
    plain = s1.reduce_root_listing("\n".join(base), {"LIVEUSDT"})
    more = s1.reduce_root_listing("\n".join(base + extra), {"LIVEUSDT"})
    assert plain == more
    assert "NEW" not in json.dumps(more) and set(more) == {"candidates", "modified_dates_present"}


def test_root_listing_without_any_date_has_no_candidates_and_says_so() -> None:
    out = s1.reduce_root_listing('<a href="AAAUSDT/">AAAUSDT/</a>', set())
    assert out == {"candidates": [], "modified_dates_present": False}


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
    one = {"retCode": 0, "result": {"symbol": "AAAUSDT", "list": rows[:1]}}
    both = {"retCode": 0, "result": {"symbol": "AAAUSDT", "list": rows}}
    plain = s1.reduce_kline(one, "AAAUSDT", 7, limit=1000)
    cut = s1.reduce_kline(both, "AAAUSDT", 7, limit=1000)
    assert plain == cut
    assert (plain["n"], plain["oldest_ms"], plain["newest_ms"]) == (1, START, START)
    assert PRICE not in json.dumps(cut)
    with pytest.raises(s1.SchemaMismatchError, match="symbol"):
        s1.reduce_kline({"retCode": 0, "result": {"symbol": "BBBUSDT", "list": []}}, "AAAUSDT", 7)
    bad = {"retCode": 0, "result": {"symbol": "AAAUSDT", "list": [[str(START)]]}}
    with pytest.raises(s1.SchemaMismatchError, match="row"):
        s1.reduce_kline(bad, "AAAUSDT", 7)


def hourly(starts: list[int], limit: int = 1000) -> dict[str, Any]:
    rows = [[str(t), PRICE, PRICE, PRICE, PRICE] for t in starts]
    return s1.reduce_kline(
        {"retCode": 0, "result": {"symbol": "AAAUSDT", "list": rows}}, "AAAUSDT", 5, limit=limit
    )


def test_hourly_series_records_the_runs_of_missing_bars_and_only_timestamps() -> None:
    """P1-2 regression: a series with two points over days is not a covered series."""
    hour = s1.HOUR_MS
    sparse = hourly([START, START + 400 * hour])
    assert sparse["runs"] == [[START + hour, 399]]
    whole = hourly([START + i * hour for i in range(10)])
    assert whole["runs"] == [] and whole["runs_truncated"] is False
    holes = hourly([START + i * hour for i in range(60) if i % 2 == 0])
    assert len(holes["runs"]) == 20 and holes["runs_truncated"] is True
    assert hourly([START, START + hour], limit=2)["full"] is True
    assert PRICE not in json.dumps(sparse)


def test_funding_keeps_timestamps_and_page_boundaries_only() -> None:
    def item(t: int) -> dict[str, str]:
        return {"symbol": "AAAUSDT", "fundingRate": "0.0123456789", "fundingRateTimestamp": str(t)}

    payload = {"retCode": 0, "result": {"list": [item(START + 200), item(START + 100), item(CUT)]}}
    out = s1.reduce_funding(payload, "AAAUSDT")
    assert out == {
        "n": 2,
        "oldest_ms": START + 100,
        "newest_ms": START + 200,
        "max_gap_ms": 100,
        "full": False,
    }
    assert "0.0123456789" not in json.dumps(out)
    bad = {"retCode": 0, "result": {"list": [item(START) | {"symbol": "X"}]}}
    with pytest.raises(s1.SchemaMismatchError):
        s1.reduce_funding(bad, "AAAUSDT")


# clock and eligibility


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


# funding pagination (HYP-036 rule)


EIGHT = 8 * s1.HOUR_MS
FSTART = START + 5 * DAY
FEND = FSTART + 19 * DAY


def fpage(
    oldest: int | None,
    newest: int | None,
    full: bool,
    *,
    n: int = 10,
    gap: int | None = EIGHT,
    outcome: str = "ok",
) -> dict[str, Any]:
    facts = {"n": n, "oldest_ms": oldest, "newest_ms": newest, "max_gap_ms": gap, "full": full}
    return {"outcome": outcome, "facts": facts}


def test_funding_short_page_with_no_gap_over_8h_across_the_span_passes_the_coarse_check() -> None:
    state = s1.funding_state([fpage(FSTART + EIGHT, FEND - 1, False)], FSTART, FEND)
    assert state == {"decided": True, "covered": True, "next_end": FEND, "reason": "no_gap_over_8h"}


def test_funding_one_row_for_the_whole_interval_is_not_coverage() -> None:
    """P1-2 regression: a short, non-empty page used to cover the whole interval."""
    one = fpage(FSTART + 9 * DAY, FSTART + 9 * DAY, False, n=1, gap=None)
    state = s1.funding_state([one], FSTART, FEND)
    assert state["decided"] is True and state["covered"] is False
    assert state["reason"] == "starts_late"
    early = fpage(FSTART + EIGHT, FSTART + EIGHT, False, n=1, gap=None)
    assert s1.funding_state([early], FSTART, FEND)["reason"] == "ends_early"
    hole = fpage(FSTART + EIGHT, FEND - 1, False, gap=3 * EIGHT)
    assert s1.funding_state([hole], FSTART, FEND)["reason"] == "gap_inside"


def test_funding_full_page_covers_only_from_its_oldest_row() -> None:
    first = fpage(FSTART + 10 * DAY, FEND - 1, True)
    state = s1.funding_state([first], FSTART, FEND)
    assert state["decided"] is False and state["next_end"] == FSTART + 10 * DAY - 1
    older = fpage(FSTART + EIGHT, FSTART + 10 * DAY - EIGHT, False)
    assert s1.funding_state([first, older], FSTART, FEND)["covered"] is True
    # the pages must join: a hole between them is a gap even if each page is dense
    far = fpage(FSTART + EIGHT, FSTART + 8 * DAY, False)
    assert s1.funding_state([first, far], FSTART, FEND)["reason"] == "gap_between"
    # a full page whose oldest row reaches the span start finishes the pagination
    reach = fpage(FSTART, FEND - 1, True)
    assert s1.funding_state([reach], FSTART, FEND)["reason"] == "no_gap_over_8h"


def test_funding_end_is_an_event_only_when_the_catalog_delivered_the_symbol() -> None:
    stops = fpage(FSTART + EIGHT, FSTART + 5 * DAY, False)
    assert s1.funding_state([stops], FSTART, FEND)["reason"] == "ends_early"
    delivered = FSTART + 5 * DAY + EIGHT
    assert s1.funding_state([stops], FSTART, FEND, delivered)["covered"] is True
    assert s1.funding_state([stops], FSTART, FEND, delivered + 1)["covered"] is False


def test_funding_unproven_or_unfinished_is_never_covered() -> None:
    empty = fpage(None, None, False, n=0, gap=None)
    assert s1.funding_state([empty], FSTART, FEND)["reason"] == "no_rows"
    failed = fpage(FSTART, FEND, False, outcome="unavailable")
    assert s1.funding_state([failed], FSTART, FEND)["covered"] is False
    full = [
        fpage(FEND - 1 - i * DAY, FEND - 1 - (i - 1) * DAY, True)
        for i in range(s1.FUNDING_MAX_PAGES)
    ]
    state = s1.funding_state(full, FSTART, FEND)
    assert state["reason"] == "pagination_exhausted" and state["covered"] is False
    assert s1.funding_state([], FSTART, FEND)["reason"] == "pending"


# identity from the catalog


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


def test_an_old_symbol_closed_inside_the_window_is_judged_by_its_delivery_date() -> None:
    """Reviewer A, P2: delivered before the launch is a relist, after it is two symbols."""
    launch = ms("2024-12-01T00:00:00")
    phase0 = phase0_of(
        [catalog_row("AUSDT", "A", launch), catalog_row("BUSDT", "B", launch)],
        [
            {
                "symbol": "1000AUSDT",
                "base": "A",
                "delivered_before_window": False,
                "delivery_ms": launch - DAY,
            },
            {
                "symbol": "1000BUSDT",
                "base": "B",
                "delivered_before_window": False,
                "delivery_ms": launch + DAY,
            },
        ],
    )
    assert s1.catalog_contradictions(phase0) == {
        "AUSDT": "relist",
        "BUSDT": "multiplier_ambiguous",
    }


def test_the_catalog_keeps_a_prior_delivery_date_only_inside_the_window() -> None:
    rows = [
        row("AAAUSDT", START - 10, deliveryTime=str(START - 5)),
        row("BBBUSDT", START - 10, deliveryTime=str(START + 5 * DAY)),
        row("CCCUSDT", START - 10, deliveryTime=str(CUT + DAY)),
    ]
    prior = {p["symbol"]: p for p in s1.reduce_catalog_page(page(rows))["prior"]}
    assert prior["AAAUSDT"] == {
        "symbol": "AAAUSDT",
        "base": "AAA",
        "delivered_before_window": True,
        "delivery_ms": None,
    }
    assert prior["BBBUSDT"]["delivery_ms"] == START + 5 * DAY
    assert prior["CCCUSDT"]["delivery_ms"] is None  # a date from the cut is never kept


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


# planner


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


# contract


def test_contract_is_deterministic_and_hash_bound() -> None:
    a, b = s1.build_contract(), s1.build_contract()
    assert a == b and a["request_list_sha256"] == s1.contract_hash(a)
    other = json.loads(json.dumps(a))
    other["go_limits"]["1"]["max_attempts"] += 1
    assert s1.contract_hash(other) != a["request_list_sha256"]
    assert [r["cls"] for r in a["static_requests"]] == ["C1", "C1", "C1", "C3"]


def test_contract_has_two_gos_with_their_own_counts_caps_and_hashes() -> None:
    contract = s1.build_contract()
    classes = {c["cls"]: c for c in contract["classes"]}
    assert set(classes) == {"C1", "C3", "D1", "D2h", "D2", "D3", "D4", "D5", "D6"}
    go1 = [c for c in classes.values() if c["go"] == 1]
    go2 = [c for c in classes.values() if c["go"] == 2]
    assert [c["cls"] for c in go1] == ["C1", "C3", "D1", "D2h"]  # metadata only, no body
    assert [c["cls"] for c in go2] == ["D2", "D3", "D4", "D5", "D6"]
    assert sum(c["expected_requests"] for c in go1) == 526
    assert sum(c["expected_requests"] for c in go2) == 1201
    assert sum(c["max_requests"] for c in go1) == 761
    assert sum(c["max_requests"] for c in go2) == 2400
    assert contract["go_limits"] == {
        "1": {"max_attempts": 1000, "max_bytes": 300 * s1.MIB},
        "2": {"max_attempts": 1700, "max_bytes": 7680 * s1.MIB},
    }
    for c in classes.values():
        assert c["expected_requests"] <= c["max_requests"]
    # no class of go 1 reads a body that holds prices: the only GET bodies are JSON metadata
    # and HTML listings; every first file and bar is in go 2
    assert {c["cls"] for c in go1 if c["method"] == "GET"} == {"C1", "C3", "D1"}
    assert classes["D2"]["max_class_bytes"] <= contract["go_limits"]["2"]["max_bytes"]
    assert "F" not in classes and "C2" not in classes  # book and spot are out of the contract
    assert s1.go_classes(1) == ("C1", "C3", "D1", "D2h")


def test_each_go_hash_covers_the_shared_rules_and_only_its_own_classes() -> None:
    base = s1.build_contract()
    assert base["go_sha256"]["1"] != base["go_sha256"]["2"]

    def changed(edit: Any) -> dict[str, Any]:
        copy: dict[str, Any] = json.loads(json.dumps(base))
        edit(copy)
        return copy

    def go2_class(c: dict[str, Any]) -> None:
        next(x for x in c["classes"] if x["cls"] == "D2")["max_class_bytes"] -= 1

    def threshold(c: dict[str, Any]) -> None:
        c["thresholds"]["m1_ambiguous_max"] = 0.2

    assert s1.go_hash(changed(go2_class), 1) == base["go_sha256"]["1"]  # go 1 unaffected
    assert s1.go_hash(changed(go2_class), 2) != base["go_sha256"]["2"]
    assert s1.go_hash(changed(threshold), 1) != base["go_sha256"]["1"]  # shared rules: both
    assert s1.go_hash(changed(threshold), 2) != base["go_sha256"]["2"]
    assert s1.go_hash(base, 1) == base["go_sha256"]["1"]


def test_checked_in_contract_and_request_list_match_the_code() -> None:
    saved = json.loads((REPO / s1.CONTRACT_PATH).read_text())
    assert saved == s1.build_contract()
    doc = (REPO / s1.REQUEST_LIST_DOC).read_text()
    assert saved["request_list_sha256"] in doc
    assert all(h in doc for h in saved["go_sha256"].values())
    assert hashlib.sha256((REPO / s1.CONTRACT_PATH).read_bytes()).hexdigest() in doc
    assert "\u2014" not in doc  # no em dash anywhere


def test_the_ledger_charges_a_crashed_attempt_its_cap_and_reserve() -> None:
    attempts = [
        {"seq": 1, "cls": "D4", "key": "a"},
        {"seq": 2, "cls": "D4", "key": "b"},
    ]
    receipts = [{"seq": 1, "key": "a", "body_bytes": 1000}]
    count, used, by_n, by_b = s1.ledger_totals(attempts, receipts)
    assert (count, by_n["D4"]) == (2, 2)
    # the crashed send is charged its response cap plus the read reserve
    assert used == 1000 + s1.reserve(s1.CLASS_BY_ID["D4"]) == by_b["D4"]
