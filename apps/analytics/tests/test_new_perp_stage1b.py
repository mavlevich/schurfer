"""Synthetic Stage 1b metadata tests. No holdout or external server is contacted."""

from __future__ import annotations

import json
import logging
import threading
from contextlib import contextmanager
from dataclasses import FrozenInstanceError, asdict, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from schurfer_analytics import new_perp_m2ii as m
from schurfer_analytics import new_perp_stage1 as s
from schurfer_analytics import new_perp_stage1_verdict as verdict
from schurfer_analytics import new_perp_stage1b as b
from schurfer_analytics.new_perp_window import HOLDOUT, CoverageWindow

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

SYNTH = "SYNTHUSDT"
FOREIGN = "SYNTHFOREIGNUSDT"
FUTURE = "SYNTHFUTUREUSDT"
EARLY = "SYNTHEARLYUSDT"


class Pieces(httpx.SyncByteStream):
    def __init__(self, content: bytes) -> None:
        self.content = content

    def __iter__(self) -> Iterator[bytes]:
        for start in range(0, len(self.content), 65536):
            yield self.content[start : start + 65536]


def html(*links: str) -> bytes:
    return ("<html>" + "".join(f'<a href="{href}">row</a>' for href in links) + "</html>").encode()


def row(symbol: str = SYNTH, day: str = "2023-04-01", **extra: Any) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "launchTime": str(s.day_ms(day)),
        "deliveryTime": "0",
        "contractType": "LinearPerpetual",
        "settleCoin": "USDT",
        **extra,
    }


def page(rows: list[dict[str, Any]], cursor: str = "", **extra: Any) -> bytes:
    return json.dumps(
        {"retCode": 0, "result": {"list": rows, "nextPageCursor": cursor}, **extra}
    ).encode()


def parsed_row(symbol: str = SYNTH, day: str = "2023-04-01") -> dict[str, Any]:
    result: dict[str, Any] = b.catalog_page(page([row(symbol, day)]))["rows"][0]
    return result


def run_case(
    tmp_path: Path,
    rows: list[dict[str, Any]],
    listings: dict[str, bytes | None],
    *,
    validate: Callable[[], None] = lambda: None,
    custom: Callable[[httpx.Request], httpx.Response | None] | None = None,
    contract: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[httpx.Request]]:
    calls: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert not (tmp_path / "run/catalog").exists()
        assert not (tmp_path / "run/admitted").exists()
        logging.getLogger("httpx").warning("private %s", request.url)
        if custom is not None:
            response = custom(request)
            if response is not None:
                return response
        if request.url.path == "/v5/market/instruments-info":
            content = page(rows if request.url.params["status"] == "Trading" else [])
        elif request.method == "HEAD":
            return httpx.Response(200, headers={"Content-Length": "12345"})
        elif request.url.path == "/trading/":
            content = html(*(symbol + "/" for symbol in listings))
        else:
            symbol = request.url.path.split("/")[-2]
            body = listings[symbol]
            if body is None:
                return httpx.Response(404)
            content = body
        return httpx.Response(200, stream=Pieces(content))

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        report = b.run(
            tmp_path / "run",
            client,
            contract=contract or b.build_contract(),
            code={"synthetic": True},
            validate=validate,
            sleep=lambda _: None,
        )
    return report, calls


def facts(tmp_path: Path) -> list[dict[str, Any]]:
    return [json.loads(p.read_text()) for p in sorted((tmp_path / "run/catalog").glob("*.json"))]


def text(tmp_path: Path) -> str:
    return "".join(p.read_text() for p in (tmp_path / "run").rglob("*.json"))


