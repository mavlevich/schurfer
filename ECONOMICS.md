# Economics and candidate feasibility

Status: planning worksheet, registered 2026-09-07. Owner capital, loss tolerance,
leverage and the income stance recorded 2026-09-09 and revised 2026-09-11; a
materiality/promotion gate and a portfolio register are now filled. Infrastructure
cost, research budget and owner time cost remain pending (owner-absorbed infra is
excluded from the trading break-even by decision).
This is not a profitability claim, a new hypothesis registration or authorization
to trade. [ROADMAP.md](ROADMAP.md) owns delivery order; existing research contracts
and the [discovery ledger](docs/research/discovery-ledger.md) own frozen evidence.

## Owner inputs

| Input                                          | Current value                                                                                                                                                                                         | Decision it enables                                   |
| ---------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------- |
| Available trading capital                      | USD 300 initial test (revised up from 50 on 2026-09-11), leverage up to 5x (~USD 1,500 max exposure); willing to add capital repeatedly while consistently net-positive                               | Margin, liquidity reserve and feasible position sizes |
| Maximum acceptable capital loss / drawdown     | Owner accepts losing the entire test budget; operating drawdown/stop thresholds not yet specified                                                                                                     | Risk ceiling and portfolio stop policy                |
| Desired net monthly income and time horizon    | "As much as possible" -- no fixed monthly target; the gate is a consistent net-positive edge, then scale capital. Income is bounded by opportunity rate x executable capacity, not by a target number | Whether an executable edge is economically meaningful |
| Monthly infrastructure, storage and data cost  | Not inventoried                                                                                                                                                                                       | Cash break-even                                       |
| Research budget in money and engineering hours | Not specified                                                                                                                                                                                         | Stop/review boundary for the next cycle               |
| Owner time cost / required return on effort    | Not specified                                                                                                                                                                                         | Economic result including ongoing maintenance         |

Missing inputs do not block safety fixes, preservation of existing evidence or
technical feasibility checks. They do prevent a defensible decision that the
project meets the owner's income/risk needs. Record the owner decision and date
when these fields are filled; do not replace unknown values with zero.

### Owner decision — 2026-09-09

The owner allocated USD 50 to an initial trading experiment and explicitly accepts
the possible loss of that entire amount. This is total experiment capital, not
USD 50 per trade, a chosen position notional, or a recurring replenishment budget.
Infrastructure and engineering costs remain separately unquantified. Additional
capital, leverage, position sizing and operating stop thresholds are not set by
this decision. The budget statement does not enable live mode or authorize orders.

The owner prefers evaluating earnings relative to invested capital. Report net
trading PnL in USD and as a percentage of experiment equity over an explicit
period, alongside drawdown, sample sufficiency and capital flows. For a fixed
USD 50 starting balance with no deposits or withdrawals, equity return is net
trading PnL divided by USD 50; it is not a trade's return on notional or margin.
Show infrastructure costs and the resulting project cash result separately.
No numeric return target or compounding forecast has been agreed.

A small funded probe can check execution and accounting once candidate evidence
and execution safeguards permit it. Its budget alone establishes neither sample
sufficiency nor profitability or capacity at larger sizes. Feasibility at USD 50
still depends on the chosen instruments' minimum orders, fees and margin needs.

Clarifications recorded from the owner on 2026-09-09: the USD 50 is a nominal
real-money mechanics-test floor, not a cap on ambition. Leverage may extend
position size within it (subject to a later, separately set stop/margin policy),
and the owner is willing to add capital beyond the nominal amount once an edge is
demonstrated; no specific scaling figure, target percentage or horizon is
committed by this statement. Infrastructure and hosting are absorbed personally
by the owner and are deliberately excluded from the trading break-even: report
trading PnL and equity return on their own, and keep owner-borne infrastructure
cost as a separate line rather than netting it into the strategy result. None of
this authorizes live mode or any order; income still depends on a demonstrated
edge and a committed scaling amount, neither of which exists yet.

### Owner decision -- 2026-09-11

The owner revised the initial test capital to USD 300 (from 50) so operations are
possible across several venues, with leverage up to 5x (so up to ~USD 1,500 of
gross exposure), and restated willingness to add capital repeatedly as long as
the account is consistently net-positive. The income goal is "as much as
possible" -- there is no fixed monthly target. This does not authorize live mode
or any order; it sets the sizing envelope and the promotion gate below.

## Materiality and the promotion gate

With no fixed income number, "maximise income" is not a target to hit but a
constraint to respect: monthly income is, at best,

`income ~= net_edge_per_trade x trades_per_month x executable_notional`

