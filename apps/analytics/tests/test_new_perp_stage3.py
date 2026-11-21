"""I25 stage 3 (power calibration, dispersion only): synthetic tests, no network.

The cohort comes from a real Stage 1 run against the synthetic world of ``new_perp_world``;
the hourly klines of stage 3 come from a mock transport in this file. Every price in a body
is a sentinel or a controlled number, so a test can prove that no receipt or report holds a
close, that nothing after the entry minute is parsed, and that no mean or per-launch value
leaves the sealed root.
"""

from __future__ import annotations

import json
import math
import random
import re
import statistics
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from schurfer_analytics import new_perp_stage1 as s1
from schurfer_analytics import new_perp_stage1_verdict as v1
from schurfer_analytics import new_perp_stage3 as t

from .new_perp_world import DAY, Clock, Launch, World, ms, query, rehash, run_world

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator


class Pieces(httpx.SyncByteStream):
    """A body delivered in chunks of at most 64 KiB, as the real transport does."""

    def __init__(self, content: bytes) -> None:
        self.content = content

    def __iter__(self) -> Iterator[bytes]:
        for start in range(0, len(self.content), 65536):
            yield self.content[start : start + 65536]


@dataclass
class ChunkedWorld(World):
    """The Stage 1 synthetic world, answering a body larger than 64 KiB in several chunks."""

    def handle(self, request: httpx.Request) -> httpx.Response:
        built = self._respond(request)
        return httpx.Response(
            built.status_code, headers=built.headers, stream=Pieces(built.content)
        )


HOUR = t.HOUR_MS
START_CLOSE = "7351.2468"
GARBAGE = "NOTAPRICE"
REPO = Path(__file__).resolve().parents[3]


# The synthetic cohort: a real Stage 1 run, once.