def test_exit_at_cut_is_allowed_and_discovery_behavior_is_unchanged() -> None:
    discovery = CoverageWindow(s.WINDOW_START_DAY, s.CUT_DAY)
    for offset in range(0, 360, 7):
        t0 = discovery.start_ms + offset * s.DAY_MS + 17250
        assert discovery.cuts(t0) == s.v1_cuts(t0)
        assert discovery.funding_span(t0) == s.funding_span(t0)
    t0 = HOLDOUT.cut_ms - 14 * s.DAY_MS
    assert HOLDOUT.cuts(t0)["exit_ms"] == HOLDOUT.cut_ms
    assert HOLDOUT.cuts(t0)["window_censored"] is False
    assert HOLDOUT.cuts(t0 + 1)["window_censored"] is True
    assert s.day_of(HOLDOUT.cuts(t0)["exit_ms"] - 1) == "2024-09-15"
    assert not HOLDOUT.contains(HOLDOUT.cut_ms)
    assert HOLDOUT.contains(HOLDOUT.cut_ms - 1)
    assert HOLDOUT.cuts(HOLDOUT.start_ms + 23 * s.DAY_MS)["lookback_censored"] is False
    assert HOLDOUT.cuts(HOLDOUT.start_ms + 23 * s.DAY_MS - s.MINUTE_MS)["lookback_censored"] is True


def test_window_is_immutable() -> None:
    with pytest.raises(FrozenInstanceError):
        HOLDOUT.start_day = "2020-01-01"  # type: ignore[misc]
    with pytest.raises(ValueError, match="invalid coverage window"):
        CoverageWindow("2024-01-01", "2023-01-01")


def test_catalog_filters_both_ends_and_prior_labels_only_in_ram() -> None:
    response = b.catalog_page(
        page(
            [
                row(EARLY, "2023-01-31"),
                row(SYNTH, "2023-02-01"),
                row(FOREIGN, "2024-09-15"),
                {"launchTime": str(HOLDOUT.cut_ms), "malformed": True},
            ]
        )
    )
    assert {r["symbol"] for r in response["rows"]} == {SYNTH, FOREIGN}
    assert response["prior"][0]["symbol"] == EARLY
    assert "2023-01-31" not in json.dumps(response)


@pytest.mark.parametrize("code", [False, 0.0, "0", None])
def test_catalog_retcode_strict(code: Any) -> None:
    with pytest.raises(s.SchemaMismatchError):
        b.catalog_page(json.dumps({"retCode": code, "result": {"list": []}}).encode())


@pytest.mark.parametrize(
    "links",
    [
        ("invalid.txt",),
        (SYNTH + "2023-02-30.csv.gz",),
        ("../",),
        (SYNTH + "2023-04-01.csv.gz?unexpected=1",),
    ],
)
def test_same_listing_parser_rejects_unsupported_or_unproven_html(links: tuple[str, ...]) -> None:
    with pytest.raises(s.SchemaMismatchError):
        b.listing(html(*links))


def test_listing_rejects_incomplete_html() -> None:
    with pytest.raises(s.SchemaMismatchError, match="incomplete_html"):
        b.listing(html(SYNTH + "2023-04-01.csv.gz").replace(b"</html>", b""))


def test_catalog_pre_window_predicates_without_persisting_dates() -> None:
    near = b.catalog_facts(
        parsed_row(day="2023-02-01"), {SYNTH: {"2023-01-31", "2023-02-01"}}, "ok"
    )
    assert near["has_pre_window_file"] and near["first_file_within_1d_of_launch"]
    assert not near["ticker_reuse_suspected"]
    assert near["t0_status"] == "t0_pre_window_unreadable"
    assert near["first_file_class_from_day"] is None
    assert "first_file_day" not in near
    assert "2023-01-31" not in json.dumps(near)
    distant = b.catalog_facts(
        parsed_row(day="2023-02-01"), {SYNTH: {"2022-12-01", "2023-02-01"}}, "ok"
    )
    assert distant["ticker_reuse_suspected"]
    assert distant["first_file_class_from_day"] == "premarket_or_ambiguous"
    assert not distant["first_file_within_1d_of_launch"]
    assert "2022-12-01" not in json.dumps(distant)


def test_pre_window_head_is_never_planned_even_when_old_planner_would_request_it(
    tmp_path: Path,
) -> None:
    assert s.needs_first_file("2023-01-31", s.day_ms("2023-02-01"))
    report, calls = run_case(
        tmp_path,
        [row(day="2023-02-01")],
        {SYNTH: html(SYNTH + "2023-01-31.csv.gz", SYNTH + "2023-02-01.csv.gz")},
    )
    assert report["catalog_count"] == report["catalog_slots"] == 1
    assert report["m1_states"] == {"t0_pre_window_unreadable": 1}
    assert not any(c.method == "HEAD" or c.url.path.endswith(".csv.gz") for c in calls)
    assert "2023-01-31" not in text(tmp_path)


