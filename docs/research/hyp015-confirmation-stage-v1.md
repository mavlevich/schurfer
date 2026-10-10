# HYP-015 after `candidate`: the confirmation stage (design draft v1)

Status: **design approved 2026-10-10, not registered.** No cohort is registered and no
new collection starts here. The freeze comes after HYP-012 v2 is terminal, with a future
boundary and the conditions below met. Four weeks are accepted as a bounded budget; the
USD 10 economic floor is not approved. Nothing here reads a return, and nothing changes
the registered HYP-015 cohort, its thresholds or its 2026-11-04 read.

## Purpose

The [registration](momentum-flow-hold12h-verdict-v1.md) lets a `candidate` authorize
only the next gate (the episode study or a real shadow), never live trading. That gate
needs a sample the 11-04 read has not seen. This design prepares it in advance, so a
`candidate` does not then wait weeks for a design. It does not assume a positive result:
the cohort is read only if the first read returned `candidate`. Its start is set by the
conditions below, not by a calendar target.

## What a `candidate` on 2026-11-04 does and does not show

It shows that on one four-week cohort the 720-minute hold beat the 240-minute baseline
on a fixed USD 300 bank and was positive on its own, with a cluster-bootstrap interval
above zero, after fees and actual funding. It does not show that this holds on a new
sample: one cohort is one draw.

## When the confirmation cohort may start

The start is a literal Monday 00:00 UTC, frozen by the freeze PR, which merges before
it. It is the first Monday that meets all three conditions:

1. **v2's blind window is respected.** No data on or after 2026-09-29 may be read for a
   new study before HYP-012 v2 reaches a terminal state (its formal read or its
   registered administrative stop;
   [stop rule](source-lead-forward-cohort-v2-administrative-stop.md)). The first HYP-015
   registration (2026-09-27) predates that rule and gives this new cohort no exception.
   So the confirmation is **registered only after v2 is terminal**, and the freeze PR
   names v2's terminal artifact by path and sha256. If v2 ended by an administrative
   stop, the stop artifact's `closed_windows` are excluded mechanically: a confirmation
   decision is dropped from the denominator, before any return is read, when its feature
   window (the WATCH 60-minute lookback) or its outcome window (entry to 720 minutes)
   intersects a closed window on the same canonical asset (by base ticker where no
   canonical asset maps), and cross-sectional inputs are checked the same way. Dropped
   decisions are counted and reported. Closed windows end before v2's terminal state and
   the cohort starts after it, so the set should be empty by construction; it is checked
   mechanically anyway. The 2026-10-31 checkpoint does not necessarily stop v2; if v2
   is still running, the start moves to the first Monday after it ends.
2. **Episodes do not overlap the first cohort.** The first cohort's decisions end
   2026-11-02 00:00 UTC, and its last positions hold until about 12:00 UTC that day.
   The confirmation's first feature window opens 60 minutes before its start. So the
   start is at least the first cohort's last possible exit (2026-11-02 12:00 UTC) plus
   the 60-minute lookback; the earliest Monday that satisfies this is **2026-11-09**.
   This separates full feature and outcome windows, not only decision dates.
3. **The registration is complete:** this design reviewed, the power scenarios below
   published, the freeze PR merged.

The cohort then runs four full UTC ISO weeks, Monday to Monday, and the read opens 60 h
after its end (the same delay as the first cohort). Example: a 2026-11-09 start ends on
2026-12-07 and opens its read on 2026-12-09 12:00 UTC.

## The cohort

- **The same strategy, unchanged:** `momentum_flow_watch_v1`, the hold12h paper contract
  (sha `f280bd14...`), the same reader, the same integrity checks and gates A to E with
  the same frozen thresholds, and the same capital rule (USD 300 bank, USD 50 slots,
  6 slots). The first cohort's data sufficiency does not carry over. Nothing is re-tuned
  from the 11-04 result.
- **Its own one-read claim:** a new contract version (`hold12h_confirm_v1`), keyed
  apart from the first cohort in the existing claim table. Snapshot, resume, publication
  and republication work as in #509.
- **Conditional read, verified, before the confirmation's own claim.** The reader:
  - loads the first cohort's claim row (`hold12h_verdict_v1`, the frozen bounds
    2026-10-05..11-02) and requires `status = 'completed'`;
  - reads the artifact file that row names and requires its sha256 to equal the row's
    `artifact_sha256` and its `.sha256` sidecar;
  - parses that artifact and requires its `contract_sha256` to be the first contract's,
    its bounds the frozen ones, and `verdict = "candidate"`.

  Anything else refuses. The claim row has no verdict field, so a JSON file with that
  verdict outside this chain proves nothing. If the first read is not `candidate`, the
  confirmation cohort is recorded as closed unread.

- **No extension.** If the floor is not met at the prefix (`insufficient_data` or
  `insufficient_evidence`), the line stops before live. Any later attempt needs a new
  registration with a stated reason, never a longer window of this one.

## Power: a bounded budget, not an assumption

