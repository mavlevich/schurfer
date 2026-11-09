from __future__ import annotations

import gzip
import hashlib
import json
import math
import os
from collections import Counter
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from schurfer_analytics import bybit_absorbed_buying as ab
from schurfer_analytics.bybit_absorbed_buying import (
    BLOCKS,
    EPISODE_H,
    FIRST_DECISION_MS,
    MONTHS,
    Member,
    State,
    balance,
    block_rho,
    block_variance,
    first_status,
    floor,
    flow,
    match,
    month_of,
    month_rho,
    pilot_gate,
    pool,
    raw_hourly,
    recount_day,
    scope_days,
    seed,
    states,
    tape_binding,
    weights_excess,
)
from schurfer_analytics.bybit_compressed_spring import (
    DAY_MS,
    HOUR_MS,
    MINUTE_MS,
    RANK_HOURS,
    WINDOW_END_MS,
    WINDOW_FIRST_MS,
    Features,
    Series,
)
from schurfer_analytics.bybit_compressed_spring import VERSION as TAPE_VERSION
from schurfer_analytics.bybit_compressed_spring_cond4 import Tape
from schurfer_analytics.bybit_compressed_spring_tape import Task, part_path, seal, write_part
from schurfer_analytics.bybit_squeeze_continuation import _day

if TYPE_CHECKING:
    from pathlib import Path

T = FIRST_DECISION_MS + 3 * DAY_MS


def series(wiggle: float = 0.004, turnover: float = 1e6, phase: int = 0) -> Series:
    """Hourly closes alternating +-wiggle; ``phase`` shifts the pattern."""
    s = Series(0, WINDOW_END_MS - 1)
    price = 100.0
    for k, start in enumerate(range(WINDOW_FIRST_MS, WINDOW_END_MS, HOUR_MS)):
        price *= 1 + wiggle if (k + phase) % 2 else 1 - wiggle
        s.bars[start] = (price, price, price, turnover)
    return s