@pytest.mark.parametrize(
    "cls,method,day",
    [
        ("D2h", "HEAD", "2023-01-31"),
        ("D2h", "HEAD", "2024-09-16"),
        ("D2", "GET", "2023-04-01"),
        ("D2h", "GET", "2023-04-01"),
    ],
)
def test_direct_request_rejects_pre_window_or_unimplemented_body(
    cls: str, method: str, day: str
) -> None:
    with pytest.raises(ValueError, match="outside sealed"):
        b.OpaqueRequest(cls, method, s.first_file_url(SYNTH, day))


def test_no_archive_stays_in_n_and_all_rejection_counts_without_its_name(tmp_path: Path) -> None:
    report, calls = run_case(tmp_path, [row()], {SYNTH: None})
    assert report["catalog_count"] == report["catalog_slots"] == 1
    assert report["m1_states"] == {"no_archive": 1}
    assert report["m2_i_no_file_near_launch"] == 1
    assert report["m3_catalog_states"] == {"not_contradicted": 1}
    assert report["reasons"] == {"t0_unavailable": 1}
    assert SYNTH not in text(tmp_path)
    assert not any(c.method == "HEAD" for c in calls)
    assert facts(tmp_path)[0]["listing_state"] == "absent"
    assert report["outcome"] == "incomplete_coverage"
    assert report["known_coverage_failures"] == ["m2_catalog_no_file_near_launch"]


def test_prior_catalog_base_predicates_survive_without_early_names(tmp_path: Path) -> None:
    prior = row("1000SYNTHUSDT", "2022-01-01", deliveryTime=str(s.day_ms("2022-12-01")))
    report, _ = run_case(tmp_path, [prior, row()], {SYNTH: None})
    assert report["catalog_count"] == 1
    assert report["m3_catalog_states"] == {"relist": 1}
    assert "1000SYNTHUSDT" not in text(tmp_path)
    assert SYNTH not in text(tmp_path)


