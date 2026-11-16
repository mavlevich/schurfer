from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from schurfer_analytics import bybit_presettlement_economics as b3
from schurfer_analytics import catalyst_feasibility as cf

if TYPE_CHECKING:
    from pathlib import Path

DAY = cf.DAY_MS


def ms(text: str) -> int:
    return int(datetime.fromisoformat(text).replace(tzinfo=UTC).timestamp() * 1000)


IN_DISCOVERY = ms("2025-03-03T10:00:00") + 1_234
IN_HOLDOUT = ms("2024-01-08T10:00:00") + 1_234
BLIND = ms("2026-10-05T10:00:00") + 1_234
TEST_YEAR = ms("2026-01-05T10:00:00") + 1_234
TOO_OLD = ms("2022-12-01T10:00:00") + 1_234


def binance_page(articles: list[dict[str, Any]]) -> dict[str, Any]:
    return {"code": "000000", "data": {"catalogs": [{"catalogId": 48, "articles": articles}]}}


def bybit_page(items: list[dict[str, Any]], cursor: str = "") -> dict[str, Any]:
    return {"retCode": 0, "result": {"list": items, "nextPageCursor": cursor}}


def upbit_iso(at: int, offset_ms: int = 0) -> str:
    return datetime.fromtimestamp((at + offset_ms) / 1000, UTC).isoformat()


# ---------------------------------------------------------------- window cut


def test_cut_drops_items_outside_windows_before_reading_them() -> None:
    page = binance_page(
        [
            {"id": 3, "title": "Binance Will List Blind (ZZZBLIND)", "releaseDate": BLIND},
            {"id": 2, "title": "Binance Will List Test (ZZZTEST)", "releaseDate": TEST_YEAR},
            {"id": 1, "title": "Binance Will List Kept (KEPT)", "releaseDate": IN_DISCOVERY},
        ]
    )
    out = cf.cut_page("binance", page)
    assert [a["id"] for a in out["kept"]] == [1]
    assert out["dropped"] == 2
    assert not out["oldest_reached"]
    text = json.dumps(out)
    assert "ZZZBLIND" not in text
    assert "ZZZTEST" not in text


def test_cut_marks_the_oldest_page_terminal() -> None:
    out = cf.cut_page(
        "binance",
        binance_page([{"id": 1, "title": "Binance Will List Old (OLD)", "releaseDate": TOO_OLD}]),
    )
    assert out["oldest_reached"]
    assert out["kept"] == []


def test_a_pinned_old_notice_does_not_end_the_series() -> None:
    page = binance_page(
        [
            {"id": 1, "title": "Binance Will List Old (OLD)", "releaseDate": TOO_OLD},
            {"id": 9, "title": "Binance Will List Kept (KEPT)", "releaseDate": IN_DISCOVERY},
        ]
    )
    out = cf.cut_page("binance", page)
    assert not out["oldest_reached"]  # only the page's last item sets the boundary
    assert [a["id"] for a in out["kept"]] == [9]


def test_instruments_keep_no_status_and_no_late_symbols() -> None:
    items = [
        {
            "symbol": "LATEUSDT",
            "contractType": "LinearPerpetual",
            "settleCoin": "USDT",
            "status": "Trading",
            "launchTime": str(BLIND),
            "deliveryTime": "0",
        },
        {
            "symbol": "GONEUSDT",
            "contractType": "LinearPerpetual",
            "settleCoin": "USDT",
            "status": "Closed",
            "launchTime": str(IN_HOLDOUT),
            "deliveryTime": str(BLIND),
        },
        {
            "symbol": "OLDUSDT",
            "contractType": "LinearPerpetual",
            "settleCoin": "USDT",
            "status": "Closed",
            "launchTime": str(IN_HOLDOUT),
            "deliveryTime": str(IN_DISCOVERY),
        },
        {
            "symbol": "BTC-26DEC25",
            "contractType": "LinearFutures",
            "settleCoin": "USDT",
            "status": "Trading",
            "launchTime": str(IN_HOLDOUT),
            "deliveryTime": str(BLIND),
        },
    ]
    out = cf.cut_page("bybit_instruments", bybit_page(items, "next"))
    assert out["dropped"] == 1
    assert out["next_cursor"] == "next"
    assert out["kept"] == [
        {"symbol": "GONEUSDT", "launch_ms": IN_HOLDOUT, "delivery_ms": None},
        {"symbol": "OLDUSDT", "launch_ms": IN_HOLDOUT, "delivery_ms": IN_DISCOVERY},
    ]
    assert "LATE" not in json.dumps(out)
    assert "status" not in json.dumps(out["kept"])