def tape(buy: float = 1.0, sell: float = 1.0, first: int = WINDOW_FIRST_MS) -> Tape:
    tp = Tape()
    for h in range(first, WINDOW_END_MS, HOUR_MS):
        tp.kept[h] = (buy + (h // HOUR_MS) % 7 * 0.01, sell)
    tp.ok_days = set(range(first - first % DAY_MS, WINDOW_END_MS, DAY_MS))
    return tp


def state(symbol: str = "E", d: int = T, *, qb: float = 0.97, qv: float = 0.6, b: float = 0.3,
          v: float = 1e5, sigma1: float = 0.01, z1: float = 0.2,
          z4: float = 0.1) -> State:  # fmt: skip
    return State(symbol, d, b, v, qb, qv, sigma1, z1, z4, None)


def member(symbol: str = "E", tercile: int = 0, **kw: float) -> Member:
    return Member(state(symbol, **kw), tercile)  # type: ignore[arg-type]


def test_flow_reads_the_kept_minutes_of_the_hour_before_t() -> None:
    tp = Tape(kept={T - HOUR_MS: (3.0, 1.0)}, ok_days={T - DAY_MS, T})
    assert flow(tp, T) == (0.5, 4.0)
    assert flow(tp, T + HOUR_MS) == "no_flow"
    assert flow(None, T) == "tape_missing"
    tp.unusable.add(T - HOUR_MS)
    assert flow(tp, T) == "tape_mismatch"


def test_states_ignore_everything_at_and_after_t() -> None:
    s, tp = series(), tape()
    before = states("X", s, tp, T, T + HOUR_MS)[T]
    for start in range(T, T + 10 * DAY_MS, HOUR_MS):
        s.bars[start] = (1.0, 1.0, 1.0, 1.0)
        tp.kept[start] = (99.0, 0.0)
    after = states("X", s, tp, T, T + HOUR_MS)[T]
    assert before == after
    assert before.reason is None


def test_ranks_need_their_history() -> None:
    late = tape(first=T - 100 * HOUR_MS)
    st = states("X", series(), late, T, T + HOUR_MS)[T]
    assert st.reason == "rank_history_short"


def test_first_in_episode() -> None:
    by_hour = {T - k * HOUR_MS: state(d=T - k * HOUR_MS, qb=0.5) for k in range(EPISODE_H + 1)}
    assert first_status(by_hour, T) == "true"
    by_hour[T - EPISODE_H * HOUR_MS] = state(d=T - EPISODE_H * HOUR_MS)
    assert first_status(by_hour, T) == "false"
    by_hour[T - EPISODE_H * HOUR_MS] = State("E", T, None, None, None, None, None, None, None,
                                             "tape_missing")  # fmt: skip
    assert first_status(by_hour, T) == "tape_missing"


def test_qualifies_needs_all_four_items() -> None:
    assert state().qualifies == "true"
    assert state(qb=0.94).qualifies == "false"
    assert state(b=-0.1).qualifies == "false"
    assert state(qv=0.4).qualifies == "false"
    assert state(z1=1.2).qualifies == "false"
    assert state(qb=0.5).is_control_candidate
    assert not state(qb=0.8).is_control_candidate


def test_match_same_t_tercile_calipers_nearest_five() -> None:
    e = member("E")
    pool_ = [
        member("FAR", qb=0.5, z1=0.75),  # z1 outside the caliper
        member("VOL", qb=0.5, sigma1=0.013),  # ln ratio 0.26, outside
        member("TER", tercile=1, qb=0.5),  # another tercile
        member("LATE", qb=0.5, d=T + HOUR_MS),  # another hour
        member("E", qb=0.5),  # the same instrument
        *[member(f"N{k}", qb=0.5, z1=0.2 + 0.05 * (k + 1)) for k in range(6)],
    ]
    assert [c.symbol for c in match([e], pool_)[e.key]] == ["N0", "N1", "N2", "N3", "N4"]


def test_block_variance_counts_overlaps_by_instrument() -> None:
    rho = dict.fromkeys(MONTHS, 0.2)
    one = block_variance({("A", T): 1.0}, {("A", T): 0.04}, rho)
    assert one == pytest.approx(0.04**2)
    same = block_variance({("A", T): 1.0, ("A", T + HOUR_MS): 1.0},
                          {("A", T): 0.04, ("A", T + HOUR_MS): 0.04}, rho)  # fmt: skip
    assert same == pytest.approx(2 * 0.04**2 + 2 * 0.75 * 0.04**2)
    other = block_variance({("A", T): 1.0, ("B", T + HOUR_MS): 1.0},
                           {("A", T): 0.04, ("B", T + HOUR_MS): 0.04}, rho)  # fmt: skip
    assert other == pytest.approx(2 * 0.04**2 + 2 * 0.75 * 0.2 * 0.04**2)
    paired = block_variance({("A", T): 1.0, ("B", T): -1.0}, {("A", T): 0.04, ("B", T): 0.04}, rho)
    assert paired == pytest.approx(2 * 0.04**2 * (1 - 0.2))


def test_month_rho() -> None:
    month = MONTHS[2]
    together = Features({"BTCUSDT": series(), "A": series(), "B": series()}, ())
    assert month_rho(together, month, ["A", "B"], relative=False) == pytest.approx(1.0)
    apart = Features({"BTCUSDT": series(), "A": series(), "B": series(phase=1)}, ())
    assert month_rho(apart, month, ["A", "B"], relative=False) == pytest.approx(-1.0)


def test_pool_excludes_btc_and_ranks_by_turnover() -> None:
    month = MONTHS[1]
    instruments = {"BTCUSDT": series(turnover=1e12), "BIG": series(turnover=1e9),
                   "SMALL": series(turnover=1e3)}  # fmt: skip
    assert pool(Features(instruments, ()), month) == ["BIG", "SMALL"]


def test_scope_starts_fifteen_days_before_the_block() -> None:
    instruments = {"BTCUSDT": series(), "A": series()}
    days = scope_days(Features(instruments, ()), "blocks")["A"]
    first = BLOCKS[0][0] - 15 * DAY_MS
    assert days[0] == _day(first)
    assert _day(BLOCKS[1][1] - DAY_MS) == days[-1]
    assert month_of(BLOCKS[0][0]) == MONTHS[2]


def test_balance_uses_absolute_control_weights() -> None:
    e1, e2 = member("E1", z1=0.0), member("E2", z1=0.4)
    c1, c2 = member("C1", qb=0.5, z1=0.0), member("C2", qb=0.5, z1=0.2)
    weights = {e1.key: 0.5, e2.key: 0.5, c1.key: -0.25, c2.key: -0.75}
    members = {m.key: m for m in (e1, e2, c1, c2)}
    out = balance(weights, members)["z1"]
    event_var, control_var = 0.2**2, 0.25 * 0.15**2 + 0.75 * 0.05**2
    expected = (0.2 - 0.15) / math.sqrt((event_var + control_var) / 2)
    assert out["smd_unrounded"] == pytest.approx(expected)


def test_excess_weights_sum_to_zero() -> None:
    e = member("E")
    c1, c2 = member("C1", qb=0.5), member("C2", qb=0.5)
    w = weights_excess([e], {e.key: [c1, c2]})
    assert w == {e.key: 1.0, c1.key: -0.5, c2.key: -0.5}


def test_floor_order_data_balance_power() -> None:
    shape = {"clusters": 300, "symbols": 100, "iso_weeks": 40, "top5_symbol_clusters": 10}
    controls = {"events": 100, "events_with_control": 90}
    good = {"z1": {"smd_unrounded": 0.05}, "z4": {"smd_unrounded": 0.0},
            "ln_sigma1": {"smd_unrounded": 0.0}}  # fmt: skip
    powered = {"a_net": {"mde_unrounded": 0.004}, "b_excess": {"mde_unrounded": 0.0049}}
    assert floor(shape, controls, (95, 100), good, powered)["verdict"] == "pass_to_reading"
    weak = {**powered, "b_excess": {"mde_unrounded": 0.0051}}
    assert floor(shape, controls, (95, 100), good, weak)["verdict"] == "insufficient_power"
    tilted = {**good, "z4": {"smd_unrounded": -0.11}}
    assert floor(shape, controls, (95, 100), tilted, weak)["verdict"] == "insufficient_balance"
    few = {"events": 100, "events_with_control": 79}
    assert floor(shape, few, (95, 100), tilted, weak)["verdict"] == "insufficient_data"
    gate = pilot_gate(1600, {"events": 100, "events_with_control": 86}, (95, 100), good, powered)
    assert gate["verdict"] == "stop"  # 49 bps passes the floor but not the pilot's 45
    lean = {"a_net": {"mde_unrounded": 0.004}, "b_excess": {"mde_unrounded": 0.0044}}
    gate = pilot_gate(1600, {"events": 100, "events_with_control": 86}, (95, 100), good, lean)
    assert gate["verdict"] == "propose_full_fetch"


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (["scope", "--mode", "blocks", "--archive-sizes", "x", "--archive-sizes-sha256", "0"],
         "features"),
        (["check", "--manifest", "x", "--manifest-sha256", "0"], "features"),
    ],
)  # fmt: skip
def test_main_refuses_other_inputs(extra: list[str], message: str) -> None:
    common = ["--features", "x", "--features-sha256", "0" * 64, "--output-dir", "out",
              "--code-revision", "abc", "--no-working-tree-dirty"]  # fmt: skip
    with pytest.raises(SystemExit, match=message):
        ab.main([*extra, *common])