def test_complete_metadata_only_has_head_and_sealed_facts(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    report, calls = run_case(tmp_path, [row()], {SYNTH: html(SYNTH + "2023-04-01.csv.gz")})
    assert report["outcome"] == "metadata_complete_coverage_pending"
    assert report["complete"] and report["binding_valid"]
    assert report["body_phase_authorized"] is False
    assert report["stop_reason"] is None
    assert "m1_csv_pending" in report["not_assessed"]
    assert len(calls) == 6  # 3 catalog + root + directory + HEAD
    assert sum(c.method == "HEAD" for c in calls) == 1
    assert (tmp_path / "run").stat().st_mode & 0o777 == 0o700
    assert facts(tmp_path)[0]["symbol"] == SYNTH
    assert facts(tmp_path)[0]["head"]["content_length"] == 12345
    assert SYNTH not in (tmp_path / "run/metadata-report.json").read_text()
    assert SYNTH not in caplog.text
    for folder in ("receipts", "attempts"):
        for path in (tmp_path / "run" / folder).glob("*.json"):
            receipt = json.loads(path.read_text())
            assert not receipt["symbol"] and not receipt["meta"]
            assert not ({"schema", "body_sha256", "ret_msg", "facts", "url"} & receipt.keys())


def test_catalog_foreign_prefix_blocks_its_head_and_protects_unknown_collision(
    tmp_path: Path,
) -> None:
    report, calls = run_case(
        tmp_path,
        [row()],
        {
            SYNTH: html(SYNTH + "2023-04-01.csv.gz", FOREIGN + "2023-04-01.csv.gz"),
            FUTURE: html(FOREIGN + "2023-04-02.csv.gz", FUTURE + "2025-01-01.csv.gz"),
        },
    )
    assert report["catalog_count"] == 1
    assert report["m1_states"] == {"filename_identity_unverified": 1}
    assert report["m2ii"]["collision_directories"] == 1
    assert report["m2ii"]["share"] is None
    assert report["outcome"] == "metadata_complete_unassessed"
    assert "m2_prefix_collision_unverified" in report["not_assessed"]
    assert not any(c.method == "HEAD" for c in calls)
    assert all(name not in text(tmp_path) for name in (SYNTH, FOREIGN, FUTURE))


def test_unknown_pre_window_only_keeps_boolean_and_never_old_prefix_or_dates(
    tmp_path: Path,
) -> None:
    report, _ = run_case(
        tmp_path,
        [row()],
        {SYNTH: None, FUTURE: html(EARLY + "2022-01-01.csv.gz", FUTURE + "2025-01-01.csv.gz")},
    )
    assert report["m2ii"]["archive_only_pre_window"] == 1
    admitted = list((tmp_path / "run/admitted").glob("*.json"))
    assert json.loads(admitted[0].read_text()) == {"has_pre_window_file": True}
    assert all(name not in text(tmp_path) for name in (EARLY, FUTURE, "2022-01-01", "2025-01-01"))


def test_known_m2_breach_has_priority_over_an_unverified_folder(tmp_path: Path) -> None:
    listings = {SYNTH: None, FUTURE: html("BAD_PREFIX2023-05-01.csv.gz")}
    listings.update({f"SYNTH{i}USDT": html(f"SYNTH{i}USDT2023-05-01.csv.gz") for i in range(2)})
    report, _ = run_case(tmp_path, [row()], listings)
    assert report["m2ii"]["archive_only_in_window"] == 2
    assert report["m2ii"]["unverified_directories"] == 1
    assert report["m2ii"]["share"] is None
    assert report["outcome"] == "incomplete_coverage"


def test_post_cut_files_do_not_change_scientific_facts(tmp_path: Path) -> None:
    before = b.listing(html(SYNTH + "2023-05-01.csv.gz"))
    mixed = b.listing(html(SYNTH + "2023-05-01.csv.gz", FUTURE + "2025-01-01.csv.gz"))
    assert before == mixed
    assert b.unknown_observation(before) == b.unknown_observation(mixed)
    run_case(tmp_path, [row()], {SYNTH: html(FUTURE + "2025-01-01.csv.gz")})
    assert facts(tmp_path)[0]["t0_status"] == "no_pre_cut_file"
    assert FUTURE not in text(tmp_path)


def test_catalog_cursor_cycle_stops_without_a_partial_n(tmp_path: Path) -> None:
    def custom(request: httpx.Request) -> httpx.Response | None:
        return httpx.Response(200, stream=httpx.ByteStream(page([row()], "SECRET_CURSOR")))

    report, calls = run_case(tmp_path, [], {}, custom=custom)
    assert len(calls) == 2
    assert not report["complete"] and report["catalog_count"] is None
    assert not facts(tmp_path)
    assert "SECRET_CURSOR" not in text(tmp_path)


def test_catalog_conflicting_duplicate_fails_closed(tmp_path: Path) -> None:
    report, _ = run_case(tmp_path, [row(), row(day="2023-04-02")], {SYNTH: None})
    assert not report["complete"] and report["catalog_count"] is None
    assert "catalog_duplicate_conflict" in (tmp_path / "run/stop.json").read_text()
    assert report["stop_reason"] == "catalog_duplicate_conflict"


def test_catalog_truncated_page_is_not_proven(tmp_path: Path) -> None:
    response = b.catalog_page(page([row()] * 1000))
    assert response["unproven_full_page"]
    report, _ = run_case(tmp_path, [row()] * 1000, {})
    assert not report["complete"]
    assert "catalog_truncation_unproven" in (tmp_path / "run/stop.json").read_text()


def test_source_failure_erases_admission_and_positive_result(tmp_path: Path) -> None:
    validations = 0

    def corrupt() -> None:
        nonlocal validations
        validations += 1
        if validations > 1:
            raise ValueError("private detail must not persist")

    report, _ = run_case(
        tmp_path,
        [row()],
        {SYNTH: None, FOREIGN: html(FOREIGN + "2023-05-01.csv.gz")},
        validate=corrupt,
    )
    assert report["outcome"] == "source_integrity_error"
    assert not report["complete"] and report["catalog_count"] is None
    assert not facts(tmp_path) and not list((tmp_path / "run/admitted").glob("*.json"))
    assert "private detail" not in text(tmp_path)


def test_invalid_initial_prerequisite_makes_no_request_or_output_root(tmp_path: Path) -> None:
    def invalid() -> None:
        raise ValueError("private source detail")

    with (
        httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail("no HTTP"))) as client,
        pytest.raises(ValueError, match="source/code prerequisite invalid"),
    ):
        b.run(tmp_path / "run", client, contract=b.build_contract(), code={}, validate=invalid)
    assert not (tmp_path / "run").exists()


