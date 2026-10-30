# Research strategy v2: how the program looks for money (draft 4)

Status: **draft 4, 2026-10-30.** It decides nothing, reads no cohort, opens no line
and changes no registered rule. Drafts 1 (5fd5945), 2 (0ba3115) and 3 (01975a6) got
REQUEST CHANGES. Draft 2 took the reviewer's order and fixes and added
the long-term plan the owner asked for. Draft 3 answers the second review:

- the order book fill model;
- one frozen holdout batch;
- live reproduces the tested contract;
- a scaling rule;
- the compressed-spring definitions.

Draft 4 answers the third review:

- a confidence-bound scaling rule;
- the maker-read conclusion limited to its contract;
- volume-limited partial fills in the book model. It sits beside the
  [program review](program-review-2026-10-31.md) and differs from it in one point: the
  owner continues the program, so its "on no success, move to maintenance" outcome is
  replaced by the cost discipline below. The owner decides on 2026-10-31 or later.

## Why change the approach

The record has one repeated shape: **a gross effect, no net effect.**

- Executable pump-short, HYP-029 (gross +1.26%, net interval spans zero), the
  source-lead family (HYP-012b, HYP-012c), HYP-030 (typical trade near zero) and
  abnormal-flow v1 all lost their apparent edge once costs and realistic fills were
  priced.
- Entries are taker orders placed late: a 101 s polling cycle, a +20% 24h trigger, and
  for the momentum line a 30 s settle and two polling hops
  ([edge-loss decomposition](edge-loss-decomposition-v1.md)). By the time the program
  acts, the move is visible to everyone.
- Own-collector cohorts take months per read. A public-history screen answered carry in
  a day.
- Each hypothesis carries its own protocol, code, report and review, so one test costs
  weeks of engineering before any number exists.

So the proposal changes **where an edge is looked for and how a test is built**, not
the discipline of pre-registration, which worked: every failure above was found before
money moved.

## Constraints that do not change

- **The HYP-012 v2 blind window.** Nothing on or after 2026-09-29 is read, for any
  venue, until v2 is terminal; after a stop its closed windows stay unread forever
  ([administrative stop](source-lead-forward-cohort-v2-administrative-stop.md)). Every
  historical study ends at 2026-09-28T23:59:59.999Z, and that cut bounds **every
  datum a study uses**: lookbacks, the decision, any order wait, the hold and the exit
  bar, not only the decision time. A live cost probe is not exempt and waits for v2 to
  be terminal or for its own design and ruling.
- **Registered cohorts run under their own rules.** HYP-015 is read on 2026-11-04; v2
  runs to its stop or its 2027-03-31 deadline.
- **Bybit USDT perpetuals** are the only execution venue.
- **Closed lines stay closed.** A new test on the same idea needs a new id, an untouched
  window and a stated reason why it differs.
- **Every result goes into the [discovery ledger](discovery-ledger.md)** with its
  variant count, and every window a study reads goes into the
  [seen windows registry](seen-windows-registry.md) before its first fetch.
- **Costs belong to each hypothesis's contract.** The 30 bps primary and 50 bps
  sensitivity are HYP-031's; another hypothesis fixes its own from its entry and exit
  type before any fetch.

## Data the program can use

From the [registry](seen-windows-registry.md) and its 2026-10-30 coverage checks:

- **Discovery: Bybit public history 2024-10-01 to 2026-09-28.** It is not untouched:
  carry, the funding feasibility check and HYP-031 already read parts of it. It is used
  for discovery only, except HYP-031's own reserved test year.
- **Backward holdout: 2023-02-01 to 2024-09-15.** No study read more than coverage
  there.
  - 180 to 400 USDT symbols per month;
  - klines, open interest and funding for every symbol delisted in 2023 and 2024;
  - order book history from 2023-01-18.

  2022 is left out: 5 of its 9 checked delistings have no klines or funding, and there
  is no order book. It stays unread. It is not a quiet stress test either, since
  reading it would be one more test.

  Before the holdout is opened, the batch's protocol fixes:
  - the point-in-time universe, from the archive's listing and delisting dates;
  - a missing-data rule: a symbol or window without full inputs counts as insufficient
    coverage, never silently dropped;
  - selection by rank fraction, not by a fixed top K, since the universe is about half
    of today's;
  - funding per hour, since the intervals differ;
  - today's contract costs, so the holdout tests the signal under the costs it would
    pay now.

  The regime differs, so a pass is grounds to check the signal on the current market
  (forward data and the live cost probe), and a fail is a fail.