@pytest.fixture(scope="module")
def stage1(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    launches = World.evenly(228, start="2024-11-01T10:00:00", step_days=1).launches
    launches.append(Launch("LATE000USDT", "LATE000", ms("2025-09-20T10:00:00")))
    launches.append(Launch("EARLY00USDT", "EARLY00", ms("2024-10-05T10:00:00")))
    world = ChunkedWorld(launches)
    root = tmp_path_factory.mktemp("stage1") / "run"
    _, report = run_world(world, root)
    data = v1.load_run(root)
    rows = t._stage1_rows(data)
    episodes = t.derive_cohort(root, report, manifest_sha256=s1.manifest_sha256(root), bound=None)
    return {"root": root, "report": report, "rows": rows, "episodes": episodes, "world": world}


class Hourly:
    """Serves hourly trade klines. Rows other than the two endpoints hold garbage closes."""

    def __init__(
        self,
        episodes: list[t.Episode],
        log_returns: dict[str, float],
        *,
        missing_end: frozenset[str] = frozenset(),
        errors: list[tuple[str, Any, str]] | None = None,
        crash: list[str] | None = None,
        extra_row: dict[str, int] | None = None,
        pad: dict[str, int] | None = None,
    ) -> None:
        self.by_symbol = {e.symbol: e for e in episodes}
        self.log_returns = log_returns
        self.missing_end = missing_end
        self.errors = errors if errors is not None else []
        self.crash = crash if crash is not None else []
        self.extra_row = extra_row or {}
        self.pad = pad or {}
        self.requests: list[httpx.Request] = []
        self.end_closes: set[str] = set()

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host != "api.bybit.com" or not request.url.path.endswith("/market/kline"):
            raise AssertionError(f"unexpected request {request.url}")
        q = query(request)
        symbol = q["symbol"]
        for entry in list(self.errors):
            if entry[0] == symbol:
                self.errors.remove(entry)
                envelope = {"retCode": entry[1], "retMsg": entry[2], "result": {}, "time": 1}
                return self._response(envelope)
        if symbol in self.crash:
            self.crash.remove(symbol)
            raise RuntimeError("the process died")
        start, end = int(q["start"]), int(q["end"])
        rows: list[list[str]] = []
        for stamp in range(end, start - 1, -HOUR):
            close = GARBAGE
            if stamp == start:
                close = START_CLOSE
            elif stamp == end:
                if symbol in self.missing_end:
                    continue
                close = f"{7351.2468 * math.exp(self.log_returns[symbol]):.8f}"
                self.end_closes.add(close)
            rows.append([str(stamp), GARBAGE, GARBAGE, GARBAGE, close, GARBAGE, GARBAGE])
        if symbol in self.extra_row:
            rows.insert(0, [str(self.extra_row[symbol]), "x", "x", "x", "x", "x", "x"])
        body = {
            "retCode": 0,
            "retMsg": "OK",
            "result": {"symbol": symbol, "category": "linear", "list": rows[: int(q["limit"])]},
        }
        if symbol in self.pad:
            body["pad"] = "x" * self.pad[symbol]  # one chunk over the 64 KiB read reserve
        return self._response(body)

    @staticmethod
    def _response(payload: Any) -> httpx.Response:
        return httpx.Response(200, stream=httpx.ByteStream(json.dumps(payload).encode()))


def fetcher_for(
    root: Path,
    transport: Hourly,
    *,
    contract: dict[str, Any] | None = None,
    code: dict[str, Any] | None = None,
    clock: Clock | None = None,
) -> tuple[s1.Fetcher, Clock]:
    clock = clock or Clock()
    f = s1.Fetcher(
        root,
        contract or t.build_contract(),
        httpx.Client(transport=transport.transport()),
        go=1,
        code=code,
        prior={},
        now=clock.now,
        sleep=clock.sleep,
        wall=clock.wall,
        specs=dict(t.SPECS),
    )
    return f, clock


def draw_returns(
    episodes: list[t.Episode], *, seed: int = 11, sd: float = 0.05, week_sd: float = 0.0
) -> dict[str, float]:
    rng = random.Random(seed)  # noqa: S311
    weeks = {w: rng.gauss(0, week_sd) for w in sorted({e.week for e in episodes})}
    return {e.symbol: weeks[e.week] + rng.gauss(0, sd) for e in episodes}


def run_full(
    tmp_path: Path,
    episodes: list[t.Episode],
    transport: Hourly,
    monkeypatch: pytest.MonkeyPatch | None = None,
    *,
    code: dict[str, Any] | None = None,
    fingerprint: str | None = None,
) -> dict[str, Any]:
    if monkeypatch is not None:
        monkeypatch.setattr(t, "BOOTSTRAP_DRAWS", 400)
    root = tmp_path / "run"
    f, _ = fetcher_for(root, transport, code=code)
    t.run_stage3(f, episodes, fingerprint)
    return t.evaluate(root)


# The contract


def test_contract_is_deterministic_hash_bound_and_checked_in() -> None:
    first, second = t.build_contract(), t.build_contract()
    assert first == second
    assert s1.contract_hash(first) == first["request_list_sha256"]
    assert first["go_sha256"]["1"] == t.go_hash(first)
    saved = json.loads((REPO / t.CONTRACT_PATH).read_text())
    assert saved == first


def test_every_pre_registered_rule_moves_the_hashes(monkeypatch: pytest.MonkeyPatch) -> None:
    base = t.build_contract()
    for name, value in (
        ("MDE_LIMIT_BPS", 401.0),
        ("GATE_MULTIPLIER", 2.81),
        ("BOOTSTRAP_DRAWS", 9_999),
        ("DEFF_FLOOR", 1.1),
        ("SEED", 1),
        ("MIN_WEEKS", 25),
        ("DEFINED_MIN_PERCENT", 94),
        ("PROXY_LIMIT", 201),
    ):
        with monkeypatch.context() as patch:
            patch.setattr(t, name, value)
            changed = t.build_contract()
        assert changed["request_list_sha256"] != base["request_list_sha256"], name
        assert changed["go_sha256"]["1"] != base["go_sha256"]["1"], name


def test_one_go_one_class_one_endpoint_and_the_budget_arithmetic() -> None:
    c = t.build_contract()
    assert [x["cls"] for x in c["classes"]] == ["P1"] and list(c["go_limits"]) == ["1"]
    assert c["classes"][0]["url"] == s1.KLINE_URL
    b = c["budget"]
    assert b["requests"] == 228 == c["classes"][0]["max_requests"]
    assert b["attempt_ceiling"] == 684 == 3 * 228 == c["go_limits"]["1"]["max_attempts"]
    assert b["response_cap_bytes"] == 64 * 1024
    assert b["class_byte_cap"] == b["go_byte_cap"] == 48 * 1024 * 1024
    assert b["worst_case_response_bytes"] == 684 * 64 * 1024
    # the last attempt can still start: used + response cap + reserve fits the go cap
    assert b["worst_case_response_bytes"] + s1.reserve(t.P1) <= b["go_byte_cap"]
    assert b["seconds_expected"] == 228 and b["seconds_all_retries"] == 684 + 228 * 40 == 9_804


def test_decision_table_is_in_the_contract_in_protocol_order() -> None:
    c = t.build_contract()
    assert [r["verdict"] for r in c["decision_table"]] == [
        "incomplete_coverage",
        "insufficient_events",
        "not_estimable",
        "insufficient_power",
        "power_gate_passed",
    ]
    assert c["thresholds"]["mde_limit_bps"] == 400.0 and c["thresholds"]["gate_multiplier"] == 2.8
    assert set(c["output_allowlist"]) == t.REPORT_KEYS


# The estimand and its window


def make_episode(t0_ms: int) -> t.Episode:
    entry, exit_ = s1.entry_clock(t0_ms)
    return t.Episode("SYNTHUSDT", "2024-W50", t0_ms, t0_ms, entry, exit_)


def test_the_window_stays_inside_t0_plus_one_day_and_the_entry_minute() -> None:
    base = ms("2024-11-12T00:00:00")
    seen_hours: set[int] = set()
    for k in range(0, 4_000):
        t0 = base + k * 37_001  # off the minute, off the second, sweeps several hours
        ep = make_episode(t0)
        t.check_episode(ep)
        seen_hours.add(ep.hours)
        assert 143 <= ep.hours <= 144
        assert t0 + DAY <= ep.grid_a_ms < t0 + DAY + HOUR  # A: first grid point at or after T0+1d
        assert ep.entry_ms - HOUR < ep.grid_b_ms <= ep.entry_ms  # B: last grid point at or before E
        assert ep.first_start_ms >= t0 + DAY - HOUR > t0  # the first bar starts after T0
        assert ep.last_start_ms + HOUR == ep.grid_b_ms <= ep.entry_ms  # the last bar ends by E
    assert seen_hours == {143, 144}


def test_an_entry_on_the_hour_never_reads_the_bar_that_starts_at_e() -> None:
    t0 = ms("2024-11-12T10:00:00")  # E = T0 + 7 d exactly, on the hour
    ep = make_episode(t0)
    assert ep.entry_ms % HOUR == 0 and ep.grid_b_ms == ep.entry_ms and ep.hours == 144
    params = dict(t.proxy_request(ep).params)
    assert int(params["end"]) == ep.entry_ms - HOUR  # the bar that starts at E is not requested
    assert int(params["start"]) == ep.grid_a_ms - HOUR
    assert (params["interval"], params["limit"], params["category"]) == ("60", "200", "linear")


def test_an_episode_whose_path_leaves_the_window_is_refused_before_any_request() -> None:
    late = make_episode(ms("2025-09-20T10:00:00"))  # V1 exit after 2025-10-01
    with pytest.raises(ValueError, match="after the cut"):
        t.proxy_request(late)
    early = make_episode(ms("2024-10-05T10:00:00"))  # E - 30 d before 2024-10-01
    with pytest.raises(ValueError, match="lookback"):
        t.proxy_request(early)
    boundary = make_episode(ms("2025-09-17T00:00:00"))  # E + 7 d == the cut: allowed
    assert boundary.exit_ms == s1.CUT_MS
    t.check_episode(boundary)
    after = make_episode(ms("2025-09-17T00:00:00") + 61_000)  # one minute past: censored
    with pytest.raises(ValueError, match="after the cut"):
        t.check_episode(after)


def test_a_wrong_clock_in_the_cohort_file_is_refused() -> None:
    ep = make_episode(ms("2024-11-12T00:00:00"))
    with pytest.raises(ValueError, match="entry clock"):
        t.check_episode(replace(ep, entry_ms=ep.entry_ms + 60_000))


# The reducer: what is parsed


def payload(ep: t.Episode, rows: list[list[Any]], symbol: str | None = None) -> dict[str, Any]:
    return {
        "retCode": 0,
        "retMsg": "OK",
        "result": {"symbol": symbol or ep.symbol, "category": "linear", "list": rows},
    }


def row(start: int, close: Any = GARBAGE, width: int = 7) -> list[Any]:
    return [str(start), "x", "x", "x", close, "x", "x"][:width]


@pytest.fixture
def ep() -> t.Episode:
    return make_episode(ms("2024-11-12T00:00:00") + 12_345)


def test_only_two_closes_are_parsed_and_only_their_log_ratio_survives(ep: t.Episode) -> None:
    rows = [row(ep.last_start_ms, "1100.55")]
    rows += [row(x) for x in range(ep.last_start_ms - HOUR, ep.first_start_ms, -HOUR)]
    rows += [row(ep.first_start_ms, "1000.5")]
    facts = t.reduce_proxy(payload(ep, rows), ep.symbol, ep.first_start_ms, ep.last_start_ms)
    assert facts["log_return"] == pytest.approx(math.log(1100.55 / 1000.5))
    assert facts["rows"] == facts["expected_rows"] == ep.hours + 1
    assert set(facts) == {"log_return", "rows", "expected_rows"}  # no close, no start time


def test_a_missing_endpoint_bar_is_an_undefined_proxy_never_imputed(ep: t.Episode) -> None:
    inner = [row(x) for x in range(ep.last_start_ms - HOUR, ep.first_start_ms, -HOUR)]
    for rows in (
        [row(ep.last_start_ms, "5"), *inner],
        [*inner, row(ep.first_start_ms, "5")],
        inner,
        [],
    ):
        facts = t.reduce_proxy(payload(ep, rows), ep.symbol, ep.first_start_ms, ep.last_start_ms)
        assert facts["log_return"] is None


@pytest.mark.parametrize(
    ("offset", "reason"),
    [
        (0, "row_outside_range"),  # last_start + 1 h: the bar that ends after B (at or after E)
        (1, "row_outside_range"),
        (-1, "row_outside_range"),  # before the first bar
        (2, "row_outside_range"),
    ],
)
def test_a_row_outside_the_requested_range_stops_before_any_other_field_is_read(
    ep: t.Episode, offset: int, reason: str
) -> None:
    starts = {
        0: ep.last_start_ms + HOUR,  # starts at B: ends after B, may straddle or follow E
        1: ep.entry_ms,  # the bar that starts at the entry minute
        -1: ep.first_start_ms - HOUR,
        2: ep.last_start_ms + 90_000,  # not on the hour grid
    }
    rows = [row(starts[offset], close="not read")]
    with pytest.raises(s1.SchemaMismatchError) as caught:
        t.reduce_proxy(payload(ep, rows), ep.symbol, ep.first_start_ms, ep.last_start_ms)
    assert caught.value.reason == reason


@pytest.mark.parametrize(
    ("rows_of", "reason"),
    [
        (lambda e: [row(e.first_start_ms), row(e.first_start_ms)], "duplicate_start"),
        (lambda e: [row(e.first_start_ms, width=6)], "row"),
        (lambda e: [["x"] * 7], "bad:start"),
        (lambda e: [row(e.first_start_ms)] * 200, "page_full"),
    ],
)
def test_malformed_pages_stop_the_run(
    ep: t.Episode, rows_of: Callable[[t.Episode], list[Any]], reason: str
) -> None:
    with pytest.raises(s1.SchemaMismatchError) as caught:
        t.reduce_proxy(payload(ep, rows_of(ep)), ep.symbol, ep.first_start_ms, ep.last_start_ms)
    assert caught.value.reason == reason


def test_wrong_symbol_list_or_envelope_stop_the_run(ep: t.Episode) -> None:
    good = [row(ep.first_start_ms, "5"), row(ep.last_start_ms, "5")]
    cases = [
        (payload(ep, good, symbol="OTHERUSDT"), "symbol"),
        ({"retCode": 0, "result": {"symbol": ep.symbol, "list": "x"}}, "list"),
        ({"retCode": 10001, "retMsg": "m", "result": {}}, "envelope"),
        ({"retCode": True, "result": {}}, "envelope"),
        ({"retCode": 0, "result": []}, "result"),
        ([], "envelope"),
    ]
    for body, reason in cases:
        with pytest.raises(s1.SchemaMismatchError) as caught:
            t.reduce_proxy(body, ep.symbol, ep.first_start_ms, ep.last_start_ms)
        assert caught.value.reason == reason


@pytest.mark.parametrize("bad", ["0", "-1", "1e5", "nan", "", "0x10", 5, None, "1,5"])
def test_a_bad_endpoint_close_stops_the_run(ep: t.Episode, bad: Any) -> None:
    rows = [row(ep.first_start_ms, "5"), row(ep.last_start_ms, bad)]
    with pytest.raises(s1.SchemaMismatchError) as caught:
        t.reduce_proxy(payload(ep, rows), ep.symbol, ep.first_start_ms, ep.last_start_ms)
    assert caught.value.reason == "bad:close"


# The planner and the cohort from the sealed Stage 1 run


def test_the_cohort_is_the_primary_episodes_and_the_censored_launches_are_not_in_it(
    stage1: dict[str, Any],
) -> None:
    episodes: list[t.Episode] = stage1["episodes"]
    rows: list[dict[str, Any]] = stage1["rows"]
    counts = t.reason_counts(rows)
    assert len(episodes) == counts["primary"] == 228
    assert counts["window_censored"] == 1 and counts["lookback_censored"] == 1
    assert {e.symbol for e in episodes}.isdisjoint({"LATE000USDT", "EARLY00USDT"})
    assert [e.launch_ms for e in episodes] == sorted(e.launch_ms for e in episodes)
    for e in episodes:
        t.check_episode(e)
        assert e.t0_ms == e.launch_ms + 10 * 60_000  # the first trade of the synthetic world
    assert {e.week for e in episodes} == {r["week"] for r in rows if r["reason"] == "primary"}


def test_the_cohort_must_match_the_reviewed_report_and_the_pinned_size(
    stage1: dict[str, Any], tmp_path: Path
) -> None:
    root, report = stage1["root"], stage1["report"]
    manifest = s1.manifest_sha256(root)
    assert len(t.derive_cohort(root, report, manifest_sha256=manifest, bound=None)) == 228
    with pytest.raises(ValueError, match="manifest"):
        t.derive_cohort(root, report, manifest_sha256="0" * 64, bound=None)
    with pytest.raises(ValueError, match="differs from the reviewed"):
        t.derive_cohort(root, {**report, "n_primary": 227}, manifest_sha256=manifest, bound=None)
    with pytest.raises(ValueError, match="differs from the reviewed"):
        reasons = {**report["reasons"], "primary": 227, "funding_uncovered": 1}
        t.derive_cohort(root, {**report, "reasons": reasons}, manifest_sha256=manifest, bound=None)
    # the contract binds 228 episodes in 48 weeks: the synthetic year has other weeks
    with pytest.raises(ValueError, match="228 episodes in 48 weeks"):
        t.derive_cohort(root, report, manifest_sha256=manifest)


def test_the_reviewed_stage1_evidence_in_the_repository_matches_its_pins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = t.load_reviewed_report(REPO)
    assert report["n_primary"] == t.COHORT_EPISODES == 228
    assert report["iso_weeks"] == t.COHORT_WEEKS == 48
    assert report["reasons"]["primary"] == 228 and report["stop"] is None
    monkeypatch.setattr(t, "STAGE1_REPORT_SHA256", "0" * 64)
    with pytest.raises(ValueError, match="pinned hash"):
        t.load_reviewed_report(REPO)


def test_an_incomplete_or_stopped_stage1_run_is_not_a_prerequisite(
    stage1: dict[str, Any], tmp_path: Path
) -> None:
    import shutil

    copy = tmp_path / "copy"
    shutil.copytree(stage1["root"], copy)
    (copy / "go2_done.json").unlink()
    with pytest.raises(ValueError, match="not complete"):
        t.derive_cohort(
            copy, stage1["report"], manifest_sha256=s1.manifest_sha256(copy), bound=None
        )


# The full run: requests, receipts, cut


def test_one_request_per_episode_on_the_kline_endpoint_ending_before_every_entry(
    stage1: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    episodes: list[t.Episode] = stage1["episodes"]
    server = Hourly(episodes, draw_returns(episodes))
    run_full(tmp_path, episodes, server, monkeypatch)
    assert len(server.requests) == 228
    by_symbol = {e.symbol: e for e in episodes}
    seen: set[str] = set()
    for request in server.requests:
        q = query(request)
        e = by_symbol[q["symbol"]]
        seen.add(e.symbol)
        assert (request.url.host, request.url.path) == ("api.bybit.com", "/v5/market/kline")
        assert (q["category"], q["interval"], q["limit"]) == ("linear", "60", "200")
        assert int(q["end"]) + HOUR <= e.entry_ms  # the last bar ends at or before E
        assert int(q["end"]) + HOUR + 7 * DAY <= s1.CUT_MS  # and the hold week stays in the year
        assert int(q["start"]) >= e.t0_ms + DAY - HOUR
        assert int(q["end"]) < ms("2025-09-24T00:00:00")  # nothing near the cut, nothing later
    assert seen == set(by_symbol)  # none twice, none missing, no censored launch
    assert not seen & {"LATE000USDT", "EARLY00USDT"}


def test_no_close_price_is_kept_in_the_sealed_root(
    stage1: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    episodes: list[t.Episode] = stage1["episodes"]
    server = Hourly(episodes, draw_returns(episodes))
    run_full(tmp_path, episodes, server, monkeypatch)
    text = "".join(p.read_text() for p in (tmp_path / "run").rglob("*.json"))
    assert START_CLOSE not in text and GARBAGE not in text
    assert not any(close in text for close in server.end_closes)
    receipt = json.loads(sorted((tmp_path / "run" / "receipts").glob("*.json"))[0].read_text())
    assert set(receipt["facts"]) == {"log_return", "rows", "expected_rows"}
    assert "schema" in receipt and all(":" in k for k in receipt["schema"])  # key paths and types


def test_the_log_return_is_the_only_price_derived_value_and_is_exact(
    stage1: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    episodes: list[t.Episode] = stage1["episodes"]
    returns = draw_returns(episodes)
    server = Hourly(episodes, returns)
    run_full(tmp_path, episodes, server, monkeypatch)
    results, _ = t.results_by_symbol(tmp_path / "run")
    for e in episodes:
        assert t._facts(results[e.symbol]) == pytest.approx(returns[e.symbol], abs=1e-7)


# The error envelope, through the real fetcher


def one(tmp_path: Path, ep: t.Episode, server: Hourly) -> tuple[s1.Fetcher, Clock, dict[str, Any]]:
    f, clock = fetcher_for(tmp_path / "run", server)
    result = f.run_request(t.proxy_request(ep), t.proxy_reducer(ep), keep_facts=True)
    return f, clock, result


@pytest.mark.parametrize("code", sorted(s1.RET_RATE_LIMIT | s1.RET_TRANSIENT))
def test_retryable_ret_codes_are_retried_and_only_the_code_and_message_are_stored(
    stage1: dict[str, Any], tmp_path: Path, code: int
) -> None:
    episodes: list[t.Episode] = stage1["episodes"]
    e = episodes[0]
    server = Hourly(
        episodes, draw_returns(episodes), errors=[(e.symbol, code, "too many" + "x" * 300)]
    )
    f, clock, result = one(tmp_path, e, server)
    assert result["outcome"] == "ok" and len(server.requests) == 2
    failed = json.loads((tmp_path / "run" / "receipts" / "00001.json").read_text())
    assert failed["ret_code"] == code and len(failed["ret_msg"]) == s1.MAX_RET_MSG
    assert failed["outcome"] == "error" and "facts" not in failed
    if code in s1.RET_RATE_LIMIT:
        assert f.spacing == 2 * s1.SPACING_S  # a rate limit slows the next requests
    else:
        assert f.spacing == s1.SPACING_S
    assert any(wait >= s1.BACKOFF_S[0] for wait in clock.sleeps)


def test_any_other_ret_code_stops_the_run_naming_it_and_the_stop_persists(
    stage1: dict[str, Any], tmp_path: Path
) -> None:
    episodes: list[t.Episode] = stage1["episodes"]
    e = episodes[0]
    server = Hourly(episodes, draw_returns(episodes), errors=[(e.symbol, 10001, "params error")])
    f, _ = fetcher_for(tmp_path / "run", server)
    with pytest.raises(s1.FetchStoppedError, match="retCode 10001"):
        f.run_request(t.proxy_request(e), t.proxy_reducer(e), keep_facts=True)
    assert json.loads((tmp_path / "run" / "stop.json").read_text())["reason"].endswith("10001")
    with pytest.raises(s1.FetchStoppedError, match="was stopped"):
        fetcher_for(tmp_path / "run", server)  # a stopped root never continues
    assert len(server.requests) == 1  # no retry of a fatal code


def test_a_boolean_or_float_ret_code_is_a_schema_mismatch_not_a_success(
    stage1: dict[str, Any], tmp_path: Path
) -> None:
    episodes: list[t.Episode] = stage1["episodes"]
    for k, bad in enumerate((True, 0.0, "0", None)):
        e = episodes[k]
        server = Hourly(episodes, draw_returns(episodes), errors=[(e.symbol, bad, "OK")])
        f, _ = fetcher_for(tmp_path / f"run{k}", server)
        with pytest.raises(s1.FetchStoppedError, match=r"schema mismatch \(envelope\)"):
            f.run_request(t.proxy_request(e), t.proxy_reducer(e), keep_facts=True)


def test_three_failed_sends_make_the_proxy_undefined_not_a_stop(
    stage1: dict[str, Any], tmp_path: Path
) -> None:
    episodes: list[t.Episode] = stage1["episodes"]
    e = episodes[0]
    errors = [(e.symbol, 10016, "busy")] * 3
    server = Hourly(episodes, draw_returns(episodes), errors=list(errors))
    _, _, result = one(tmp_path, e, server)
    assert result["outcome"] == "unavailable" and len(server.requests) == 3
    assert not (tmp_path / "run" / "stop.json").exists()
    assert t._facts(result) is None


def test_a_row_after_the_entry_minute_in_a_response_stops_the_run_unread(
    stage1: dict[str, Any], tmp_path: Path
) -> None:
    episodes: list[t.Episode] = stage1["episodes"]
    e = episodes[0]
    server = Hourly(episodes, draw_returns(episodes), extra_row={e.symbol: e.entry_ms})
    f, _ = fetcher_for(tmp_path / "run", server)
    with pytest.raises(s1.FetchStoppedError, match="row_outside_range"):
        f.run_request(t.proxy_request(e), t.proxy_reducer(e), keep_facts=True)
    receipt = json.loads((tmp_path / "run" / "receipts" / "00001.json").read_text())
    assert receipt["outcome"] == "schema_mismatch" and "facts" not in receipt


# Roots: fresh, stopped, resumed


def test_a_sealed_root_inside_the_repository_is_refused() -> None:
    with pytest.raises(ValueError, match="outside the repository"):
        t.prepare_root(REPO / "docs" / "sealed-root-must-not-exist")
    assert not (REPO / "docs" / "sealed-root-must-not-exist").exists()


def test_the_cohort_summary_is_aggregates_only_and_the_command_needs_the_pinned_root(
    stage1: dict[str, Any], tmp_path: Path
) -> None:
    summary = t.cohort_summary(stage1["episodes"], "e" * 64)
    assert summary["source_fingerprint"] == "e" * 64
    assert summary["episodes"] == 228 and summary["slots_k"] >= 1
    assert summary["variant_episodes_lower_bound"]["V1"] == 228
    text = json.dumps(summary)
    assert not any(e.symbol in text for e in stage1["episodes"])
    with pytest.raises(ValueError, match="manifest"):
        t.main(
            ["cohort", "--stage1-root", str(stage1["root"])]
        )  # synthetic root, not the pinned one


def test_a_root_with_files_and_no_run_binding_is_refused_and_a_fresh_one_is_private(
    tmp_path: Path,
) -> None:
    fresh = tmp_path / "fresh"
    t.prepare_root(fresh)
    assert fresh.stat().st_mode & 0o777 == 0o700
    dirty = tmp_path / "dirty"
    dirty.mkdir()
    (dirty / "stray.json").write_text("{}")
    with pytest.raises(ValueError, match="no run binding"):
        t.prepare_root(dirty)


def test_a_resume_under_different_code_is_refused_and_under_the_same_code_continues(
    stage1: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(t, "BOOTSTRAP_DRAWS", 400)
    episodes: list[t.Episode] = stage1["episodes"]
    returns = draw_returns(episodes)
    crash = [episodes[100].symbol]
    server = Hourly(episodes, returns, crash=crash)
    root = tmp_path / "run"
    code = {"revision": "aaa"}
    f, _ = fetcher_for(root, server, code=code)
    with pytest.raises(RuntimeError, match="died"):
        t.run_stage3(f, episodes)
    assert t.evaluate(root)["verdict"] == "stage3_incomplete"
    assert "dispersion" not in t.evaluate(root)  # no statistic from a partial run
    with pytest.raises(ValueError, match="binding differs"):
        fetcher_for(root, server, code={"revision": "bbb"})
    changed = rehash({**t.build_contract(), "budget": {}})
    with pytest.raises(ValueError, match="binding differs"):
        fetcher_for(root, server, contract=changed, code=code)
    g, _ = fetcher_for(root, server, code=code)
    t.run_stage3(g, episodes)
    assert t.evaluate(root)["verdict"] in {"power_gate_passed", "insufficient_power"}
    assert len(server.requests) == 229  # 228 plus the one send that died


def test_a_different_cohort_in_an_existing_root_is_refused(
    stage1: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    episodes: list[t.Episode] = stage1["episodes"]
    server = Hourly(episodes, draw_returns(episodes))
    run_full(tmp_path, episodes, server, monkeypatch)
    f, _ = fetcher_for(tmp_path / "run", server)
    with pytest.raises(ValueError, match="different cohort"):
        t.run_stage3(f, [*episodes[1:], replace(episodes[0], launch_ms=episodes[0].launch_ms + 1)])


def test_a_cohort_of_another_size_is_refused_before_any_request(
    stage1: dict[str, Any], tmp_path: Path
) -> None:
    episodes: list[t.Episode] = stage1["episodes"]
    server = Hourly(episodes, draw_returns(episodes))
    f, _ = fetcher_for(tmp_path / "run", server)
    with pytest.raises(ValueError, match="size the contract binds"):
        t.run_stage3(f, episodes[:-1])
    assert server.requests == []


# Statistics


def test_scaling_and_the_planning_variable_by_hand() -> None:
    z144, y144 = t.scaled_bps(0.10, 144)
    assert z144 == pytest.approx(0.10 * math.sqrt(7 / 6))  # the protocol's 7/6 at 144 hours
    assert y144 == pytest.approx(1e4 * (math.exp(z144) - 1))
    z143, _ = t.scaled_bps(0.10, 143)
    assert z143 == pytest.approx(0.10 * math.sqrt(168 / 143)) and z143 > z144
    assert t.scaled_bps(0.0, 143) == (0.0, 0.0)


def test_sample_variance_and_the_top5_share_are_the_textbook_numbers() -> None:
    values = [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 64.0]
    assert t.sample_variance(values) == pytest.approx(statistics.variance(values))
    assert 0 < t.top5_share(values) < 1 and t.top5_share(values) > 0.9
    with pytest.raises(ValueError, match="two values"):
        t.sample_variance([1.0])


def items_of(
    weeks: int, per_week: int, *, seed: int, week_sd: float, noise_sd: float
) -> list[tuple[int, float, float]]:
    rng = random.Random(seed)  # noqa: S311
    out: list[tuple[int, float, float]] = []
    for w in range(weeks):
        common = rng.gauss(0, week_sd)
        for _ in range(per_week):
            z = common + rng.gauss(0, noise_sd)
            out.append((2_900 + w, 1e4 * math.expm1(z), z))
    return out


def test_independent_weeks_give_a_design_effect_near_one_and_a_common_factor_a_large_one() -> None:
    flat = t.calibrate(items_of(200, 5, seed=3, week_sd=0.0, noise_sd=0.2), draws=1_000)
    for block in ("2", "4", "8"):
        assert 0.75 < flat["dependence"]["blocks"][block]["deff_cluster_weighted"] < 1.35
    common = t.calibrate(items_of(40, 5, seed=4, week_sd=0.4, noise_sd=0.05), draws=2_000)
    assert common["dependence"]["deff_largest"] > 3.0
    assert common["dependence"]["n_eff"] < 200 / 3.0


def test_the_largest_block_decides_never_the_favorable_one() -> None:
    items: list[tuple[int, float, float]] = []
    for w in range(48):  # a slow regime: neighbouring weeks move together
        common = 0.5 * math.sin(w / 4.0)
        for _ in range(5):
            z = common + 0.02 * ((w * 7 + len(items)) % 5 - 2)
            items.append((2_900 + w, 1e4 * math.expm1(z), z))
    out = t.calibrate(items, draws=2_000)
    deffs = [out["dependence"]["blocks"][b]["deff_cluster_weighted"] for b in ("2", "4", "8")]
    assert max(deffs) > deffs[0]  # a longer block sees more dependence than block 2 here
    assert out["dependence"]["deff_largest"] == max(deffs)
    assert out["dependence"]["deff_used"] == max(max(deffs), t.DEFF_FLOOR)
    assert out["power"]["se_bps"] == pytest.approx(
        math.sqrt(out["dependence"]["deff_used"] * out["dispersion"]["sd_bps"] ** 2 / 240)
    )


def test_mde_is_two_point_eight_times_the_standard_error_with_the_protocol_identity() -> None:
    items = items_of(40, 5, seed=5, week_sd=0.1, noise_sd=0.1)
    out = t.calibrate(items, draws=2_000)
    sd, n = out["dispersion"]["sd_bps"], len(items)
    assert sd == pytest.approx(statistics.stdev(b for _, b, _ in items))
    expected = 2.80 * sd * math.sqrt(out["dependence"]["deff_used"]) / math.sqrt(n)
    assert out["power"]["mde_gate_bps"] == pytest.approx(expected, rel=1e-12)
    assert out["power"]["sensitivity_bps"]["2.487"] == pytest.approx(2.487 * out["power"]["se_bps"])
    assert out["power"]["sensitivity_bps"]["2.123"] == pytest.approx(2.123 * out["power"]["se_bps"])
    assert out["dependence"]["n_eff"] == pytest.approx(n / out["dependence"]["deff_used"])


def test_a_design_effect_below_one_is_floored_at_independence() -> None:
    items: list[tuple[int, float, float]] = []
    for w in range(40):  # two opposite episodes per week: every weekly mean is exactly zero
        for z in (0.2 + 0.01 * (w % 3), -(0.2 + 0.01 * (w % 3))):
            items.append((2_900 + w, 1e4 * math.expm1(z), z))
    out = t.calibrate(items, draws=500)
    assert out["dependence"]["deff_largest"] < 1.0 and out["dependence"]["deff_floor_applied"]
    assert out["dependence"]["deff_used"] == 1.0
    assert out["power"]["se_bps"] == pytest.approx(out["dispersion"]["sd_bps"] / math.sqrt(80))


def test_a_degenerate_variance_is_not_estimable() -> None:
    flat = [(2_900 + w, 100.0, 0.01) for w in range(30) for _ in range(5)]
    with pytest.raises(t.NotEstimableError) as caught:
        t.calibrate(flat, draws=100)
    assert caught.value.reason == "zero_variance"
    with pytest.raises(t.NotEstimableError) as empty:
        t.block_bootstrap([(0.0, 0)] * 8, 2, 10, 1)  # every drawn week is empty
    assert empty.value.reason == "empty_draw"
    with pytest.raises(t.NotEstimableError) as short:
        t.block_bootstrap([(1.0, 1)] * 5, 8, 10, 1)
    assert short.value.reason == "block_longer_than_weeks"
    assert {"zero_variance", "empty_draw", "block_longer_than_weeks"} <= set(
        t.NOT_ESTIMABLE_REASONS
    )


def test_the_gate_compares_unrounded_values_at_exactly_400(monkeypatch: pytest.MonkeyPatch) -> None:
    items = items_of(40, 5, seed=6, week_sd=0.1, noise_sd=0.1)
    mde = t.calibrate(items, draws=300)["power"]["mde_gate_bps"]
    with monkeypatch.context() as patch:
        patch.setattr(t, "MDE_LIMIT_BPS", mde)
        assert t.calibrate(items, draws=300)["power"]["passes"] is True  # at the limit: passes
    with monkeypatch.context() as patch:
        patch.setattr(t, "MDE_LIMIT_BPS", math.nextafter(mde, 0.0))
        assert t.calibrate(items, draws=300)["power"]["passes"] is False  # one ulp over: stops


def test_every_dispersion_statistic_is_invariant_to_the_mean_level() -> None:
    rng = random.Random(9)  # noqa: S311
    base = [rng.gauss(0, 1) for _ in range(120)]
    shifted = [x + 1_000.0 for x in base]
    assert t.sample_variance(shifted) == pytest.approx(t.sample_variance(base), rel=1e-9)
    assert t.top5_share(shifted) == pytest.approx(t.top5_share(base), rel=1e-9)
    weeks_a = t.week_sequence([(w // 4, x) for w, x in enumerate(base)])
    weeks_b = t.week_sequence([(w // 4, x) for w, x in enumerate(shifted)])
    for block in (2, 4, 8):
        a = t.block_bootstrap(weeks_a, block, 500, 1)
        b = t.block_bootstrap(weeks_b, block, 500, 1)
        assert b[0] == pytest.approx(a[0], rel=1e-6) and b[1] == pytest.approx(a[1], rel=1e-6)


def test_the_bootstrap_is_deterministic_and_an_empty_week_stays_in_the_sequence() -> None:
    items = [(10, 1.0), (10, 3.0), (12, 2.0), (13, 5.0), (13, 7.0), (14, 1.0)] * 3
    weeks = t.week_sequence(items)
    assert [n for _, n in weeks] == [6, 0, 3, 6, 3]  # week 11 is empty and counted
    assert t.block_bootstrap(weeks, 2, 300, 7) == t.block_bootstrap(weeks, 2, 300, 7)
    with pytest.raises(t.NotEstimableError, match="block_longer"):
        t.block_bootstrap(weeks, 8, 10, 7)
    assert t.week_ordinal("2024-W50") + 1 == t.week_ordinal("2024-W51")
    assert t.week_ordinal("2024-W52") + 1 == t.week_ordinal("2025-W01")


# The decision table


@pytest.mark.parametrize(
    ("cohort", "defined", "weeks", "power_ok", "verdict"),
    [
        (228, 217, 48, True, "power_gate_passed"),  # 217 * 100 >= 95 * 228
        (228, 216, 48, True, "incomplete_coverage"),  # 216 * 100 < 21,660
        (228, 216, 20, False, "incomplete_coverage"),  # coverage outranks events and power
        (100, 100, 26, True, "power_gate_passed"),  # exactly the floors
        (100, 99, 26, True, "insufficient_events"),
        (100, 100, 25, True, "insufficient_events"),
        (228, 228, 48, None, "not_estimable"),
        (228, 228, 48, False, "insufficient_power"),
        (228, 228, 25, False, "insufficient_events"),  # events outrank power
    ],
)
def test_the_decision_table_picks_the_first_applicable_row(
    cohort: int, defined: int, weeks: int, power_ok: bool | None, verdict: str
) -> None:
    assert t.decide(cohort, defined, weeks, power_ok) == verdict


# The variants and the portfolio


def test_variant_floors_and_their_own_cuts() -> None:
    def ep(day: str, week: str = "2025-W01") -> t.Episode:
        return replace(make_episode(ms(f"{day}T00:00:00")), week=week)

    early = ep("2024-10-25")  # V1 ok (E 11-01); V2 lookback cut (E 10-28); V3 ok
    late = ep("2025-09-12")  # V1 exit 09-26; V3 exit 10-03 after the cut
    middle = ep("2025-03-01")
    table = t.variant_table([early, late, middle], 100.0)
    assert [table[k]["episodes"] for k in ("V1", "V2", "V3")] == [3, 2, 2]
    assert table["V1"]["mde_bps"] == pytest.approx(100.0)
    assert table["V1"]["basis"] == "calibrated"
    assert table["V2"]["basis"] == table["V3"]["basis"] == "conditional_extrapolation_from_v1_se"
    assert table["V2"]["mde_bps"] == pytest.approx(100.0 * math.sqrt(3 / 2))
    assert not any(table[k]["floors_met"] for k in table)
    many = [replace(middle, week=f"2025-W{w % 40 + 1:02d}", symbol=f"S{w}USDT") for w in range(120)]
    assert all(t.variant_table(many, 50.0)[k]["floors_met"] for k in ("V1", "V2", "V3"))


def test_slot_count_is_the_smallest_k_with_ninety_percent_entered() -> None:
    week = 7 * DAY
    assert t.slots_needed([(i * week, (i + 1) * week) for i in range(4)]) == (1, 4)  # exit first
    assert t.slots_needed([(0, week)] * 10) == (9, 9)  # 9 of 10 enter at k = 9, 8 of 10 at k = 8
    rng = random.Random(2)  # noqa: S311
    starts = [rng.randrange(0, 300 * DAY) for _ in range(120)]
    spans = [(start, start + week) for start in starts]

    def entered_with(k: int) -> int:
        """A plain list-based reference: release at X first, then admit if a slot is free."""
        slots: list[int] = []
        count = 0
        for entry, exit_ in sorted(spans):
            slots = [x for x in slots if x > entry]
            if len(slots) < k:
                slots.append(exit_)
                count += 1
        return count

    expected_k = next(k for k in range(1, 121) if entered_with(k) * 100 >= 90 * len(spans))
    shuffled = spans[:]
    rng.shuffle(shuffled)
    expected = (expected_k, entered_with(expected_k))
    assert t.slots_needed(shuffled) == expected == t.slots_needed(spans)
    assert expected_k > 1 and entered_with(expected_k - 1) * 100 < 90 * len(spans)


def test_the_hurdle_arithmetic_matches_protocol_6_4_and_the_dollar_scale() -> None:
    table = t.portfolio_table({"k": 8, "entered": 205}, 400.0)
    assert table["hurdle_required_bps"] == pytest.approx(0.05 * 1e4 / (205 / 8))  # about 20 bps
    assert table["roc_annual_at_mde"] == pytest.approx(0.04 * 205 / 8)
    assert table["mde_over_hurdle"] == pytest.approx(400.0 / table["hurdle_required_bps"])
    usd = table["usd_per_year"]
    assert usd["5000"]["hurdle"] == pytest.approx(250.0) and usd["50"]["hurdle"] == pytest.approx(
        2.5
    )
    assert usd["5000"]["at_mde"] == pytest.approx(table["roc_annual_at_mde"] * 5000)


# Verdicts end to end, from sealed files, and the output firewall


def keys_of(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        for k, inner in value.items():
            yield str(k)
            yield from keys_of(inner)
    elif isinstance(value, list):
        for inner in value:
            yield from keys_of(inner)


def assert_firewall(report: dict[str, Any], episodes: list[t.Episode]) -> None:
    assert set(report) <= t.REPORT_KEYS and report["returns_read"] is False
    for key in keys_of(report):
        lowered = key.lower()
        assert not any(name in lowered for name in t.FORBIDDEN_NAMES), key
    text = json.dumps(report)
    assert not any(e.symbol in text for e in episodes)
    assert START_CLOSE not in text and GARBAGE not in text
    assert re.search(r"(?<![0-9a-f.])\d{13}(?![0-9a-f.])", text) is None  # no ms timestamp
    assert not any(isinstance(x, list) and len(x) > 3 for x in _lists(report))


def _lists(value: Any) -> Iterator[Any]:
    if isinstance(value, dict):
        for inner in value.values():
            yield from _lists(inner)
    elif isinstance(value, list):
        yield value
        for inner in value:
            yield from _lists(inner)


def test_a_dispersed_cohort_stops_at_insufficient_power_before_any_return(
    stage1: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    episodes: list[t.Episode] = stage1["episodes"]
    server = Hourly(episodes, draw_returns(episodes, sd=0.6, week_sd=0.2))
    report = run_full(tmp_path, episodes, server, monkeypatch)
    assert report["verdict"] == "insufficient_power" and report["stop_code"] == "insufficient_power"
    assert report["power"]["mde_gate_bps"] > 400.0 and report["power"]["passes"] is False
    assert report["cohort"]["defined"] == 228 and report["cohort"]["slots_k"] >= 1
    assert_firewall(report, episodes)


def test_a_quiet_cohort_passes_the_gate_and_the_report_is_recomputable(
    stage1: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    episodes: list[t.Episode] = stage1["episodes"]
    returns = draw_returns(episodes, sd=0.05, week_sd=0.02)
    report = run_full(tmp_path, episodes, Hourly(episodes, returns), monkeypatch)
    assert report["verdict"] == "power_gate_passed" and report["stop_code"] is None
    assert report["power"]["passes"] is True and report["power"]["mde_gate_bps"] <= 400.0
    # an independent recomputation of the standard deviation from the controlled inputs
    ys = [t.scaled_bps(returns[e.symbol], e.hours)[1] for e in episodes]
    assert report["dispersion"]["sd_bps"] == pytest.approx(statistics.stdev(ys), rel=1e-6)
    zs = [t.scaled_bps(returns[e.symbol], e.hours)[0] for e in episodes]
    assert report["dispersion"]["sd_log_7d"] == pytest.approx(statistics.stdev(zs), rel=1e-6)
    assert report["window"]["hours_between_grid_points"].keys() == {"143", "144"}
    assert sum(report["window"]["hours_between_grid_points"].values()) == 228
    assert report["window"]["hour_rows_missing_share"] == 0.0
    assert set(report["variants"]) == {"V1", "V2", "V3"} and "portfolio" in report
    assert report["portfolio"]["slots_k"] == report["cohort"]["slots_k"]
    assert_firewall(report, episodes)


def test_default_bootstrap_size_is_used_by_the_run_itself(
    stage1: dict[str, Any], tmp_path: Path
) -> None:
    episodes: list[t.Episode] = stage1["episodes"]
    server = Hourly(episodes, draw_returns(episodes, sd=0.05))
    report = run_full(tmp_path, episodes, server)
    assert t.BOOTSTRAP_DRAWS == 10_000 and report["verdict"] == "power_gate_passed"
    assert set(report["dependence"]["blocks"]) == {"2", "4", "8"}


def test_coverage_below_ninety_five_percent_stops_without_any_statistic(
    stage1: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    episodes: list[t.Episode] = stage1["episodes"]
    gone = frozenset(e.symbol for e in episodes[:12])  # 216 of 228 defined: 94.7%
    server = Hourly(episodes, draw_returns(episodes), missing_end=gone)
    report = run_full(tmp_path, episodes, server, monkeypatch)
    assert report["verdict"] == "incomplete_coverage" and report["cohort"]["defined"] == 216
    assert report["stop_code"] == "incomplete_coverage"
    assert not {"dispersion", "dependence", "power", "variants", "portfolio"} & set(report)
    assert_firewall(report, episodes)


def test_coverage_at_exactly_ninety_five_percent_goes_on(
    stage1: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    episodes: list[t.Episode] = stage1["episodes"]
    gone = frozenset(e.symbol for e in episodes[:11])  # 217 of 228 defined: 95.18%
    server = Hourly(episodes, draw_returns(episodes), missing_end=gone)
    report = run_full(tmp_path, episodes, server, monkeypatch)
    assert report["cohort"]["defined"] == 217 and report["verdict"] == "power_gate_passed"
    assert report["window"]["hour_rows_missing_share"] > 0  # time-only coverage diagnostic


def test_a_cohort_with_one_value_is_not_estimable(
    stage1: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    episodes: list[t.Episode] = stage1["episodes"]
    server = Hourly(episodes, {e.symbol: 0.01 for e in episodes})
    report = run_full(tmp_path, episodes, server, monkeypatch)
    assert report["verdict"] == "not_estimable" and report["stop_code"] is None
    assert report["reason"] == "zero_variance" and "power" not in report
    assert_firewall(report, episodes)


def test_a_fatal_code_mid_run_leaves_a_stopped_root_with_counts_only(
    stage1: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(t, "BOOTSTRAP_DRAWS", 400)
    episodes: list[t.Episode] = stage1["episodes"]
    server = Hourly(episodes, draw_returns(episodes), errors=[(episodes[50].symbol, 10003, "key")])
    root = tmp_path / "run"
    f, _ = fetcher_for(root, server)
    with pytest.raises(s1.FetchStoppedError, match="10003"):
        t.run_stage3(f, episodes)
    report = t.evaluate(root)
    assert report["verdict"] == "stage3_stopped" and report["run"]["api_error_codes"] == {
        "10003": 1
    }
    assert report["run"]["requests_finished"] == 50 and "power" not in report
    assert_firewall(report, episodes)


def test_a_tampered_cohort_file_is_refused(
    stage1: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    episodes: list[t.Episode] = stage1["episodes"]
    run_full(tmp_path, episodes, Hourly(episodes, draw_returns(episodes)), monkeypatch)
    path = tmp_path / "run" / "cohort.json"
    document = json.loads(path.read_text())
    document["episodes"][0]["week"] = "2024-W01"
    path.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="own hash"):
        t.evaluate(tmp_path / "run")


# The report is bound to the code and the inputs that produced the root


CODE = {
    "rules": "new_perp_stage3_v1",
    "revision": "a" * 40,
    "publication": {"apps/analytics/schurfer_analytics/new_perp_stage3.py": "b" * 64},
    "request_list_sha256": t.build_contract()["request_list_sha256"],
}


def test_a_root_is_not_re_analysed_by_other_code_or_constants_under_its_old_hash(
    stage1: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: changing only the current MDE limit used to flip insufficient_power to
    power_gate_passed while the stored contract hash stayed the same."""
    episodes: list[t.Episode] = stage1["episodes"]
    server = Hourly(episodes, draw_returns(episodes, sd=0.6, week_sd=0.2))
    report = run_full(tmp_path, episodes, server, monkeypatch, code=CODE)
    assert report["verdict"] == "insufficient_power"
    root = tmp_path / "run"
    with monkeypatch.context() as patch:
        patch.setattr(t, "MDE_LIMIT_BPS", 1e9)  # the executing implementation now differs
        with pytest.raises(ValueError, match="another contract hash"):
            t.evaluate(root)
    with pytest.raises(ValueError, match="code binding differs"):
        t.evaluate(root, code={**CODE, "revision": "c" * 40})
    with pytest.raises(ValueError, match="code binding differs"):
        t.evaluate(root, code={**CODE, "publication": {"x": "y"}})
    assert t.evaluate(root, code=CODE)["verdict"] == "insufficient_power"


def test_the_report_exports_the_revision_the_publication_hashes_and_the_clean_tree_state(
    stage1: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    episodes: list[t.Episode] = stage1["episodes"]
    server = Hourly(episodes, draw_returns(episodes))
    run_full(tmp_path, episodes, server, monkeypatch, code=CODE)
    binding = t.evaluate(tmp_path / "run", code=CODE)["run"]["binding"]
    assert binding == {
        "rules": "new_perp_stage3_v1",
        "revision": "a" * 40,
        "publication": CODE["publication"],
        "clean_tree_at_run": True,
    }


def test_the_first_report_of_a_finished_root_is_stored_once_and_returned_unchanged(
    stage1: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    episodes: list[t.Episode] = stage1["episodes"]
    server = Hourly(episodes, draw_returns(episodes))
    run_full(tmp_path, episodes, server, monkeypatch, code=CODE)
    root = tmp_path / "run"
    first = t.report_once(root, code=CODE)
    assert (root / "report.json").exists() and first["verdict"] == "power_gate_passed"
    with monkeypatch.context() as patch:
        patch.setattr(t, "MDE_LIMIT_BPS", 1.0)  # a later version cannot change the stored report
        assert t.report_once(root, code=CODE) == first
        assert t.report_once(root) == first


def test_a_running_root_gets_no_stored_report(
    stage1: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    episodes: list[t.Episode] = stage1["episodes"]
    server = Hourly(episodes, draw_returns(episodes), crash=[episodes[10].symbol])
    root = tmp_path / "run"
    f, _ = fetcher_for(root, server)
    with pytest.raises(RuntimeError, match="died"):
        t.run_stage3(f, episodes, "f" * 64)
    assert t.report_once(root)["verdict"] == "stage3_incomplete"
    assert not (root / "report.json").exists()


def test_the_cohort_is_bound_to_the_stage1_source_files_not_only_to_aggregates(
    stage1: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: the manifest and the aggregate counts did not fix the Go 2 inputs. Moving a
    first-trade time inside its day changes no count, week or reason, only the fingerprint."""
    import shutil

    root: Path = stage1["root"]
    fingerprint = t.source_fingerprint(root)
    assert fingerprint == t.source_fingerprint(root) and len(fingerprint) == 64
    episodes: list[t.Episode] = stage1["episodes"]
    server = Hourly(episodes, draw_returns(episodes))
    run_full(tmp_path, episodes, server, monkeypatch, fingerprint=fingerprint)
    assert t.evaluate(tmp_path / "run", stage1_root=root)["verdict"] == "power_gate_passed"
    copy = tmp_path / "tampered"
    shutil.copytree(root, copy)
    target = next(
        p for p in sorted((copy / "receipts").glob("*.json")) if '"min_ms"' in p.read_text()
    )
    document = json.loads(target.read_text())
    document["facts"]["min_ms"] += 1_000  # one second later: same day, same class, same counts
    target.write_text(json.dumps(document))
    assert t.source_fingerprint(copy) != fingerprint
    with pytest.raises(ValueError, match="sources differ"):
        t.evaluate(tmp_path / "run", stage1_root=copy)
    # and the cohort derived from the tampered copy has the same counts and a different hash
    base = t.verified_cohort(
        root, report=stage1["report"], manifest_sha256=s1.manifest_sha256(root), bound=None
    )
    assert base[1] == fingerprint
    with pytest.raises(ValueError, match="owner approved"):
        t.verified_cohort(
            root,
            "0" * 64,
            report=stage1["report"],
            manifest_sha256=s1.manifest_sha256(root),
            bound=None,
        )
    stop = tmp_path / "stopped"
    shutil.copytree(root, stop)
    (stop / "stop.json").write_text("{}")
    with pytest.raises(ValueError, match="stop file"):
        t.source_fingerprint(stop)


def test_a_reserve_violation_receipt_keeps_the_root_stopped_after_a_crash(
    stage1: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stage 3 safeguard that does not depend on the fetcher's own recovery: a receipt with
    reserve_exceeded closes the root (reporting and running) even with no stop file."""
    episodes: list[t.Episode] = stage1["episodes"]
    server = Hourly(episodes, draw_returns(episodes), pad={episodes[3].symbol: 100_000})
    root = tmp_path / "run"
    f, _ = fetcher_for(root, server)

    def die(_self: s1.Fetcher, _reason: str) -> None:
        raise RuntimeError("the process died before the stop file")

    with monkeypatch.context() as patch:
        patch.setattr(s1.Fetcher, "record_stop", die)
        with pytest.raises(RuntimeError, match="died"):
            t.run_stage3(f, episodes, "f" * 64)
    assert not (root / "stop.json").exists() and t.reserve_violation(root)
    report = t.evaluate(root)
    assert report["verdict"] == "stage3_stopped" and "dispersion" not in report
    sent = len(server.requests)
    with pytest.raises(s1.FetchStoppedError):
        g, _ = fetcher_for(root, server)
        t.run_stage3(g, episodes, "f" * 64)
    assert len(server.requests) == sent  # nothing was sent after the violation
    assert (root / "stop.json").exists()


def test_the_fetchers_own_recovery_closes_a_stage3_root_after_a_reserve_crash(
    stage1: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the Stage 1 fix (#630) the reopened Fetcher itself raises from the receipt and writes
    the stop file, so after such a crash the root stays stopped and no statistics are published."""
    episodes: list[t.Episode] = stage1["episodes"]
    server = Hourly(episodes, draw_returns(episodes), pad={episodes[3].symbol: 100_000})
    root = tmp_path / "run"
    f, _ = fetcher_for(root, server)

    def die(_self: s1.Fetcher, _reason: str) -> None:
        raise RuntimeError("the process died before the stop file")

    with monkeypatch.context() as patch:
        patch.setattr(s1.Fetcher, "record_stop", die)
        with pytest.raises(RuntimeError, match="died"):
            t.run_stage3(f, episodes, "f" * 64)
    assert not (root / "stop.json").exists()
    sent = len(server.requests)
    with pytest.raises(s1.FetchStoppedError, match="larger than the read reserve"):
        fetcher_for(root, server)  # the fetcher's own recovery, not the Stage 3 check
    assert (root / "stop.json").exists()
    with pytest.raises(s1.FetchStoppedError, match="was stopped"):
        fetcher_for(root, server)
    report = t.evaluate(root)
    assert report["verdict"] == "stage3_stopped"
    assert "dispersion" not in report and "variants" not in report
    assert t.evaluate(root) == report  # the stored report is returned unchanged
    assert len(server.requests) == sent  # nothing was sent after the crash


# Wiring, documents and the registry


def test_the_only_http_client_is_built_in_the_fetch_command() -> None:
    source = (REPO / t.MODULE_PATH).read_text()
    assert source.count("httpx.Client(") == 1
    assert source.index("httpx.Client(") > source.index("def main(")
    assert "import requests" not in source and "urllib" not in source


def test_plan_and_check_commands_make_no_request_and_the_fetch_needs_the_exact_hash(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    assert t.main(["plan"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["go_sha256"]["1"] == t.build_contract()["go_sha256"]["1"]
    assert t.main(["write-contract", "--check"]) == 0
    with pytest.raises(ValueError, match="approved hash"):
        t.main(
            [
                "fetch",
                "--approved-sha256",
                "0" * 64,
                "--stage1-root",
                str(tmp_path),
                "--expected-cohort-sha256",
                "0" * 64,
                "--root",
                str(tmp_path / "r"),
            ]
        )
    assert not (tmp_path / "r").exists()


def test_document_registry_and_protocol_state_the_contract_numbers() -> None:
    c = t.build_contract()
    doc = (REPO / t.DOC).read_text()
    registry = (REPO / s1.REGISTRY).read_text()
    protocol = (REPO / t.PROTOCOL).read_text()
    for needle in (c["request_list_sha256"], c["go_sha256"]["1"], "228", "684", "48 MiB", "9,804"):
        assert needle in doc, needle
    assert c["request_list_sha256"] in registry and "I25 Stage 3" in registry
    assert "Proposed Amendment 7" in protocol and "new-perp-stage3-power-calibration-v1" in protocol
    for path in (
        t.DOC,
        t.CONTRACT_PATH,
        t.MODULE_PATH,
        "apps/analytics/tests/test_new_perp_stage3.py",
    ):
        text = (REPO / path).read_text()
        assert chr(0x2014) not in text, path