@pytest.mark.parametrize(
    ("source", "payload"),
    [
        ("binance", {"code": "000000", "data": {"catalogs": [{"articles": [{"id": 1}]}]}}),
        ("binance", {"code": "1", "data": {}}),
        ("bybit", {"retCode": 0, "result": {"list": [{"title": "x"}]}}),
        ("bybit", {"retCode": 10001, "result": {"list": []}}),
        ("upbit", {"success": True, "data": {"notices": [{"listed_at": "2025-01-01"}]}}),
        ("upbit", {"success": True, "data": {"list": []}}),
        ("bybit_instruments", {"retCode": 0, "result": {"list": [{"symbol": "AUSDT"}]}}),
    ],
)
def test_schema_mismatch_raises(source: str, payload: dict[str, Any]) -> None:
    with pytest.raises((KeyError, ValueError, TypeError)):
        cf.cut_page(source, payload)


@pytest.mark.parametrize(
    ("source", "bad", "reason"),
    [
        ("bybit", {"title": "ZZZBLIND"}, "missing:publishTime"),
        (
            "bybit",
            {"title": "ZZZBLIND", "publishTime": 0},
            "non_positive_or_unusable_number:publishTime",
        ),
        ("bybit", {"title": "ZZZBLIND", "publishTime": ""}, "str_not_positive_integer:publishTime"),
        ("bybit", {"title": "ZZZBLIND", "publishTime": None}, "type_NoneType:publishTime"),
        ("bybit", {"title": "ZZZBLIND", "publishTime": True}, "bool:publishTime"),
        (
            "binance",
            {"title": "ZZZBLIND", "releaseDate": -5},
            "non_positive_or_unusable_number:releaseDate",
        ),
        (
            "upbit",
            {"title": "ZZZBLIND", "first_listed_at": "x", "listed_at": 7},
            "type_int:listed_at",
        ),
    ],
)
def test_item_failure_names_the_reason_without_values(
    source: str, bad: dict[str, Any], reason: str
) -> None:
    good: dict[str, Any] = {
        "bybit": {"publishTime": IN_DISCOVERY},
        "binance": {"releaseDate": IN_DISCOVERY},
        "upbit": {"first_listed_at": upbit_iso(IN_DISCOVERY), "listed_at": upbit_iso(IN_DISCOVERY)},
    }[source]
    items = [good, good, bad]
    payload = {
        "bybit": bybit_page,
        "binance": binance_page,
        "upbit": lambda i: {"success": True, "data": {"notices": i}},
    }[source](items)
    with pytest.raises(cf.SchemaMismatchError) as caught:
        cf.cut_page(source, payload)
    assert (caught.value.index, caught.value.reason) == (2, reason)
    assert "ZZZBLIND" not in json.dumps(cf.failure_record(caught.value))


def test_envelope_failure_record_never_echoes_exception_text() -> None:
    with pytest.raises(ValueError, match="retCode") as caught:
        cf.cut_page("bybit", {"retCode": 10001, "result": {"list": []}})
    assert cf.failure_record(caught.value) == {
        "class": "ValueError",
        "stage": "envelope",
        "reason": "retCode",
    }
    assert cf.failure_record(ValueError("ZZZBLIND")) == {
        "class": "ValueError",
        "stage": "envelope",
        "reason": "other",
    }


def test_upbit_canonical_time_is_the_earliest_machine_time() -> None:
    item = {
        "id": 1,
        "title": "x",
        "first_listed_at": upbit_iso(IN_DISCOVERY),
        "listed_at": upbit_iso(IN_DISCOVERY, 3_600_000),
    }
    assert cf.canonical_ms("upbit", item) == IN_DISCOVERY


def test_key_tree_holds_no_values() -> None:
    tree = cf.key_tree(binance_page([{"id": 7, "title": "SECRET (ZZZ)", "releaseDate": BLIND}]))
    assert "data.catalogs[].articles[].title:str" in tree
    assert "SECRET" not in json.dumps(tree)
    assert str(BLIND) not in json.dumps(tree)


# ---------------------------------------------------------------- classification