- **Order book history** (`ob500`/`ob200` daily files) from 2023-01-18, delisted symbols
  included. It records aggregated price levels, not individual orders: a shrinking
  level can be a fill or a cancel, and our own place in the queue is never observed.
  So a maker entry is simulated by a **fill model with declared assumptions**, not "real
  fills". The default model:
  - the virtual order of size q joins the back of the visible quantity Q0 at its price;
  - after placement, aggressive volume V at our price or through it counts toward us
    only after Q0: the filled quantity is min(q, max(0, V minus Q0)). A trade through
    the price does not fill the whole order by itself, since our added quantity would
    have absorbed part of that flow;
  - partial fills are allowed: the filled part is traded, the rest is cancelled at the
    order's expiry;
  - cancels ahead of us are ignored.

  Ignoring cancels lowers the fill count, but its effect on profit has no fixed sign:
  the trades it drops may be losers. The protocol therefore declares, before any
  return is seen:
  - this model as the primary;
  - a looser model (no queue ahead) as a sensitivity;
  - a sensitivity to q, unless q is shown to be negligible against the level sizes.

- **Before any study uses the order book archive, one check on a small sample:**
  - that the host is Bybit's official source, through the links on Bybit's own
    history-data page;
  - the format: snapshots and deltas, depth, frequency, which timestamp is the
    engine's, and the rebuild rule (reset to the next snapshot on any sequence break);
  - quantity units and contract identity over time;
  - consistency with the trade archive on 10 symbol-days, 5 live and 5 delisted: no
    crossed books, buys at or above the last best ask and sells at or below the last
    best bid;
  - an intraday gap rule: an event whose order wait contains a gap above a threshold
    set from the measured frequency is `incomplete_book_data`, never filled or
    unfilled;
  - the raw windows kept immutable, with hashes and their starting snapshot.
- **Forward data** after v2 is terminal, minus v2's closed windows.

## How a hypothesis is tested

1. **Protocol before any fetch:**
   - one primary rule and 3 to 5 named variants;
   - costs per the contract;
   - the windows, registered in the registry;
   - a power and feasibility estimate: events per month and the minimum effect the
     sample can see.

   Long, fade, maker and taker versions of an idea are separate families, never pooled.

2. **Stage 0, screen** on discovery: feature deciles against the forward move, after
   costs. Logged in the ledger whatever the result.
3. **Stage 1, robustness** on discovery, for the primary rule only:
   - purged walk-forward;
   - dropping the top 5 instruments;
   - costs stressed;
   - beating the declared baselines.
4. **Stage 2, one holdout read for one frozen batch.**
   - **Why a batch:** a shared holdout read family by family stops being fresh after
     the first read, because each result steers the next choice. A correction inside
     each family does not fix that.
   - **How:** candidates that pass stage 1 are frozen (rule, parameters, costs, code
     revision) into one batch. The batch closes when three candidates are frozen or on
     2027-02-15, whichever comes first.
   - **The read:** the backward holdout is read once for the whole batch, with the
     correction counting every candidate in it and the number of families screened so
     far.
   - **After it:** the holdout is spent. The next confirmation source is forward data
     after v2 is terminal, under the same batch rule.
   - **HYP-031** keeps its own reserved test year.
5. **Stage 3, live,** only after the execution fixes below, and only with the entry,
   exit and filters the holdout tested.

Tooling is built as the studies need it, not up front:

- the first study writes its snapshots to Parquet and reads them with DuckDB, reusing
  the carry and HYP-031 fetch-and-hash code;
- components are extracted into shared code once a second study needs the same part.

## Order