def test_main_pins_the_hyp032_tape_for_step_one() -> None:
    args = ["check", "--manifest", "x", "--manifest-sha256", "0" * 64,
            "--features", "x", "--features-sha256", ab.FEATURES_SHA256, "--output-dir", "out",
            "--code-revision", "abc", "--no-working-tree-dirty"]  # fmt: skip
    with pytest.raises(SystemExit, match="manifest"):
        ab.main(args)


def test_rank_window_is_336_hours() -> None:
    assert RANK_HOURS == 336


def test_build_runs_on_blocks_and_counts_every_planned_hour() -> None:
    instruments = {"BTCUSDT": series(), **{f"S{k}": series(phase=k) for k in range(4)}}
    features = Features(instruments, ())
    tapes = {f"S{k}": tape(buy=1.0 + k) for k in range(4)}
    report, frozen = ab.build(features, tapes, "blocks")
    assert report["funnel"]["member_hours"] == report["planned_member_hours"] == 4 * 28 * 24
    assert report["determinable_share"] == pytest.approx(1.0)
    assert report["gate"]["verdict"] == "stop"
    assert len(frozen["events"]) == report["events"]["impulses"]


def test_block_rho_is_known_at_the_earliest_covering_decision() -> None:
    feb = MONTHS[3]
    rho = dict.fromkeys(MONTHS, 0.2) | {feb: 0.9}
    late = feb - 2 * HOUR_MS  # 22:00 on the last day of January
    blocks = [block_rho(late + j * HOUR_MS, rho) for j in range(4)]
    assert blocks == [0.2, 0.2, 0.2, 0.2]  # 00:00 and 01:00 still use January's rho
    assert block_rho(feb + 2 * HOUR_MS, rho) == pytest.approx(0.2)  # covered from 23:00
    assert block_rho(feb + 3 * HOUR_MS, rho) == pytest.approx(0.9)
    assert block_rho(FIRST_DECISION_MS, rho) == pytest.approx(0.2)  # no decision before
    paired = block_variance({("A", late): 1.0, ("B", late): -1.0},
                            {("A", late): 0.04, ("B", late): 0.04}, rho)  # fmt: skip
    assert paired == pytest.approx(2 * 0.04**2 * (1 - 0.2))