so it is bounded by three things, not one. A positive edge alone earns nothing if
the signal fires rarely or only on instruments too thin to hold size. The
promotion gate is therefore, per candidate:

1. **Consistent net-positive edge**: the frozen strategy's after-cost lower bound
   is above zero and robust (bootstrap LB > 0, survives leave-one-out); a
   single-window positive is not "consistent".
2. **Opportunity rate**: enough trades per month that scaling capital produces a
   non-trivial income (a handful of trades a month cannot).
3. **Executable capacity**: the fired instruments are liquid enough to absorb the
   scaled notional (USD 300 -> USD 1,500 with 5x, and beyond as capital is added)
   without the slippage that was never measured -- which needs the L2/book-depth
   shadow, since `capacity_unknown` is the current honest state.

Only a candidate clearing all three is worth adding capital to. Reaching them in
order also fails fast: a mature-negative edge stops at (1); a real-but-rare or
illiquid edge stops at (2)/(3) even if (1) holds. Slippage stays unmodelled until
the L2 shadow exists, so no result is "net proven" before then.

## Portfolio register (concluded discovery lines vs the gate)

None of the lines concluded so far clears the gate; the binding failure is noted
so we do not re-spend effort by assertion.

| Line                    | Result (2026-09)     | Gate failure                                                  |
| ----------------------- | -------------------- | ------------------------------------------------------------- |
| `early_momentum_v4`     | fail                 | (1) no gross edge before costs; net negative                  |
| HYP-024 order-flow      | inconclusive         | underpowered (89 episodes); no edge demonstrated              |
| extreme-mover endpoint  | stop                 | (1) negative after-cost EV at the floor                       |
| net-buy accumulation v1 | too_rare_or_illiquid | (2)/(3): ~2 fires, one illiquid cluster, at frozen thresholds |

Common thread: detection is not the problem; the recurring wall is (1) an edge
that clears ~22 bps round-trip costs, (2) a workable opportunity rate, and (3)
executable capacity on liquid names. Until a candidate clears all three, added
capital has nothing to scale. This is why L2/book-depth capture (which turns
`capacity_unknown` into a measured (3)) is the highest-value data upgrade once any
line shows a positive, non-rare (1)/(2).

## Required candidate card

Create a card beside the candidate's existing research contract, or extend its
existing document rather than duplicating identity. For a new candidate, complete
this before expensive implementation. Existing frozen cohorts keep collecting
under their contracts; adding an economics card does not change those contracts.

- **Identity / status:** family, version, existing HYP/contract link; discovery,
  confirmation, parked or failed; exact venue and instrument route.
- **Mechanism:** what creates the proposed advantage, information available at
  decision time, and the cheapest test that can reject the mechanism.
- **Opportunity rate:** independent episodes/assets/weeks, unresolved and control
  matching losses, expected calendar time to the registered evidence floor.
- **Execution:** entry/exit side, latency, fill probability, fees, signed funding
  evidence versus modeled costs, slippage, partial fills and adverse selection.
- **Capacity:** three explicitly chosen test notionals, executable depth/impact,
  overlap, capital occupancy, margin requirements and correlated exposure.
- **Economics:** plausible net result range, uncertainty, monthly cash costs and
  owner effort; distinguish measured values from scenarios.
- **Next decision:** one artifact, implementer, effort budget, required inputs,
  untouched evaluation window and continue/park/stop rule fixed before reading it.

No new Confirmation line without its own preregistration and a free research slot.
Missing or unusable historical data may stop a pilot before any return calculation.
Archival OHLCV is not proof of historical book depth, queue position, publication
latency or liquidation-stream completeness.

## Units and calculation

For a descriptive scenario:

`monthly cash result ≈ executed trades × mean filled notional × net return per execution − fixed cash costs`

If EV already includes unfilled signals as cash, use that denominator and do not
multiply by fill probability again. If size varies, sum each size-dependent result
instead of multiplying independent averages. Explicitly separate notional, margin
and equity: `size_usd` in the present order path is already notional, and leverage
must not be multiplied into it a second time. Higher size can change both fill
probability and returns; a $50 probe is not a capacity estimate.

Report accounting costs, unresolved exposure, drawdown and capital occupancy beside
the result. A high win rate, gross return, maker touch or positive hindsight/MFE
upper bound is not evidence of executable profitability. Do not rewrite a mature
negative frozen verdict after selecting favorable cost/exit assumptions.

## Review record

At each formal result or budget review, record: date, candidate/version, evidence
artifact, cost/hours spent, remaining budget, decision and its reason. Keep earlier
decisions visible. Quarterly is a reasonable review cadence for owner economics;
research reads still follow their own stopping rules. This document schedules no
automations and authorizes no production or account changes.