@pytest.mark.parametrize(
    ("source", "series", "title", "bucket", "bases"),
    [
        ("binance", "binance:48", "Binance Will List Sonic (S)", "A1", ["S"]),
        (
            "binance",
            "binance:48",
            "Binance Will List Foo (FOO) and Binance Futures Will Launch USDⓈ-M FOOUSDT "
            "Perpetual Contract",
            "A1",
            ["FOO"],
        ),
        (
            "binance",
            "binance:48",
            "Binance Futures Will Launch USDⓈ-M 1000CATUSDT and BANANAUSDT Perpetual Contracts",
            "A2",
            ["BANANA", "CAT"],
        ),
        ("binance", "binance:48", "Binance Will Add FOO on Earn, Margin", "other", []),
        (
            "binance",
            "binance:161",
            "Binance Will Delist ANT, MULTI, VAI on 2024-02-20",
            "A3",
            ["ANT", "MULTI", "VAI"],
        ),
        (
            "binance",
            "binance:161",
            "Binance Futures Will Delist USDⓈ-M BLZUSDT Perpetual Contract",
            "A3",
            ["BLZ"],
        ),
        (
            "binance",
            "binance:161",
            "Notice of Removal of Spot Trading Pairs - 2025-01-03",
            "other",
            [],
        ),
        (
            "binance",
            "binance:49",
            "Binance Will Extend Monitoring Tag to Include AERGO and ALPACA",
            "A3",
            ["AERGO", "ALPACA"],
        ),
        ("binance", "binance:49", "Binance Will Remove Monitoring Tag on AERGO", "other", []),
        ("upbit", "upbit:notices", "알케미페이(ACH) 원화 마켓 디지털 자산 추가", "A1", ["ACH"]),
        ("upbit", "upbit:notices", "비트코인 마켓 디지털 자산 추가 (FOO)", "other", []),
        ("upbit", "upbit:notices", "유의 종목 지정 안내 (WAVES)", "A3", ["WAVES"]),
        ("upbit", "upbit:notices", "유의 종목 지정 해제 안내 (WAVES)", "other", []),
        ("upbit", "upbit:notices", "거래지원 종료 안내 (LOOM)", "A3", ["LOOM"]),
        (
            "bybit",
            "bybit_ann:delistings",
            "Delisting of XYZUSDT Perpetual Contract",
            "A4d",
            ["XYZ"],
        ),
        ("bybit", "bybit_ann:delistings", "Delisting of XYZ/USDT Spot Trading Pair", "other", []),
        (
            "bybit",
            "bybit_ann:product_updates",
            "Adjustment of Funding Rate Interval for ABCUSDT",
            "A4f",
            ["ABC"],
        ),
        (
            "bybit",
            "bybit_ann:product_updates",
            "Risk Limit Adjustment for DEFUSDT and 1000GHIUSDT Perpetual Contracts",
            "A4r",
            ["DEF", "GHI"],
        ),
        ("bybit", "bybit_ann:product_updates", "New Copy Trading Feature", "other", []),
    ],
)
def test_classify(source: str, series: str, title: str, bucket: str, bases: list[str]) -> None:
    assert cf.classify(source, series, {"title": title}) == (bucket, bases)


def test_free_text_parser_reads_only_after_the_key_phrase() -> None:
    # An all-caps word before the phrase is never part of the list, and a non-asset
    # acronym after it is dropped. Both would otherwise match a live perp by name.
    title = "Binance AI Desk Will Extend Monitoring Tag to Include AERGO and ALPACA for EEA Users"
    assert cf.classify_parsed("binance", "binance:49", {"title": title}) == (
        "A3",
        ["AERGO", "ALPACA"],
        "caps",
    )
    listed = cf.classify_parsed("binance", "binance:48", {"title": "Binance Will List Sonic (S)"})
    assert listed == ("A1", ["S"], "explicit")
    spot = {"title": "Binance Will Delist ANT, MULTI, VAI on 2024-02-20"}
    assert cf.classify_parsed("binance", "binance:161", spot)[2] == "caps"
    # A real ticker that is also a word stays readable where the form is explicit.
    explicit = cf.classify_parsed("binance", "binance:48", {"title": "Binance Will List ID (ID)"})
    assert explicit == ("A1", ["ID"], "explicit")


@pytest.mark.parametrize(
    ("ticker", "base"),
    [("1000PEPE", "PEPE"), ("1000000MOG", "MOG"), ("SHIB1000", "SHIB"), ("1INCH", "1INCH")],
)
def test_asset_strips_multipliers(ticker: str, base: str) -> None:
    assert cf.asset(ticker) == base


def test_effective_time_from_title() -> None:
    assert cf.effective_ms("Will Delist FOO on 2025-03-04") == ms("2025-03-05T00:00:00") - 1
    assert cf.effective_ms("Opens 2025-03-04 08:00 (UTC)") == ms("2025-03-04T08:00:00")
    assert cf.effective_ms("No date") is None