def test_incomplete_listing_does_not_admit_partial_unknown_series(tmp_path: Path) -> None:
    report, _ = run_case(
        tmp_path,
        [row()],
        {SYNTH: None, "SYNTHAUSDT": html("SYNTHAUSDT2023-05-01.csv.gz"), "SYNTHZUSDT": None},
    )
    assert not report["complete"] and report["catalog_count"] == 1
    assert report["catalog_slots"] == 1
    assert not list((tmp_path / "run/admitted").glob("*.json"))


def test_zero_catalog_is_insufficient_without_archive_scan(tmp_path: Path) -> None:
    report, calls = run_case(tmp_path, [], {})
    assert report["outcome"] == "insufficient_events"
    assert report["catalog_count"] == 0
    assert len(calls) == 3


def test_source_and_existing_roots_refused_before_http(tmp_path: Path) -> None:
    root = tmp_path / "sealed"
    root.mkdir()
    with httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail("no HTTP"))) as client:
        with pytest.raises(ValueError, match="inside a sealed"):
            b.run(
                root / "child",
                client,
                contract=b.build_contract(),
                code={},
                validate=lambda: None,
                sources=(root,),
            )
        with pytest.raises(FileExistsError):
            b.run(root, client, contract=b.build_contract(), code={}, validate=lambda: None)


def test_metadata_contract_caps_and_c1_ninth_full_response_reserve(tmp_path: Path) -> None:
    contract = b.build_contract()
    assert contract == json.loads(Path(b.CONTRACT).read_text())
    assert contract["request_list_sha256"] in Path(b.DOC).read_text()
    assert contract["request_list_sha256"] in Path(s.REGISTRY).read_text()
    assert contract["go_limits"]["1"] == {"max_attempts": 2400, "max_bytes": 448 * s.MIB}
    assert sum(spec.max_requests for spec in b.class_specs().values()) == 1810
    assert b.class_specs()["D2h"].max_class_bytes == 0
    body = page([], ignored_padding="x" * (3 * s.MIB))
    # Build valid JSON of exactly the C1 response cap.
    body = page([], ignored_padding="x" * (3 * s.MIB - (len(body) - 3 * s.MIB)))
    assert len(body) == 3 * s.MIB

    class Chunks(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            for start in range(0, len(body), 65536):
                yield body[start : start + 65536]

    def drive(root: Path, old: bool) -> int:
        specs = b.class_specs()
        if old:
            specs["C1"] = replace(specs["C1"], max_class_bytes=27 * s.MIB)
        data = b.build_contract()
        data["classes"] = [asdict(spec) for spec in specs.values()]
        data["request_list_sha256"] = s.contract_hash(data)
        with httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=Chunks()))
        ) as client:
            fetcher = m.PrivateFetcher(root, data, client, specs=specs, sleep=lambda _: None)
            for i in range(9):
                request = b.OpaqueRequest(
                    "C1",
                    "GET",
                    s.CATALOG_URL,
                    (
                        ("category", "linear"),
                        ("status", "Trading"),
                        ("limit", "1000"),
                        ("cursor", str(i)),
                    ),
                )
                fetcher.run_request(request, b.catalog_page, keep_facts=False)
            return int(fetcher.summary()["bytes"])

    with pytest.raises(s.BudgetExceededError, match="class byte cap"):
        drive(tmp_path / "old", True)
    assert len(list((tmp_path / "old/receipts").glob("*.json"))) == 8
    assert drive(tmp_path / "new", False) == 27 * s.MIB