HEADER = (
    "timestamp,symbol,side,size,price,tickDirection,trdMatchID,"
    "grossValue,homeNotional,foreignNotional"
)
DAY = _day(T)


def trade(ms: int, side: str, usd: float, symbol: str = "AUSDT") -> str:
    seconds, millis = divmod(ms, 1000)
    return f"{seconds}.{millis:03d}0,{symbol},{side},1,{usd},PlusTick,id,0,1,{usd}"


def raw_file(rows: list[str]) -> bytes:
    return gzip.compress(("\n".join([HEADER, *rows]) + "\n").encode(), mtime=0)


ROWS = [
    trade(T + 30 * MINUTE_MS, "Buy", 10.0),
    trade(T + 30 * MINUTE_MS + 5, "Sell", 4.0),
    trade(T + 57 * MINUTE_MS, "Buy", 100.0),  # masked, before the next hour
    trade(T + HOUR_MS + 2 * MINUTE_MS, "Sell", 100.0),  # masked, after it
    trade(T + 31 * MINUTE_MS, "Buy", 100.0, symbol="BUSDT"),
    trade(T - 1, "Buy", 100.0),  # the day before
]


def test_raw_hourly_sums_the_kept_minutes_by_taker_side(tmp_path: Path) -> None:
    path = tmp_path / "f.csv.gz"
    path.write_bytes(raw_file(ROWS))
    assert raw_hourly(path, "AUSDT", DAY) == {T: (10.0, 4.0)}