# ---------------------------------------------------------------- live perp and quality


def perp(symbol: str, launch: int, delivery: int | None = None) -> dict[str, Any]:
    return {"symbol": symbol, "launch_ms": launch, "delivery_ms": delivery}


def test_live_status() -> None:
    t = IN_DISCOVERY
    assert cf.live_status([], t) == ("no_candidate", None)
    assert cf.live_status([perp("AUSDT", t - DAY + 1)], t) == ("no_live_perp", None)
    assert cf.live_status([perp("AUSDT", t - DAY)], t) == ("matched", "AUSDT")
    assert cf.live_status([perp("AUSDT", t - 5 * DAY, t - 1)], t) == ("no_live_perp", None)
    two = [perp("PEPEUSDT", t - 5 * DAY), perp("1000PEPEUSDT", t - 5 * DAY)]
    assert cf.live_status(two, t) == ("identity_ambiguous", None)


def article(**kw: Any) -> dict[str, Any]:
    base = {
        "source": "binance",
        "series": "binance:48",
        "id": "1",
        "announced_ms": IN_DISCOVERY,
        "window": "discovery",
        "bucket": "A2",
        "bases": ["A"],
        "effective_ms": None,
        "machine_ms": [],
        "author_ms": None,
        "parser": "explicit",
    }
    return base | kw


def test_source_quality_whole_minutes_edits_and_order() -> None:
    whole = [
        article(id=str(i), announced_ms=ms("2025-03-03T10:00:00") + i * 60_000) for i in range(10)
    ]
    assert not cf.source_quality(whole)["timed"]
    rows = [article(id=str(i), announced_ms=IN_DISCOVERY + i * 1_000) for i in range(10)]
    rows[0] = rows[0] | {"id": "99"}  # newest id, oldest time
    quality = cf.source_quality(rows)
    assert quality["timed"]
    assert quality["id_order_discordant_share"] == pytest.approx(9 / 45)
    assert not quality["provenance_ok"]
    edited = [
        article(id=str(i), announced_ms=IN_DISCOVERY + i, machine_ms=[IN_DISCOVERY + i + 120_000])
        for i in range(2)
    ] + [
        article(id=str(i), announced_ms=IN_DISCOVERY + i, machine_ms=[IN_DISCOVERY + i])
        for i in range(2, 10)
    ]
    q = cf.source_quality(edited)
    assert q["machine_edit_share"] == pytest.approx(0.2)
    assert not q["provenance_ok"]
    authored = [
        article(id=str(i), announced_ms=IN_DISCOVERY + i, author_ms=IN_DISCOVERY - DAY)
        for i in range(5)
    ]
    assert cf.source_quality(authored)["provenance_ok"]  # author times never gate


def test_order_check_compares_ids_only_within_one_catalog() -> None:
    # Catalog 161 drafts carry low ids but are published after catalog 48's articles.
    # Each catalog is in order; only the pooled comparison disagrees.
    listings = [
        article(series="binance:48", id=str(100 + i), announced_ms=IN_DISCOVERY + i * 1_000)
        for i in range(6)
    ]
    delistings = [
        article(series="binance:161", id=str(i), announced_ms=IN_DISCOVERY + DAY + i * 1_000)
        for i in range(6)
    ]
    quality = cf.source_quality(listings + delistings)
    assert quality["id_order_discordant_pooled"] == pytest.approx(36 / 66)
    assert quality["id_order_discordant_by_series"] == {"binance:161": 0.0, "binance:48": 0.0}
    assert quality["id_order_discordant_share"] == 0.0
    assert not quality["order_check_failed"]
    assert quality["provenance_ok"]

    delistings[0] = delistings[0] | {"id": "99"}  # disorder inside one catalog still fails
    failed = cf.source_quality(listings + delistings)
    assert failed["id_order_discordant_by_series"]["binance:161"] == pytest.approx(5 / 15)
    assert failed["order_check_failed"]
    assert not failed["edit_check_failed"]
    assert not failed["provenance_ok"]


# ---------------------------------------------------------------- report


