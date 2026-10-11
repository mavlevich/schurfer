from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from schurfer_analytics.bybit_order_rules import OrderSizing
from schurfer_analytics.momentum_flow_hold12h_executable import (
    evaluate_executable_cohort,
    executable_view,
)
from schurfer_analytics.momentum_flow_hold12h_verdict import HOLD12H_VERDICT_CONTRACT
from schurfer_analytics.momentum_flow_hold12h_verdict_report import (
    FundingCoverage,
    HorizonOutcome,
    InstrumentRoute,
    ProbeClass,
    ProbeRecord,
    WatchDecision,
    evaluate_cohort,
)

BASE = datetime(2026, 10, 5, tzinfo=UTC)
ROUTE = InstrumentRoute("bybit", "linear", "FOOUSDT", "FOO/USDT:USDT")
CONTRACT = HOLD12H_VERDICT_CONTRACT


class _NoFunding:
    def coverage(
        self, route: InstrumentRoute, entry_at: datetime, exit_at: datetime
    ) -> FundingCoverage:
        return FundingCoverage(events=(), proven_full_coverage=True)


def _probe(
    watch_id: str, at: datetime, gross_720: float, gross_240: float, *, filled: bool = True
) -> ProbeRecord:
    return ProbeRecord(
        watch_id=watch_id,
        route=ROUTE,
        entry_at=at,
        entry_ok=filled,
        exit_at=at + timedelta(minutes=720) if filled else None,
        exit_resolved=filled,
        exit_reason="max_hold" if filled else None,
        actual_gross_return_pct=gross_720 if filled else None,
        actual_notional_usd=50.0 if filled else None,
        max_adverse_return_pct=-1.0 if filled else None,
        horizons={
            240: HorizonOutcome(
                240,
                resolved=filled,
                observed_at=at + timedelta(minutes=240) if filled else None,
                gross_return_pct=gross_240 if filled else None,
                notional_usd=50.0,
            ),
        },
    )


def _cohort(n: int = 40) -> tuple[list[WatchDecision], dict[str, ProbeRecord]]:
    watches, probes = [], {}
    for i in range(n):
        at = BASE + timedelta(hours=3 * i)
        wid = f"w{i:03d}"
        watches.append(WatchDecision(wid, f"asset{i % 12}", at))
        probes[wid] = _probe(wid, at, ((i * 7) % 11 - 4) * 0.5, ((i * 5) % 9 - 4) * 0.4,
                             filled=i % 10 != 3)  # fmt: skip
    return watches, probes


def _accept(notional: str = "50") -> OrderSizing:
    return OrderSizing(True, None, Decimal(1), Decimal(notional))


def test_every_order_accepted_at_the_position_equals_the_paper_evaluation() -> None:
    watches, probes = _cohort()
    sizings = {w.watch_id: _accept() for w in watches if probes[w.watch_id].entry_ok}
    executable, view = evaluate_executable_cohort(CONTRACT, watches, probes, _NoFunding(), sizings)
    paper = evaluate_cohort(CONTRACT, watches, probes, _NoFunding())
    assert executable == paper
    assert view.refused == {} and view.accepted == len(sizings)


def test_a_refused_order_leaves_the_pairs_and_counts_its_reason() -> None:
    watches, probes = _cohort()
    filled = [w.watch_id for w in watches if probes[w.watch_id].entry_ok]
    sizings = {wid: _accept() for wid in filled}
    sizings[filled[0]] = OrderSizing(False, "below_min_qty")
    del sizings[filled[1]]  # no sizing at all is a refusal, never an acceptance
    executable, view = evaluate_executable_cohort(CONTRACT, watches, probes, _NoFunding(), sizings)
    assert view.refused == {"below_min_qty": 1, "rules_missing": 1}
    assert len(view.watches) == len(watches) - 2
    assert {filled[0], filled[1]}.isdisjoint(p.watch_id for p in executable.pairs)
    assert sum(executable.funnel.values()) == len(watches) - 2
    paper = evaluate_cohort(CONTRACT, watches, probes, _NoFunding())
    assert executable.funnel[ProbeClass.ANALYZABLE] == paper.funnel[ProbeClass.ANALYZABLE] - 2