def test_recount_day_finds_a_swapped_side() -> None:
    features = Features({"AUSDT": series()}, ())
    good = Tape(kept={T: (10.0, 4.0)}, ok_days={T})
    out = recount_day(features, good, "AUSDT", DAY, {T: (10.0, 4.0)})
    assert out["flow_hours_differing"] == [] and out["rank_hours_differing"] == []
    swapped = Tape(kept={T: (4.0, 10.0)}, ok_days={T})
    out = recount_day(features, swapped, "AUSDT", DAY, {T: (10.0, 4.0)})
    assert len(out["flow_hours_differing"]) == 1


def test_plain_ranks_agree_with_the_rolling_ranks() -> None:
    tp = tape()
    raw = {h: tp.kept[h] for h in range(T, T + DAY_MS, HOUR_MS)}
    out = recount_day(Features({"AUSDT": series()}, ()), tp, "AUSDT", DAY, raw)
    assert out["rank_hours_defined"] == 24 and out["rank_hours_differing"] == []


def test_check_raw_fetches_the_sample_and_checks_the_file(tmp_path: Path) -> None:
    body = raw_file(ROWS)
    tapes = {"AUSDT": Tape(kept={T: (10.0, 4.0)}, ok_days={T})}
    parts = tmp_path / "parts"
    part = parts / "AUSDT" / f"{DAY}.json.gz"
    part.parent.mkdir(parents=True)

    def respond(_request: httpx.Request) -> httpx.Response:
        headers = {"content-length": str(len(body))}
        return httpx.Response(200, headers=headers, stream=httpx.ByteStream(body))

    features = Features({"AUSDT": series()}, ())
    for digest, verdict in ((hashlib.sha256(body).hexdigest(), "agrees"), ("0" * 64, "differs")):
        part.write_bytes(gzip.compress(json.dumps({"file_sha256": digest}).encode()))
        out = tmp_path / verdict
        report = ab.build_check_raw(
            features, tapes, parts, out, 10**6, httpx.MockTransport(respond)
        )
        assert report["verdict"] == verdict and report["bytes_read"] == len(body)
        assert list((out / "tmp").iterdir()) == []


# ---------------------------------------------------------------- seeding