def receipts_for(
    articles_by_series: dict[str, list[dict[str, Any]]], perps: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    out = [
        {"seq": i, "series": series, "outcome": "ok", "kept": kept}
        for i, (series, kept) in enumerate(articles_by_series.items())
    ]
    out.append({"seq": 99, "series": "bybit_inst:Trading", "outcome": "ok", "kept": perps})
    return out


def futures_listings(start: int, clusters: int, bases: int, weeks: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for i in range(clusters):
        at = start + (i % weeks) * 7 * DAY + (i // weeks) * DAY + 1_234 + i
        rows.append(
            {
                "id": len(rows) + 1,
                "title": f"Binance Futures Will Launch USDⓈ-M B{i % bases}USDT Perpetual Contract",
                "releaseDate": at,
            }
        )
    return sorted(rows, key=lambda r: r["releaseDate"])


def with_ids(rows: list[dict[str, Any]], first: int) -> list[dict[str, Any]]:
    return [r | {"id": first + i} for i, r in enumerate(rows)]


def test_report_feasible_lab_only_and_holdout_privacy() -> None:
    holdout = with_ids(futures_listings(ms("2023-03-06T00:00:00"), 30, 15, 8), 1)
    discovery = with_ids(futures_listings(ms("2024-10-07T00:00:00"), 40, 20, 8), 1_000)
    perps = [perp(f"B{i}USDT", ms("2022-01-01T00:00:00")) for i in range(20)]
    public, sealed = cf.report(receipts_for({"binance:48": holdout + discovery}, perps))
    a2 = public["buckets"]["A2"]
    assert a2["verdict"] == "feasible"
    assert a2["discovery"]["clusters"] == 40
    assert a2["discovery"]["announcements"] == 40
    assert a2["discovery"]["caps_parsed_events"] == 0
    assert set(a2["holdout"]) == set(cf.HOLDOUT_FIELDS)
    assert a2["holdout"]["clusters"] == 30
    assert public["outcome"] == "continue_to_lab_protocol"
    assert len(sealed) == 30
    assert all(e["announced_ms"] < cf.WINDOWS["holdout"][1] for e in sealed)
    assert all(e["announced_ms"] >= cf.WINDOWS["discovery"][0] for e in public["discovery_events"])
    holdout_text = json.dumps(a2["holdout"])
    assert "B0" not in holdout_text
    assert "W" not in holdout_text  # no calendar distribution
    assert public["buckets"]["A4d"]["verdict"] == "not_reconstructible"  # no bybit source
    assert public["buckets"]["A1"]["verdict"] == "insufficient_events"

    short_holdout = holdout[:29]
    public, _ = cf.report(receipts_for({"binance:48": short_holdout + discovery}, perps))
    assert public["buckets"]["A2"]["verdict"] == "lab_only"
    assert public["outcome"] == "stop_direction_a"


def test_bulk_articles_do_not_pass_as_independent_announcements() -> None:
    # Eight articles over eight weeks, five perps each: 40 clusters, 40 bases and 8
    # weeks all pass, but there are only eight independent announcements.
    start = ms("2024-10-07T00:00:00")
    rows = [
        {
            "id": 1_000 + week,
            "title": "Binance Futures Will Launch USDⓈ-M "
            + " and ".join(f"B{week * 5 + k}USDT" for k in range(5))
            + " Perpetual Contracts",
            "releaseDate": start + week * 7 * DAY + 1_234 + week,
        }
        for week in range(8)
    ]
    perps = [perp(f"B{i}USDT", ms("2022-01-01T00:00:00")) for i in range(40)]
    public, _ = cf.report(receipts_for({"binance:48": rows}, perps))
    a2 = public["buckets"]["A2"]
    assert a2["discovery"]["clusters"] == 40
    assert a2["discovery"]["bases"] == 40
    assert a2["discovery"]["weeks"] == 8
    assert a2["discovery"]["announcements"] == 8
    assert a2["verdict"] == "insufficient_events"


def test_report_counts_events_read_by_the_free_text_parser() -> None:
    rows = [
        {
            "id": 1,
            "title": "Binance Will Extend Monitoring Tag to Include B0 and B1",
            "releaseDate": ms("2024-10-07T00:00:00") + 1_234,
        }
    ]
    perps = [perp(f"B{i}USDT", ms("2022-01-01T00:00:00")) for i in range(2)]
    public, _ = cf.report(receipts_for({"binance:49": rows}, perps))
    a3 = public["buckets"]["A3"]["discovery"]
    assert a3["events"] == 2
    assert a3["announcements"] == 1
    assert a3["caps_parsed_events"] == 2
    assert {e["parser"] for e in public["discovery_events"]} == {"caps"}


def test_report_counts_ambiguous_and_missing_perps() -> None:
    rows = with_ids(futures_listings(ms("2024-10-07T00:00:00"), 3, 3, 3), 1)
    perps = [
        perp("B0USDT", ms("2022-01-01T00:00:00")),
        perp("B1USDT", ms("2022-01-01T00:00:00")),
        perp("1000B1USDT", ms("2022-01-01T00:00:00")),
    ]
    public, _ = cf.report(receipts_for({"binance:48": rows}, perps))
    stats = public["buckets"]["A2"]["discovery"]
    assert stats["status"] == {"matched": 1, "identity_ambiguous": 1, "no_candidate": 1}
    assert stats["ambiguous_share"] == pytest.approx(0.5)


def test_report_counts_instrument_launches_per_window_deduplicated() -> None:
    w1 = ms("2024-10-07T00:00:00")
    w2 = ms("2024-10-14T00:00:00")
    receipts = receipts_for(
        {},
        [
            perp("NEW1USDT", w1),
            perp("1000NEW1USDT", w1),
            perp("NEW2USDT", w2),
            perp("HOLD1USDT", IN_HOLDOUT),
            perp("OLD1USDT", TOO_OLD),
        ],
    )
    # Duplicate NEW1USDT in Closed status must not inflate the launch count.
    receipts.append(
        {
            "seq": 99,
            "series": "bybit_inst:Closed",
            "position": "",
            "outcome": "ok",
            "stop": "cursor_end",
            "kept": [perp("NEW1USDT", w1)],
            "dropped": 0,
            "terminal": True,
        }
    )
    public, _ = cf.report(receipts)
    assert public["instrument_launches"] == {
        "discovery": {"launches": 3, "bases": 2, "weeks": 2},
        "holdout": {"launches": 1, "bases": 1, "weeks": 1},
    }


def test_report_refuses_an_unclassified_source() -> None:
    rows = [
        {"id": i + 1, "title": "Something else", "releaseDate": IN_DISCOVERY + i * 1_000}
        for i in range(50)
    ]
    with pytest.raises(ValueError, match="no discovery-year article classified"):
        cf.report(receipts_for({"binance:48": rows}, []))


# ---------------------------------------------------------------- fetcher


SMALL = (
    cf.Series(
        "binance:48", "binance", "https://example.test/b", (("pageSize", "2"),), "page", "pageNo"
    ),
    cf.Series(
        "bybit_inst:Trading",
        "bybit_instruments",
        "https://example.test/i",
        (("category", "linear"),),
        "cursor",
    ),
)


def handler(pages: dict[str, Any]) -> httpx.MockTransport:
    def respond(request: httpx.Request) -> httpx.Response:
        key = (
            request.url.path
            + "?"
            + (request.url.params.get("pageNo") or request.url.params.get("cursor") or "")
        )
        value = pages[key]
        if isinstance(value, int):
            return httpx.Response(value, content=b"secret error body ZZZBLIND")
        return httpx.Response(200, json=value)

    return httpx.MockTransport(respond)


def instrument(symbol: str, launch: int) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "contractType": "LinearPerpetual",
        "settleCoin": "USDT",
        "status": "Trading",
        "launchTime": str(launch),
        "deliveryTime": "0",
    }


PAGES = {
    "/b?1": binance_page(
        [
            {"id": 9, "title": "Binance Will List Blind (ZZZBLIND)", "releaseDate": BLIND},
            {"id": 8, "title": "Binance Will List Kept (KEPT)", "releaseDate": IN_DISCOVERY},
        ]
    ),
    "/b?2": binance_page(
        [
            {"id": 7, "title": "Binance Will List Hold (HOLD)", "releaseDate": IN_HOLDOUT},
            {"id": 6, "title": "Binance Will List Old (OLD)", "releaseDate": TOO_OLD},
        ]
    ),
    "/i?": bybit_page([instrument("AUSDT", IN_HOLDOUT)], "c2"),
    "/i?c2": bybit_page([instrument("ZZZBLINDUSDT", BLIND)], ""),
}


@pytest.fixture
def small(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cf, "SERIES", SMALL)


def fetcher(root: Path) -> cf.Fetcher:
    clock = iter(float(i) for i in range(10_000))
    return cf.Fetcher(root, {"rules": "test"}, now=lambda: next(clock), sleep=lambda _: None)


@pytest.mark.usefixtures("small")
def test_fetch_paginates_and_never_persists_bodies(tmp_path: Path) -> None:
    with httpx.Client(transport=handler(PAGES)) as client:
        summary = fetcher(tmp_path).fetch(client)
    assert summary["complete"]
    assert summary["series"]["binance:48"] == {
        "attempts": 2,
        "pages_ok": 2,
        "kept": 2,
        "dropped": 2,
        "terminal": True,
        "stop": "time_boundary",
    }
    assert summary["series"]["bybit_inst:Trading"]["stop"] == "cursor_end"
    assert summary["budget_exhausted"] == []
    assert summary["series"]["bybit_inst:Trading"]["kept"] == 1
    stored = "".join(p.read_text() for p in tmp_path.rglob("*.json"))
    assert "ZZZBLIND" not in stored
    assert str(BLIND) not in stored
    assert "body_base64" not in stored
    receipts = fetcher(tmp_path).receipts()
    assert [r["position"] for r in receipts] == [1, 2, "", "c2"]
    assert "schema" in receipts[0]
    assert "schema" not in receipts[1]


@pytest.mark.usefixtures("small")
def test_fetch_resumes_without_repeating(tmp_path: Path) -> None:
    pages = dict(PAGES) | {"/b?2": 500}
    with httpx.Client(transport=handler(pages)) as client, pytest.raises(cf.FetchStoppedError):
        fetcher(tmp_path).fetch(client)
    assert len(list((tmp_path / "attempts").glob("*.json"))) == 1 + cf.ATTEMPTS_PER_PAGE
    stored = "".join(p.read_text() for p in tmp_path.rglob("*.json"))
    assert "secret error body" not in stored
    with pytest.raises(cf.FetchStoppedError):
        fetcher(tmp_path).fetch(httpx.Client(transport=handler(pages)))


@pytest.mark.usefixtures("small")
def test_fetch_stops_on_schema_mismatch_with_keys_only(tmp_path: Path) -> None:
    pages = dict(PAGES) | {"/b?1": {"code": "000000", "data": {"rows": [{"t": "ZZZBLIND"}]}}}
    with httpx.Client(transport=handler(pages)) as client, pytest.raises(cf.FetchStoppedError):
        fetcher(tmp_path).fetch(client)
    receipt = fetcher(tmp_path).receipts()[0]
    assert receipt["outcome"] == "schema_mismatch"
    assert "data.rows[].t:str" in receipt["schema"]
    assert "ZZZBLIND" not in json.dumps(receipt)
    # A stopped run stays stopped, on the same revision as well as on a corrected one:
    # the correction starts a new run in a new root (contract, section 6).
    with pytest.raises(cf.FetchStoppedError):
        fetcher(tmp_path).fetch(httpx.Client(transport=handler(PAGES)))
    with pytest.raises(ValueError, match="starts a new run in a new root"):
        cf.Fetcher(tmp_path, {"rules": "corrected"})
    with httpx.Client(transport=handler(PAGES)) as client:
        assert fetcher(tmp_path / "second-run").fetch(client)["complete"]


@pytest.mark.usefixtures("small")
def test_item_level_mismatch_with_an_unchanged_key_tree_records_its_reason(tmp_path: Path) -> None:
    # One article without its time among well-formed ones: the union key tree equals a
    # good page's, so only the failure record can say what broke (regression: the
    # first live run stopped on such a page with nothing to diagnose it).
    good = {"id": 8, "title": "Binance Will List Kept (KEPT)", "releaseDate": IN_DISCOVERY}
    odd = {"id": 7, "title": "Binance Will List Hold (ZZZBLIND)"}
    pages = dict(PAGES) | {"/b?2": binance_page([good, odd])}
    with httpx.Client(transport=handler(pages)) as client, pytest.raises(cf.FetchStoppedError):
        fetcher(tmp_path).fetch(client)
    stopped = fetcher(tmp_path).receipts()[-1]
    assert stopped["outcome"] == "schema_mismatch"
    assert stopped["failure"] == {
        "class": "SchemaMismatchError",
        "stage": "item",
        "item_index": 1,
        "reason": "missing:releaseDate",
    }
    assert "ZZZBLIND" not in json.dumps(stopped)


@pytest.mark.usefixtures("small")
def test_old_instrument_does_not_end_the_catalog(tmp_path: Path) -> None:
    # The catalog has no time order: an instrument launched before the oldest window
    # sits on the first page, an eligible one on the next.
    pages = dict(PAGES) | {
        "/i?": bybit_page([instrument("OLDUSDT", TOO_OLD)], "c2"),
        "/i?c2": bybit_page([instrument("AUSDT", IN_HOLDOUT)], ""),
    }
    with httpx.Client(transport=handler(pages)) as client:
        summary = fetcher(tmp_path).fetch(client)
    assert summary["complete"]
    assert summary["series"]["bybit_inst:Trading"] == {
        "attempts": 2,
        "pages_ok": 2,
        "kept": 2,
        "dropped": 0,
        "terminal": True,
        "stop": "cursor_end",
    }


@pytest.mark.usefixtures("small")
def test_page_budget_is_not_a_complete_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # One page allowed, and it is newer than both windows: nothing was covered.
    monkeypatch.setattr(cf, "MAX_PAGES_PER_SERIES", 1)
    bound = {"rules": "test", "revision": "r1"}
    monkeypatch.setattr(cf, "binding", lambda: bound)
    pages = dict(PAGES) | {
        "/b?1": binance_page(
            [{"id": 9, "title": "Binance Will List Blind (ZZZBLIND)", "releaseDate": BLIND}]
        )
    }
    clock = iter(float(i) for i in range(10_000))
    budgeted = cf.Fetcher(tmp_path, bound, now=lambda: next(clock), sleep=lambda _: None)
    with httpx.Client(transport=handler(pages)) as client:
        summary = budgeted.fetch(client)
    assert summary["series"]["binance:48"]["terminal"]
    assert summary["series"]["binance:48"]["stop"] == "page_budget"
    assert summary["budget_exhausted"] == ["binance:48"]
    assert not summary["complete"]
    with pytest.raises(ValueError, match="page budget reached"):
        cf.main(["report", "--root", str(tmp_path), "--out", str(tmp_path / "r.json")])
    assert not (tmp_path / "r.json").exists()


@pytest.mark.usefixtures("small")
def test_fetch_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cf, "MAX_REQUESTS", 1)
    with httpx.Client(transport=handler(PAGES)) as client, pytest.raises(cf.BudgetExceededError):
        fetcher(tmp_path).fetch(client)
    assert len(list((tmp_path / "attempts").glob("*.json"))) == 1


@pytest.mark.usefixtures("small")
def test_prior_root_budget_carries_across_stopped_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "run1"
    second = tmp_path / "run2"
    bad_pages = dict(PAGES) | {"/b?1": {"code": "000000", "data": {"rows": []}}}
    with httpx.Client(transport=handler(bad_pages)) as client, pytest.raises(cf.FetchStoppedError):
        fetcher(first).fetch(client)
    prior = cf.prior_usage([first])
    assert prior["attempts"] == 1
    assert len(prior["runs"]) == 1
    # SMALL needs 4 requests; with 1 already spent in run1, a cap of 4 blocks run2.
    monkeypatch.setattr(cf, "MAX_REQUESTS", 4)
    clock = iter(float(i) for i in range(10_000))
    succ = cf.Fetcher(
        second,
        {"rules": "test", "revision": "r2", "prior": prior},
        now=lambda: next(clock),
        sleep=lambda _: None,
    )
    with httpx.Client(transport=handler(PAGES)) as client, pytest.raises(cf.BudgetExceededError):
        succ.fetch(client)
    assert len(list((second / "attempts").glob("*.json"))) == 3
    # Raise cap to 5: the remaining 1 request completes run2 and records cumulative total 5.
    monkeypatch.setattr(cf, "MAX_REQUESTS", 5)
    with httpx.Client(transport=handler(PAGES)) as client:
        summary = succ.fetch(client)
    assert summary["complete"]
    assert summary["attempts_and_reserved_bytes"][0] == 5


def test_run_binding_cannot_change(tmp_path: Path) -> None:
    cf.Fetcher(tmp_path, {"rules": "a"})
    with pytest.raises(ValueError, match="run binding differs"):
        cf.Fetcher(tmp_path, {"rules": "b"})


def test_binding_requires_publication_and_registry_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unpublished(_: list[str]) -> dict[str, str]:
        raise ValueError("current revision has not been published in main")

    monkeypatch.setattr(b3, "publication", unpublished)
    with pytest.raises(ValueError, match="not been published"):
        cf.main(["fetch", "--root", str(tmp_path)])
    with pytest.raises(ValueError, match="not been published"):
        cf.main(["report", "--root", str(tmp_path), "--out", str(tmp_path / "r.json")])
    registry = tmp_path / "registry.md"
    registry.write_text("no row here\n")
    monkeypatch.setattr(b3, "publication", lambda _: {})
    monkeypatch.setattr(cf, "REGISTRY", str(registry))
    with pytest.raises(ValueError, match="no seen-windows registry row"):
        cf.binding()
    assert not (tmp_path / "run.json").exists()


def test_plan_makes_no_request(capsys: pytest.CaptureFixture[str]) -> None:
    assert cf.main(["plan"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["limits"]["max_requests"] == 3_000
    assert len(plan["series"]) == 9