Four weeks is the budget, not a claim of sufficiency. Before registration a power note
(the `research_cost_power_planning` calculator of #483, run offline) reports the
minimum detectable mean net effect per trade for a four-week cohort. Inputs:

- event counts at the observed WATCH rate;
- per-trade standard deviation on a declared grid, not estimated from any HYP-015 return;
- dependence between events: clustering by canonical asset, as in the registered
  cluster bootstrap, with a design-effect grid.

If that effect lies above the economic threshold below, the registration says so
plainly, or it is not made.

The first version is [hyp015-confirmation-power-v1.md](hyp015-confirmation-power-v1.md).
The #483 dispersions come from 30 to 60 minute holds, so it uses the same formula on a
declared grid instead. In its scenarios (not a measured HYP-015 spread), four weeks
detect a mean of about 45 to 135 bps per trade, against an economic floor of 6 to
22 bps: only a large edge can be confirmed.

**Approach chosen (2026-10-10, preliminary):** a bounded check. `insufficient_evidence`
admits nothing to live; it means the confirmation was not obtained, not that the edge
is absent. The market-adjusted return is reported as a diagnostic only: it measures a
different edge, and a real hedge would be a separate strategy with its own costs and
capital.

## Four questions, with a rule each

1. **Is the edge there on the untouched sample after all costs?** The confirmation
   verdict is `candidate`, with every gate on the executable inputs of question 2.
2. **Can the trades be executed at this size?** Paper fills already use executable book
   VWAP, including spread and impact, and refuse stale quotes. What paper cannot see is
   the venue's order rules, so a separately registered **executable portfolio** is
   computed beside the paper one:
   - Bybit's minimum order value and quantity step are captured per decision
     (delivery 3);
   - the order quantity is USD 50 at the entry price, rounded down to the step; an order
     below the minimum value is refused before entry;
   - **missing metadata refuses the entry.** It never counts as a passed check;
   - refused entries free their slot, and slot occupancy and selection are recomputed
     with the same selection rule, so a refused probe cannot carry the result;
   - the PnL uses the rounded notional.

   Two samples, kept apart:
   - **the executable pairs:** every analyzable probe whose order passes the rules,
     regardless of free slots. Gate D (the standalone mean) and the paired difference of
     gate E are computed on them;
   - **the executable portfolio:** the same probes through the slot rule. Gate E's
     portfolio checks and the economics of question 3 are computed on it.

   The paper versions of both and the refused share are reported beside them. The paper entry quote latency p90 must also stay
   under the paper contract's own `max_quote_latency_seconds`.

3. **How much capital is tied up, and how much is left per month?** From the executable
   portfolio: slot occupancy, the window trading PnL in dollars **and** as a share of the
   USD 300 bank, and the same per month (x 30.44 / 28). The trading result is reported
   separately from operating costs (hosting, owner-absorbed). Rescaling one four-week
   window to a month is a description of that window, not an income forecast.
   - **Proposed floor, for the owner to decide:** USD 10 a month at USD 300, about 3.3%
     of the bank. It is not an agreed threshold until the owner sets it in the freeze PR.
4. **What does a convincing answer cost?** Calendar: from the start condition above
   (at the earliest 2026-11-09) four weeks plus the 60 h delay. Engineering: the freeze
   PR, the instrument-rule capture and the executable portfolio, all small. No new market
   collection; disk growth is covered by the PR 2b budget.

All four pass: LIVE_PROBE implementation starts (design #508, four PRs). Its first
mainnet order is the smallest size Bybit accepts, under its own owner authorization.

## When the line closes

- 11-04 is not `candidate`: closed for live; the confirmation cohort is never read.
- The confirmation is `reject_hold12h` (negative mean) or `no_duration_improvement`:
  closed.
- `insufficient_data` or `insufficient_evidence`: stopped before live, no extension.
- The latency check fails: closed for live at this design.
- Below the owner's economic floor: parked as not worth it at this scale, with the
  numbers.

## Deliveries

1. This design (design review).
2. **Instrument-rule capture** (small, outcome-blind): Bybit's minimum order value and
   quantity step per decision, stored next to the probe. It should run before the
   confirmation starts, so no probe of the cohort lacks it.
3. **Power note** (offline, no HYP-015 return read).
4. **Freeze PR**, after v2 is terminal and before the chosen Monday:
   - the `hold12h_confirm_v1` contract: the frozen hold12h constants with the new
     literal bounds and version, v2's terminal artifact by path and sha256, and the
     owner's economic floor;
   - the executable portfolio, registered;
   - the reader parameterized by contract (today it hard-codes the first one), the
     verified conditional-read chain, and the `closed_windows` exclusion;
   - a rehearsal case for each refusal, and make targets mirroring #509.

## Review answers (2026-10-10)

1. All gates are repeated, integrity checks and A to E included.
2. The floor is stated in dollars and as a share of the bank, trading result apart from
   operating costs, and stays an owner proposal until the freeze PR.
3. At least four UTC ISO weeks under the current rules; whether four suffice is for the
   power note to show.