def _json(path: Path, body: dict[str, Any]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(json.dumps(body).encode())
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source(tmp_path: Path, binding: dict[str, Any]) -> tuple[Path, str]:
    run = tmp_path / "pilot"
    entries = []
    for symbol, day, status in (("A", "2025-01-01", "ok"), ("A", "2025-01-02", "missing"),
                                ("B", "2025-01-01", "ok"), ("C", "2025-01-01", "ok")):  # fmt: skip
        path = part_path(run, symbol, day)
        write_part(path, {"version": TAPE_VERSION, "symbol": symbol, "day": day,
                          "status": status, "file_bytes": 5})  # fmt: skip
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        entries.append({"symbol": symbol, "day": day, "status": status,
                        "part_sha256": "0" * 64 if symbol == "B" else digest})  # fmt: skip
    manifest = run / "manifest.json"
    return manifest, _json(manifest, {"version": TAPE_VERSION, "binding": binding,
                                      "parts": entries})  # fmt: skip


SOURCE_SCOPE: dict[str, Any] = {"version": TAPE_VERSION, "study": ab.VERSION, "mode": "blocks",
                "primary": {"A": ["2025-01-01", "2025-01-02"], "B": ["2025-01-01"],
                            "C": ["2025-01-01"]}}  # fmt: skip
TARGET_SCOPE: dict[str, Any] = {"version": TAPE_VERSION, "study": ab.VERSION, "mode": "full",
                "primary": {"A": ["2025-01-01", "2025-01-02", "2025-01-03"],
                            "B": ["2025-01-01"]}}  # fmt: skip


def test_seed_imports_only_verified_ok_parts_and_seal_keeps_their_origin(tmp_path: Path) -> None:
    manifest, digest = _source(tmp_path, tape_binding("pilot-scope"))
    out = tmp_path / "full"
    done = seed(manifest, digest, SOURCE_SCOPE, "pilot-scope", TARGET_SCOPE, "full-scope", out)
    assert done["imported_parts"] == 1 and done["imported_file_bytes"] == 5
    assert done["skipped"] == {"not_ok": 1, "outside_scope": 1, "sha256_differs": 1}
    assert json.loads((out / "run.json").read_bytes()) == tape_binding("full-scope")
    seeded = part_path(out, "A", "2025-01-01")
    assert seeded.read_bytes() == part_path(manifest.parent, "A", "2025-01-01").read_bytes()
    tasks = [Task(s, d, part_path(out, s, d), {}, ()) for s, days in
             TARGET_SCOPE["primary"].items() for d in days]  # fmt: skip
    for task in tasks[1:]:  # what the fetch would add
        write_part(task.part, {"version": TAPE_VERSION, "symbol": task.symbol, "day": task.day,
                               "status": "missing"})  # fmt: skip
    sealed, _ = seal(tasks, out, tape_binding("full-scope"))
    assert [p["origin"] for p in sealed["parts"]] == ["seeded", "fetched", "fetched", "fetched"]
    assert sealed["seed"]["imported_parts"] == 1 and sealed["bytes_reserved"] == 0
    write_part(seeded, {"version": TAPE_VERSION, "symbol": "A", "day": "2025-01-01",
                        "status": "ok", "file_bytes": 6})  # fmt: skip
    (out / "manifest.json").unlink()
    with pytest.raises(SystemExit, match=r"not the part seed\.json imported"):
        seal(tasks, out, tape_binding("full-scope"))


@pytest.mark.parametrize(
    ("binding", "scopes", "message"),
    [
        (tape_binding("p") | {"features_sha256": "other"}, None, "other inputs, rules or scope"),
        (tape_binding("p") | {"rules": "tape-v2"}, None, "other inputs, rules or scope"),
        (tape_binding("p") | {"hyp031_index_sha256": "x"}, None, "other inputs, rules or scope"),
        (tape_binding("p") | {"pilot": 30}, None, "other inputs, rules or scope"),
        (tape_binding("other"), None, "other inputs, rules or scope"),
        (tape_binding("p"), (TARGET_SCOPE, TARGET_SCOPE), "pilot blocks"),
        (tape_binding("p"), (SOURCE_SCOPE, SOURCE_SCOPE), "full scope"),
        (tape_binding("p"), (SOURCE_SCOPE, TARGET_SCOPE | {"version": "x"}), "full scope"),
    ],
)  # fmt: skip
def test_seed_refuses_incompatible_inputs_before_writing(
    tmp_path: Path, binding: dict[str, Any], scopes: Any, message: str
) -> None:
    manifest, digest = _source(tmp_path, binding)
    source, target = scopes or (SOURCE_SCOPE, TARGET_SCOPE)
    out = tmp_path / "full"
    with pytest.raises(SystemExit, match=message):
        seed(manifest, digest, source, "p", target, "full-scope", out)
    assert not out.exists()
    assert list(tmp_path.iterdir()) == [tmp_path / "pilot"]


def test_seed_refuses_a_used_directory(tmp_path: Path) -> None:
    manifest, digest = _source(tmp_path, tape_binding("p"))
    out = tmp_path / "full"
    out.mkdir()
    (out / "run.json").write_text("{}")
    with pytest.raises(SystemExit, match="not empty"):
        seed(manifest, digest, SOURCE_SCOPE, "p", TARGET_SCOPE, "full-scope", out)
    with pytest.raises(SystemExit, match="does not match"):
        seed(manifest, "0" * 64, SOURCE_SCOPE, "p", TARGET_SCOPE, "full-scope", tmp_path / "new")


def test_an_interrupted_seed_leaves_nothing_and_can_be_run_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, digest = _source(tmp_path, tape_binding("p"))
    out = tmp_path / "full"

    def crash(fd: int) -> None:
        raise OSError("disk full")

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", crash)
        with pytest.raises(OSError, match="disk full"):
            seed(manifest, digest, SOURCE_SCOPE, "p", TARGET_SCOPE, "full-scope", out)
    assert sorted(tmp_path.iterdir()) == [tmp_path / "pilot"]
    done = seed(manifest, digest, SOURCE_SCOPE, "p", TARGET_SCOPE, "full-scope", out)
    assert done["imported_parts"] == 1
    assert sorted(p.name for p in out.iterdir()) == ["parts", "run.json", "seed.json"]


# ---------------------------------------------------------------- counts input binding


def _tape_run(tmp_path: Path, features: Features, binding: dict[str, Any] | None = None,
              drop: int = 0) -> tuple[Path, str, dict[str, Any], str]:  # fmt: skip
    primary = scope_days(features, "blocks")
    scope: dict[str, Any] = {"version": TAPE_VERSION, "study": ab.VERSION, "mode": "blocks",
                             "primary": primary}  # fmt: skip
    scope_sha = _json(tmp_path / "scope.json", scope)
    parts = [{"symbol": s, "day": d, "status": "missing", "part_sha256": "0" * 64}
             for s, days in primary.items() for d in days]  # fmt: skip
    body = {"version": TAPE_VERSION, "binding": binding or tape_binding(scope_sha),
            "parts": parts[drop:]}  # fmt: skip
    manifest = tmp_path / "run" / "manifest.json"
    return manifest, _json(manifest, body), scope, scope_sha


BOUND_FEATURES = Features({"BTCUSDT": series(), "S0": series(), "S1": series(phase=1)}, ())


def test_counts_accepts_the_tape_sealed_for_its_scope(tmp_path: Path) -> None:
    manifest, digest, scope, scope_sha = _tape_run(tmp_path, BOUND_FEATURES)
    ab.bound_tape(manifest, digest, scope, scope_sha, BOUND_FEATURES, "blocks")


@pytest.mark.parametrize(
    ("binding", "drop", "edit", "mode", "message"),
    [
        (tape_binding("x") | {"pilot": 30, "rules": "tape-v2"}, 0, {}, "blocks", "bound to other"),
        (tape_binding("x"), 0, {}, "blocks", "bound to other"),
        (None, 1, {}, "blocks", "not exactly the scope"),
        (None, 0, {"mode": "full"}, "blocks", "blocks scope"),
        (None, 0, {}, "full", "full scope"),
        (None, 0, {"primary": {"S0": ["2025-01-01"]}}, "blocks", "days the protocol fixes"),
    ],
)  # fmt: skip
def test_counts_refuses_a_tape_from_another_run(
    tmp_path: Path, binding: dict[str, Any] | None, drop: int, edit: dict[str, Any], mode: str,
    message: str,
) -> None:  # fmt: skip
    manifest, digest, scope, scope_sha = _tape_run(tmp_path, BOUND_FEATURES, binding, drop)
    with pytest.raises(SystemExit, match=message):
        ab.bound_tape(manifest, digest, scope | edit, scope_sha, BOUND_FEATURES, mode)


def test_counts_needs_the_scope_on_the_command_line() -> None:
    args = ["counts", "--mode", "full", "--manifest", "x", "--manifest-sha256", "0" * 64,
            "--features", "x", "--features-sha256", ab.FEATURES_SHA256, "--output-dir", "out",
            "--code-revision", "abc", "--no-working-tree-dirty"]  # fmt: skip
    with pytest.raises(SystemExit):
        ab.main(args)


def test_an_event_hour_with_an_unknown_episode_is_not_determined() -> None:
    funnel = Counter(qualifies_true=20, qualifies_false=80, first_true=5, first_false=3,
                     first_tape_missing=12)  # fmt: skip
    assert ab.determined_hours(funnel) == 88
