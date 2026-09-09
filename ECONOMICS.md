# Economics and candidate feasibility

Status: planning worksheet, registered 2026-09-07. Initial experiment capital
and acceptable total loss recorded from the owner on 2026-09-09; other inputs
remain pending.
This is not a profitability claim, a new hypothesis registration or authorization
to trade. [ROADMAP.md](ROADMAP.md) owns delivery order; existing research contracts
and the [discovery ledger](docs/research/discovery-ledger.md) own frozen evidence.

## Owner inputs

| Input                                          | Current value                                                                                                  | Decision it enables                                   |
| ---------------------------------------------- | -------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------- |
| Available trading capital                      | USD 50 for the initial experiment; scaling capital unspecified                                                 | Margin, liquidity reserve and feasible position sizes |
| Maximum acceptable capital loss / drawdown     | Owner accepts losing the entire USD 50 experiment budget; operating drawdown/stop thresholds not yet specified | Risk ceiling and portfolio stop policy                |
| Desired net monthly income and time horizon    | Owner prefers return as a percentage of invested capital; numeric target and horizon unspecified               | Whether an executable edge is economically meaningful |
| Monthly infrastructure, storage and data cost  | Not inventoried                                                                                                | Cash break-even                                       |
| Research budget in money and engineering hours | Not specified                                                                                                  | Stop/review boundary for the next cycle               |
| Owner time cost / required return on effort    | Not specified                                                                                                  | Economic result including ongoing maintenance         |

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