def test_an_unfilled_paper_entry_stays_in_the_funnel_without_a_sizing() -> None:
    watches, probes = _cohort()
    unfilled = [w.watch_id for w in watches if not probes[w.watch_id].entry_ok]
    sizings = {w.watch_id: _accept() for w in watches if probes[w.watch_id].entry_ok}
    view = executable_view(watches, probes, sizings, position_usd=CONTRACT.position_usd)
    assert set(unfilled) <= {w.watch_id for w in view.watches}
    assert view.refused == {}


def test_dollar_results_follow_the_rounded_notional() -> None:
    watches, probes = _cohort(4)
    sizings = {w.watch_id: _accept("25") for w in watches if probes[w.watch_id].entry_ok}
    half, _ = evaluate_executable_cohort(CONTRACT, watches, probes, _NoFunding(), sizings)
    full = evaluate_cohort(CONTRACT, watches, probes, _NoFunding())
    for policy in ("portfolio_720", "portfolio_240"):
        whole = getattr(full, policy).window_pnl_usd
        assert whole is not None
        assert getattr(half, policy).window_pnl_usd == pytest.approx(whole / 2)
    assert [p.watch_id for p in half.pairs] == [p.watch_id for p in full.pairs]


def test_a_refused_order_frees_its_slot() -> None:
    one_slot = dataclasses.replace(CONTRACT, max_concurrent_slots=1)
    watches = [WatchDecision("a", "asset0", BASE), WatchDecision("b", "asset1", BASE)]
    probes = {"a": _probe("a", BASE, 1.0, 0.5), "b": _probe("b", BASE, -2.0, -1.0)}
    both = {"a": _accept(), "b": _accept()}
    taken_a, _ = evaluate_executable_cohort(one_slot, watches, probes, _NoFunding(), both)
    assert (taken_a.portfolio_720.taken, taken_a.portfolio_720.skipped_slots_full) == (1, 1)
    assert taken_a.portfolio_720.window_pnl_usd == pytest.approx(0.5)
    refused_a = {"a": OrderSizing(False, "below_min_notional"), "b": _accept()}
    taken_b, view = evaluate_executable_cohort(one_slot, watches, probes, _NoFunding(), refused_a)
    assert view.refused == {"below_min_notional": 1}
    assert (taken_b.portfolio_720.taken, taken_b.portfolio_720.skipped_slots_full) == (1, 0)
    assert taken_b.portfolio_720.window_pnl_usd == pytest.approx(-1.0)


@pytest.mark.parametrize("amount", ["0", "-25", "NaN", "Infinity", "-Infinity", "50.01", "100"])
def test_an_accepted_notional_outside_the_position_stops_the_evaluation(amount: str) -> None:
    watches, probes = _cohort(1)
    sizings = {watches[0].watch_id: OrderSizing(True, None, Decimal(1), Decimal(amount))}
    with pytest.raises(ValueError, match="outside"):
        evaluate_executable_cohort(CONTRACT, watches, probes, _NoFunding(), sizings)


def test_a_size_order_for_a_larger_budget_is_refused_as_an_error() -> None:
    from schurfer_analytics.bybit_order_rules import parse_rule, size_order

    rule = parse_rule(
        {
            "symbol": "FOOUSDT",
            "status": "Trading",
            "lotSizeFilter": {
                "qtyStep": "1",
                "minOrderQty": "1",
                "maxMktOrderQty": "1000",
                "minNotionalValue": "5",
            },
        }
    )
    sizing = size_order(rule, price=Decimal(1), notional_usd=Decimal(100))
    assert sizing.feasible
    watches = [WatchDecision(f"w{i}", f"asset{i}", BASE) for i in range(6)]
    probes = {w.watch_id: _probe(w.watch_id, BASE, 1.0, 0.5) for w in watches}
    with pytest.raises(ValueError, match="outside"):
        evaluate_executable_cohort(
            CONTRACT, watches, probes, _NoFunding(), {w.watch_id: sizing for w in watches}
        )


def test_the_contract_position_itself_is_accepted() -> None:
    watches, probes = _cohort(1)
    sizings = {watches[0].watch_id: _accept(str(CONTRACT.position_usd))}
    _, view = evaluate_executable_cohort(CONTRACT, watches, probes, _NoFunding(), sizings)
    assert view.accepted == 1