1. **HYP-031 discovery read**, on the owner's go; stop on failure.
2. **Maker availability.** Outcome-blind floor counts on the full-path cut (episodes
   whose wait, hold and exit end by 2026-09-28):
   - **Pump-short maker-entry prospective** (registered 2026-08-24):
     - 1094 eligible episodes, 303 clusters, 5 ISO weeks;
     - the policy selects a decision in 625 of them, on eight venues;
     - Bybit has 50 of the 625, on 24 clusters, so a Bybit-only reading cannot reach
       the floor (100 fillable, 30 clusters, 4 weeks).

     The contract reads all venues pooled. So its result speaks about maker entry
     against pump momentum in general, each venue's fills judged on its own candles,
     not about Bybit execution.

     **Steps:**
     1. Count the fillable episodes with their clusters and weeks, with no PnL, on
        `--until 2026-09-28T13:00:00Z`. The last bar the path uses then ends by
        2026-09-28 21:00Z.
     2. If the floor is met, one read of the primary sensitivity
        (`activation_marketable_as_cash`), on the owner's go.

     **What the result means:** confirmed, not confirmed or insufficient data under the
     contract's frozen rules, for that contract only (its signals, entry, exit and venue
     mix).
     - It says nothing about other fade variants. A pooled result is also compatible
       with the opposite on a single venue.
     - "Not confirmed" does not by itself show adverse selection.
     - A positive result justifies an order book fill-model study on Bybit, nothing
       more.

     The read is optional: it closes an old contract and is not a gate for any new
     study. Whether to defer other fade work for budget reasons is a separate decision.
     Pump-short itself stays closed.

   - **Liquidation maker upper bound** (#332):
     - the Bybit scopes miss the floor: long 233 episodes on 21 clusters, short 217 on
       19 clusters with a 39% top asset share;
     - only the Binance scopes meet it, and Binance is not tradable for the owner.

     Proposed: no read. Borrowing Binance's sufficiency is not allowed. Liquidation
     reversion may return later as a new hypothesis on the order book fill model, and
     must meet its own floor of independent instruments there.

3. **One pump-prediction question, "compressed spring",** long only:
   - **Question:** while an instrument's price range compresses, open interest in coins
     grows and funding is at or below zero, do aggressive sells fail to push the price
     down? If so, does it then move up more than the costs?
   - **Primary setup: sell absorption.**
     - A measurable definition is fixed before the fetch: taker sell volume against
       price change over the same bars, normalized.
     - The reason: negative funding with growing open interest suggests crowded
       shorts, and a bid that holds under their selling is the setup for a squeeze.
     - Buy absorption (buying that fails to lift price) points to a large seller, so it
       is not part of this setup.
   - **A separate variant: large taker-buy breakout** out of the compressed range. It
     is never pooled with the absorption setup.
   - **Horizon:** one primary horizon, chosen in the protocol from the power estimate.
     A barrier exit (take profit and stop in ATR units with a time stop) is one of the
     declared variants. Return per dollar-hour is reported beside the primary metric.
   - **Features:**
     - each normalized against its own 14-day history and ranked across instruments at
       the same time;
     - large-trade taker imbalance from the trade archive;
     - time of day;
     - open interest at 1 h for screening and 5 min for the event windows. Controls and
       baselines get the same 5 min fetch, or the comparison is biased.
   - **Baselines:**
     - plain momentum;
     - momentum relative to the market;
     - range compression with breakout alone (no open interest, funding or trade
       flow);
     - a random draw matched on liquidity, time of day and number of trades.

     The full setup is also compared with versions that drop one input each, so every
     input shows what it adds.

   - **Variants:** a shallow gradient-boosted model is one of the declared variants,
     with purged cross-validation, never the primary.
   - **Fallback:** the protocol and power estimate come first; if the estimated sample
     is too small, the question changes before any fetch.

4. **Extract the common components** used by steps 1 to 3.

## Families after the first question

One family at a time through stages 0 and 1, aiming at 2 to 3 screens a month. The
order is the expected chance after costs, highest first:

| #   | Family                                   | Question                                                                                      | Main risk                                            |
| --- | ---------------------------------------- | --------------------------------------------------------------------------------------------- | ---------------------------------------------------- |
| 1   | Short-squeeze continuation               | After a first impulse, do negative funding and rising open interest predict more upside?      | Few events                                           |
| 2   | Pressure against price response          | Does strong taker flow that fails to move price predict a reversal, and flow that does a run? | Trade archive volume                                 |
| 3   | Liquidation-cascade reversion with fills | Does price revert after forced selling, with maker fills from the order book fill model?      | Adverse selection                                    |
| 4   | Spot against perpetual premium           | Does the perpetual's premium to the index predict the next hours?                             | Small effect against costs                           |
| 5   | Post-pump short on open-interest turn    | Does a falling open interest after a pump mark its end?                                       | Close to closed lines: needs a new id and difference |
| 6   | Relative strength                        | Do instruments leading the market keep leading for hours to days?                             | Known effect, may not clear costs                    |

Not on the list: cross-venue lead-lag. The source-lead family (HYP-012b, HYP-012c)
already failed on that idea.

## Before any live money: execution

Two issues in `apps/execution/schurfer_execution/orders.py`, both found in the
2026-10-17 audit's live paths, are fixed with tests before any live run:

- **Stop-loss placement failure** (around lines 560 to 600). The entry is already
  marked accepted. The emergency-close path returns without marking the attempt
  terminal, so reconciliation quarantines the attempt and blocks all entries.
- **Unknown submission** (around lines 435 to 451). On a network error the attempt
  becomes `submission_unknown` and no stop is placed. A late fill then leaves an
  exchange position without a stop, which reconciliation v1 only flags for manual
  action, and the notifier alerts are silenced.

## Live path and capital steps

After a candidate passes stage 2:

1. **Live cost probe** under its own design: minimum size, at the candidate's signal
   times, recording implementation shortfall (signal price against the average fill)
   for every order.
2. **LIVE_MICRO with USD 50** under the [live-probe design](live-probe-step2-design-v1.md),
   reproducing the tested contract: the same entry type, exits and filters as the
   holdout read. A PostOnly entry or a new filter changes the entry time and which
   signals trade, so it is a separate variant: it passes its own test before it trades
   live.
3. **What live trades can and cannot show.** With a 3% per-trade spread:
   - the standard error of the mean is about 42 bps at 50 trades and 21 bps at 200,
     a 95% interval of about plus or minus 42 bps;
   - as a precision guide under simplifying assumptions (independent trades, a fixed
     spread), not a power calculation: telling an edge from zero at 95% takes about 150
     trades for 50 bps and about 900 for 20 bps;
   - volatility-targeted sizing narrows the spread and shortens that.

   So live samples are not proof. They check costs early and catch a broken edge.

4. **The scaling rule,** fixed before the first live trade:
   - **Same metrics as the holdout:** net return per trade under the same cost
     accounting, and implementation shortfall against the model's assumed costs.
   - **Checkpoints at 50, 200 and 500 trades,** and only there:
     - costs: realized shortfall within the model's tolerance, which 50 trades can
       test;
     - edge: an allowed loss of edge δ is fixed before going live. The size goes up only
       when the lower one-sided confidence bound of (live mean minus holdout estimate)
       is above minus δ. The bound carries the uncertainty of both estimates and the
       dependence between trades (block bootstrap by day or week), with a correction
       for the number of checkpoints;
     - risk: drawdown within a limit set in dollars before going live.
   - **Three outcomes at a checkpoint:**
     - **pass:** costs within tolerance, the edge bound above minus δ, risk within its
       limit. The size may go up one step.
     - **inconclusive:** the edge bound is not above minus δ, but costs and risk are
       within their limits. The size stays where it is.
     - **stop:** costs outside tolerance, or the drawdown limit is hit. The size goes
       down a step or the line returns to paper.

     Not detecting a loss of edge is never a reason to scale. With a 3% spread, a pass
     on the edge is unlikely before a few hundred trades, so the early checkpoints are
     mostly about costs and risk.

   - **Capital steps** (USD 50, 250, 1 000, then more): at most one step per passed
     checkpoint, sized so the step's worst expected loss is one the owner accepts.

5. **Later, with two or more live lines:**
   - volatility-targeted sizing;
   - a BTC shock breaker;
   - a per-order liquidity cap;
   - a fixed share of capital kept for testing new lines at minimum size.

A second venue is considered only after its access is confirmed for the owner.

## Calendar

| When                   | What                                                                                                    |
| ---------------------- | ------------------------------------------------------------------------------------------------------- |
| 2026-10-31             | Program review and this strategy; disk reading; v2 checkpoint                                           |
| 2026-11-04             | HYP-015 formal read (owner's go)                                                                        |
| November               | HYP-031; maker read; execution fixes; compressed-spring protocol and discovery; first Parquet snapshots |
| December to January    | Families 1 to 4 through stages 0 and 1; survivors frozen into the holdout batch                         |
| By 2027-02-15          | The batch closes and the backward holdout is read once                                                  |
| February to March 2027 | First live cost probe and LIVE_MICRO, only for a stage 2 survivor                                       |
| 2027-03-31             | v2 deadline; forward data open under v2's rules                                                         |

The dates are a plan, not gates: a step opens when the previous one has its result.

## What stops

- Polling-based pump chasing as a source of new hypotheses: the program is last to the
  move there.
- New collectors for a hypothesis that has not passed on public history first. This
  covers branch A (the Gate collector) and an own order book collector, since public
  order book history exists.
- Carry stages A and B at this size (closed by the economics screen).
- Dashboards and UI work until a live line is net positive.
- A separate review for every document change that moves no money. Such changes are
  batched; the reviewer keeps two touchpoints per research PR: the design before
  building, and one review before merge.

## Cost discipline instead of a stop date

- **Variant budget:** one primary rule and 3 to 5 variants per family, declared before
  the first fetch; the ledger records the count, and survivors are corrected for it.
- **One family at a time** through each stage.
- **Monthly review:** owner hours and hosting cost against what was learned.
  - A family that fails stage 0 or 1 closes and is not re-tuned.
  - If by 2027-03-31 ten or more families went through stage 1 and none survived
    stage 2, the program changes the kind of edge it looks for, not just the next idea.

## Settled in review

- **Holdout:** one frozen batch, then forward data.
- **Maker read:** optional, closes the old contract, gates nothing.
- **Scaling:** the confidence-bound rule above.
- **Order book fill model:** the volume-limited model is the primary, the looser
  model is a pre-declared sensitivity, both chosen before any return is seen.

Signal definitions, power and execution details are settled in each candidate's own
protocol.