@pytest.mark.parametrize(
    "n,expected", [(19, "metadata_complete_coverage_pending"), (18, "incomplete_coverage")]
)
def test_m2_exact_five_percent_uses_holdout_n(tmp_path: Path, n: int, expected: str) -> None:
    rows = [row(f"SYNTH{i:03}USDT") for i in range(n)]
    listings: dict[str, bytes | None] = {
        r["symbol"]: html(r["symbol"] + "2023-04-01.csv.gz") for r in rows
    }
    listings[FOREIGN] = html(FOREIGN + "2023-04-01.csv.gz")
    report, _ = run_case(tmp_path, rows, listings)
    assert report["m2ii"]["share"] == 1 / (n + 1)
    assert report["outcome"] == expected


def test_in_window_numeric_metadata_does_not_accept_arbitrary_labels() -> None:
    bad = row(lotSizeFilter={"minNotionalValue": FUTURE})
    with pytest.raises(s.SchemaMismatchError, match="catalog_numeric_field"):
        b.catalog_page(page([bad]))
    bad["launchTime"] = str(HOLDOUT.cut_ms)
    assert b.catalog_page(page([bad]))["rows"] == []


def test_catalog_pre_window_gaps_and_prior_live_multiplier(tmp_path: Path) -> None:
    facts_ = b.catalog_facts(parsed_row(), {SYNTH: {"2023-02-01", "2023-04-01"}}, "ok")
    assert facts_["ticker_reuse_suspected"]
    gap = b.catalog_facts(parsed_row(), {SYNTH: {"2023-03-31", "2023-05-02"}}, "ok")
    assert gap["first_file_within_1d_of_launch"] and gap["ticker_reuse_suspected"]
    prior = row("1000SYNTHUSDT", "2022-01-01")
    report, _ = run_case(tmp_path, [prior, row()], {SYNTH: html(SYNTH + "2023-04-01.csv.gz")})
    assert report["m3_catalog_states"] == {"multiplier_ambiguous": 1}
    assert "1000SYNTHUSDT" not in text(tmp_path)


def test_old_authorization_rejected_before_paths_or_http(tmp_path: Path) -> None:
    for stale in (
        "11e52757",
        "e4513b79bfc4e2f8b31d1e691a3879565ad3f0e7372ab20ffd9793c69f772c9f",
    ):
        with pytest.raises(ValueError, match="approval or checked-in"):
            b.main(
                [
                    "fetch-metadata",
                    "--approved-sha256",
                    stale,
                    *[
                        arg
                        for name in ("base", "v2", "diagnostic", "discovery", "root")
                        for arg in ("--" + name, str(tmp_path / name))
                    ],
                ]
            )


def test_unknown_prefix_collision_and_mixed_prefixes_block_all_owners(tmp_path: Path) -> None:
    report, _ = run_case(
        tmp_path,
        [row()],
        {
            SYNTH: html(SYNTH + "2023-04-01.csv.gz"),
            "SYNTHAUSDT": html(FOREIGN + "2023-05-01.csv.gz"),
            "SYNTHBUSDT": html(FOREIGN + "2023-05-02.csv.gz", EARLY + "2023-05-02.csv.gz"),
        },
    )
    assert report["m2ii"]["collision_directories"] == 2
    assert report["m2ii"]["unverified_directories"] == 1
    assert not list((tmp_path / "run/admitted").glob("*.json"))
    assert all(name not in text(tmp_path) for name in (FOREIGN, EARLY))


def test_catalog_cap_stops_before_selecting_a_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(b, "MAX_CATALOG", 1)
    report, calls = run_case(tmp_path, [row(), row(FOREIGN)], {})
    assert not report["complete"] and report["catalog_count"] is None
    assert len(calls) == 1
    assert not facts(tmp_path)


def test_unknown_cap_stops_before_any_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(b, "MAX_UNKNOWN", 0)
    report, calls = run_case(tmp_path, [row()], {SYNTH: None, FOREIGN: None})
    assert not report["complete"] and report["catalog_count"] == 1
    assert len(calls) == 4
    assert SYNTH not in text(tmp_path) and FOREIGN not in text(tmp_path)


def test_prefix_ram_cap_includes_catalog_and_never_admits_a_partial_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(b, "MAX_PREFIX_SLOTS", 1)
    report, _ = run_case(
        tmp_path,
        [row()],
        {
            SYNTH: html(SYNTH + "2023-04-01.csv.gz"),
            FOREIGN: html(FOREIGN + "2023-04-01.csv.gz"),
        },
    )
    assert not report["complete"] and report["m2ii"]["admitted_series"] == 0
    assert "pre_cut_prefix_budget" in (tmp_path / "run/stop.json").read_text()
    assert FOREIGN not in text(tmp_path)


def test_transitive_publication_includes_asset_and_transport_code() -> None:
    assert set(s.PUBLISHED) <= set(b.PUBLISHED)
    assert "apps/analytics/schurfer_analytics/new_perp_window.py" in b.PUBLISHED


class LocalBridge(httpx.BaseTransport):
    """Tests only: all Bybit requests reach an actual loopback HTTP server instead."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.inner = httpx.HTTPTransport(trust_env=False)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        assert request.url.host in ("api.bybit.com", "public.bybit.com")
        local_url = request.url.copy_with(scheme="http", host="127.0.0.1", port=self.port)
        local = httpx.Request(request.method, local_url, headers={"Accept-Encoding": "identity"})
        return self.inner.handle_request(local)

    def close(self) -> None:
        self.inner.close()


@contextmanager
def local_server(
    body: bytes, *, declared: int | None = None, status: int = 200
) -> Iterator[tuple[httpx.Client, list[str]]]:
    calls: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        # Ruff versions differ on stdlib overrides; the HTTP hook name is fixed.
        def do_GET(self) -> None:  # noqa: N802, RUF100
            calls.append(self.path)
            self.send_response(status)
            if declared is not None:
                self.send_header("Content-Length", str(declared))
            self.end_headers()
            if declared is None or declared == len(body):
                self.wfile.write(body)

        def log_message(self, _format: str, *_args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client(
            transport=LocalBridge(server.server_port), trust_env=False, timeout=5
        ) as client:
            yield client, calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("declared", [None, 6000])
def test_real_local_transport_counts_unknown_length_and_rejects_declared_over_cap(
    tmp_path: Path, declared: int | None
) -> None:
    specs = b.class_specs()
    specs["C3"] = replace(specs["C3"], max_response_bytes=4096)
    contract = b.build_contract()
    contract["classes"] = [asdict(spec) for spec in specs.values()]
    contract["go_limits"]["1"]["max_bytes"] = 4096 + 65536
    contract["request_list_sha256"] = s.contract_hash(contract)
    with local_server(b"x" * 6000, declared=declared) as (client, calls):
        fetcher = m.PrivateFetcher(tmp_path / "transport", contract, client, specs=specs)
        result = fetcher.run_request(b.OpaqueRequest("C3", "GET", s.ARCHIVE_ROOT), keep_facts=False)
    assert result["outcome"] == "too_large"
    assert len(calls) == len(fetcher.attempts) == len(fetcher.receipts) == 1
    assert fetcher.summary()["bytes"] == (0 if declared is not None else 6000)
    assert fetcher.summary()["bytes"] <= contract["go_limits"]["1"]["max_bytes"]


def test_real_local_forbidden_receipt_prevents_recovery_send(tmp_path: Path) -> None:
    root = tmp_path / "transport"
    with local_server(b"", status=403) as (client, calls), m.private_logs():
        fetcher = m.PrivateFetcher(root, b.build_contract(), client, specs=b.class_specs())
        with pytest.raises(s.FetchStoppedError, match="403"):
            fetcher.run_request(b.OpaqueRequest("C3", "GET", s.ARCHIVE_ROOT), keep_facts=False)
        # Reproduce a crash after the durable forbidden receipt but before stop.json.
        (root / "stop.json").unlink()
        with pytest.raises(s.FetchStoppedError, match="recovered"):
            m.PrivateFetcher(root, b.build_contract(), client, specs=b.class_specs())
    assert len(calls) == 1
    assert (root / "stop.json").exists()


def test_round2_sealed_calendar_supports_path_coverage_without_refetch(tmp_path: Path) -> None:
    path_days = verdict.path_days(s.day_ms("2023-04-08"), s.day_ms("2023-04-15"))
    available = ["2023-04-01", *[day for day in path_days if day != "2023-04-09"]]
    report, calls = run_case(
        tmp_path,
        [row()],
        {SYNTH: html(*(SYNTH + day + ".csv.gz" for day in available), SYNTH + "2024-09-16.csv.gz")},
    )
    assert report["complete"]
    call_count = len(calls)
    stored = facts(tmp_path)[0]
    assert stored["days_in_window"] == available
    missing = sorted(set(path_days) - set(stored["days_in_window"]))
    assert missing == ["2023-04-09"]
    assert len(missing) > 0.05 * len(path_days)
    assert len(calls) == call_count
    assert "2024-09-16" not in json.dumps(stored)


def test_round2_calendar_never_stores_pre_window_or_cut_days() -> None:
    before = b.listing(
        html(
            SYNTH + "2023-01-31.csv.gz",
            SYNTH + "2023-02-01.csv.gz",
            SYNTH + "2024-09-15.csv.gz",
            SYNTH + "2024-09-16.csv.gz",
        )
    )
    stored = b.catalog_facts(parsed_row(day="2023-02-01"), before, "ok")
    assert stored["days_in_window"] == ["2023-02-01", "2024-09-15"]
    assert "2023-01-31" not in json.dumps(stored)
    assert "2024-09-16" not in json.dumps(stored)


@pytest.mark.parametrize(
    "first,launch,status,reason",
    [
        ("2023-04-03", "2023-04-01", "premarket_or_ambiguous", "t0_ambiguous"),
        ("2022-12-01", "2023-04-01", "premarket_or_ambiguous", "t0_ambiguous"),
        ("2022-12-01", "2023-02-01", "premarket_or_ambiguous", "t0_ambiguous"),
        ("2023-01-31", "2023-02-01", "t0_pre_window_unreadable", "t0_unavailable"),
        ("2023-04-01", "2023-04-01", "csv_pending", "csv_pending"),
    ],
)
def test_round2_filename_class_precedes_pre_window_unreadability(
    first: str, launch: str, status: str, reason: str
) -> None:
    stored = b.catalog_facts(parsed_row(day=launch), {SYNTH: {first}}, "ok")
    assert stored["t0_status"] == status
    assert stored["reason"] == reason
    if first < HOLDOUT.start_day:
        assert "first_file_day" not in stored
        assert first not in json.dumps(stored)


@pytest.mark.parametrize("failure_phase", ["unknown", "head", "catalog"])
def test_round2_known_m2_failure_survives_later_scan_stop(
    tmp_path: Path, failure_phase: str
) -> None:
    missing, good = "ASYNTHMISSUSDT", "BSYNTHGOODUSDT"
    listings = {missing: None, good: html(good + "2023-04-01.csv.gz")}
    if failure_phase == "unknown":
        listings[FOREIGN] = None

    def fail_later(request: httpx.Request) -> httpx.Response | None:
        if failure_phase == "head" and request.method == "HEAD":
            return httpx.Response(403)
        if failure_phase == "catalog" and request.url.path == f"/trading/{good}/":
            return httpx.Response(403)
        return None

    report, _ = run_case(tmp_path, [row(missing), row(good)], listings, custom=fail_later)
    assert not report["complete"] and report["binding_valid"]
    assert report["catalog_count"] == report["catalog_slots"] == 2
    assert report["m2_i_no_file_near_launch"] == 1
    assert report["outcome"] == "incomplete_coverage"
    assert "m2_catalog_no_file_near_launch" in report["known_coverage_failures"]
    assert "metadata_scan_incomplete" in report["not_assessed"]
    assert not (tmp_path / "run/metadata_done.json").exists()
