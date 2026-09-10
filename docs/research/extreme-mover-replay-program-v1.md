# Extreme-mover replay program v1

## Status and decision boundary

This is an Observation/Discovery program, not a registered Confirmation
hypothesis, strategy promotion, production-trading change, or authorization to place
orders. Its first job is to find out which proposed rules can already be replayed
honestly on preserved data and whether any broad segment is worth prospective
collection.

It narrows and sequences the existing
[LBank-first market-path study](lbank-first-market-path-study-v1.md); it does not open
a duplicate LBank research line or replace that study's lifecycle and identity
requirements.

JUGGERNAUT and FRONG, the other movers observed on 2026-09-10, and the
`2026-08-27T00:00:00Z` through `2026-09-11T00:00:00Z` feasibility window are already
viewed discovery data. They may explain or falsify a mechanism and test the report,
but cannot validate a rule selected after seeing them. A blind first-observation long
is a cash/control baseline, not the proposed strategy.

The program keeps two economically distinct questions separate:

1. **Continuation:** after an extreme move is detected, do observable state variables
   identify a long whose return remains positive after executable entry costs?
2. **Exhaustion:** after a failed continuation or reversal is observed, do observable
   state variables identify a delayed short whose return remains positive after costs?

An early short is not a hedge for the continuation question. The 2026-09-10 cases
show why: both would have lost heavily at the early 15-minute mark, while only the
later JUGGERNAUT state was favorable to the inspected delayed short. Those numbers
are case studies, not evidence of an edge.

## Money objective and current portfolio

The near-term objective is not "find a green backtest." It is to reach one of three
money-relevant decisions with the least new code:

1. reject a direction whose preserved-data net economics are already poor;
2. identify the exact missing observation that prevents a plausible mechanism from
   being measured, then collect only that observation;
3. freeze one small prospective candidate that can later earn a paper/live proposal.

The portfolio already contains evidence that must not be reset by this program:

- early-momentum v4 is economically negative on its mature evidence and is not a
  generic continuation candidate to retune;
- HYP-024 found no usable delayed-short/order-flow separation and that line remains
  stopped;
- HYP-027 has a registered one-time decision-delay-group read and group B is 16
  episodes short of readiness as of 2026-09-10;
- pump-short maker and Gate-to-Binance source-lead cohorts continue under their own
  frozen checkpoints; they are not inputs to this discovery grid;
- HYP-015's 12h sibling is already specified, but starting another paper worker is a
  portfolio decision, not the automatic consequence of spare engineering capacity.

Until the portfolio explicitly records which two lines occupy the Confirmation slots,
the default is not to start another one. The extreme-mover work stays in Discovery and
may run in the primary evidence slot without consuming a Confirmation slot.

## Replay fidelity ladder

Every result must carry one of these levels. Higher sample count does not upgrade a
lower-fidelity row.

| Level               | Preserved evidence                                                                | Honest use                                                           | Claim that remains forbidden                                  |
| ------------------- | --------------------------------------------------------------------------------- | -------------------------------------------------------------------- | ------------------------------------------------------------- |
| A: executable paper | Point-in-time entry and exit bid/ask VWAP, timing, fill and costs                 | Prospective paper execution economics at the recorded notional       | Real fill probability, queue position or larger-size capacity |
| B: path replay      | Exact native OHLCV path plus point-in-time entry liquidity; modeled exit slippage | Direction, path-dependent rules, MFE/MAE and conservative net screen | Fully executable exit economics without a preserved exit book |
| C: endpoint replay  | Exact native same-venue entry/outcome endpoints and extrema                       | Broad long/short/cash screen and coverage diagnosis                  | Stop/target ordering or arbitrary intra-window entry timing   |
| D: state only       | Trigger/decision snapshots without an exact forward path                          | Coverage, feature availability and capture design                    | Return, expectancy or strategy comparison                     |

Current momentum paper probes can reach level A. Binance/Bybit minute bars can support
level B when exact entry liquidity exists. Most of the 445 exact 60-minute outcomes
are level C. LBank is currently level D for this window. Repeated radar decisions do
not turn level D into a price path because their continued presence is conditioned on
the event remaining active.

## What existing data can answer

The first production feasibility audit selected one decision per pump event, then
required a `complete`, `forward_v1`, exact same-venue 60-minute outcome. It was a
read-only exploratory query, not a frozen report artifact. The report PR must
reproduce and fingerprint the cohort before relying on these counts.

| Venue     | Selected episodes | Exact 60m paths | Immediate use                                      |
| --------- | ----------------: | --------------: | -------------------------------------------------- |
| LBank     |               320 |               0 | Entry-state study only; no honest 60m return claim |
| MEXC      |               230 |             136 | Discovery replay                                   |
| Binance   |               178 |             145 | Discovery replay plus full-universe 1m bars        |
| BingX     |               145 |             106 | Discovery replay                                   |
| Gate      |                57 |              33 | Discovery replay                                   |
| Bybit     |                29 |              18 | Discovery replay plus full-universe 1m bars        |
| KuCoin    |                 6 |               0 | Coverage only                                      |
| Bitget    |                 5 |               3 | Coverage only                                      |
| XT        |                 3 |               2 | Coverage only                                      |
| Toobit    |                 2 |               0 | Coverage only                                      |
| OKX       |                 2 |               2 | Coverage only                                      |
| **Total** |           **977** |         **445** | **45.5% exact 60m coverage**                       |

On these preserved inputs we can replay, without pretending to know more than was
recorded:

- one deterministic observed entry decision per episode: first observation or first
  quality-eligible observation, with the full eligibility/missingness funnel;
- long, short, and cash on the same exact-venue price path;
- saved ask/bid VWAP at entry for declared notionals where the snapshot can fill it;
- fee, funding, and entry-slippage sensitivity; endpoint return plus MFE and MAE;
- Bybit/Binance-only fixed-delay, retracement, 1m acceleration, activity and bar-order
  variants, because those venues have the required full-universe minute capture;
- episode-level deduplication and clustering by base asset, venue and calendar week.

Existing data cannot honestly reconstruct:

- LBank forward returns: it is the largest source in this window and has zero exact
  native 60-minute outcomes;
- real exit depth, spread and fill at every horizon on all venues;
- the intra-minute order of target, stop and liquidation touches where only sparse
  outcomes or snapshots exist;
- second-level acceleration, taker-flow transitions or order-book replenishment on
  BingX/LBank when they were not captured;
- a cross-venue lead before the radar trigger unless both source and target markets
  were already recorded at that time;
- an unbiased path assembled only from repeated `trade_decisions`, because snapshots
  continue while an event remains active and their absence is not random.

The retrospective report can therefore screen price edge and modeled costs. It must
not call those modeled exits fully executable economics when an exit-side order book
was not preserved.

## First replay contract

The first implementation is a bounded report, not a parameter optimizer. It has a
cross-venue endpoint screen and a smaller Bybit/Binance minute-path replay; it does
not pretend the latter's features exist on every venue.

- Freeze one selected decision per `pump_event_id` before attaching outcomes.
- Match exact venue, canonical instrument identity, market type, `complete` status,
  and `resolver_version=forward_v1`; keep missing paths in the coverage funnel.
- Report 15m, 60m and 240m only when the exact path exists. Do not substitute another
  venue or a later decision.
- Compare cash, long and short for the same episode. Use the preserved USD 100
  entry-impact level as the primary descriptive liquidity probe; report unfillable
  books explicitly rather than falling back to last price. USD 500 is capacity
  sensitivity, not a position-sizing or capital decision.
- In the cross-venue screen, compare only first observation and first observed
  quality-eligible decision. Repeated decision snapshots are not a continuous path.
- Before querying outcomes, pre-declare one fixed-delay and one retracement rule for
  the Bybit/Binance minute-path subset. Keep continuation and exhaustion families
  separate and log every tried cell.
- Apply venue fees and observed funding where available. Show a conservative exit
  slippage range when exact exit depth is absent.
- Report means, medians, win rate, profit factor, tail loss, MFE/MAE, sample counts,
  exact-path coverage and unfillable-entry counts. Cluster uncertainty by base asset;
  include venue/week concentration and leave-one-venue-out sensitivity.
- Treat JUGGERNAUT, FRONG and the feasibility window as viewed discovery. Any selected
  candidate needs a frozen prospective cohort and an untouched Confirmation read.
- Stop the direction if no broad, interpretable segment is positive after conservative
  costs. Do not rescue it by increasing the grid or selecting one token, venue or
  horizon after seeing results.

### Dataset and coverage output

The report emits an immutable dataset manifest before any aggregate result:

- half-open observation bounds and generation time;
- Git revision and dirty-tree state;
- selected `pump_event_id` and `decision_id` pairs, with selection version;
- exact venue, native market id, canonical instrument id, asset class, market type,
  data/capture/resolver versions and path provenance;
- event, exchange, receive and persistence timestamps where available;
- one terminal coverage reason for every missing horizon or unusable entry book;
- content fingerprints for the cohort, rows and any external immutable path artifact;
- all cost, latency, notional and strategy-cell versions;
- deterministic bootstrap seed and implementation version.

Selection happens before the outcome join. Tokenized equities, spot instruments,
unknown asset classes, fresh-listing baselines and unresolved identity do not silently
join the crypto-perpetual denominator: they receive separate counters and cannot
support a candidate.

### Fixed discovery family

The cross-venue level-C family is fixed to avoid an open-ended strategy search:

| Dimension             | Cells                                                                           |
| --------------------- | ------------------------------------------------------------------------------- |
| Observed entry anchor | first decision; first decision that passed recorded market quality              |
| Direction             | long; short; cash                                                               |
| Horizon               | 15m; 60m; 240m                                                                  |
| Entry liquidity       | preserved USD 100 impact/VWAP level; USD 500 sensitivity only                   |
| Exit sensitivity      | shared conservative model; zero and double modeled exit slippage as diagnostics |

Cash is included in every denominator. A missing quality decision, unfillable book or
missing path stays visible and contributes cash in the per-signal view; it is not
dropped until the per-filled-trade diagnostic is computed separately.

The level-B Binance/Bybit family is separately bounded. Proposed parameter values must
be accepted in the planning/replay review before the production outcome query runs.
The default proposal reuses the existing LBank study's 0/1/5/15-minute delay grid and
adds exactly these two mechanism cells:

- `continuation_hold5_v1`: after five consecutive complete post-trigger 1m bars, every
  close remains above the point-in-time trigger price and the fifth close is not below
  the first; enter long at the next complete 1m open;
- `exhaustion_retrace10_fail5_v1`: after a post-trigger running high, a complete 1m
  close is at least 10% below that high; over the next five consecutive complete bars
  no close reclaims half of that peak-to-retracement distance; enter short at the next
  complete 1m open.

Both use fixed 15m/60m/240m exits after their own entry and remain cash when their
condition never occurs. Any missing minute inside selection or the requested outcome
window makes that path unresolved; it is not skipped over. These round values are a
review proposal for a viewed Discovery window, not a claim derived from JUGGERNAUT or
FRONG. The owner/reviewer may change them before the first outcome query; after that
lock, their completed-bar definition, entry alignment, missing-minute behavior and
multiplicity count are immutable for this run.

### Economics and robustness output

For each cell, report both per-signal and per-filled-trade economics:

- resolved, unresolved, rejected, cash and filled counts;
- distinct assets, venues and UTC weeks;
- gross and net return mean, median and quantiles;
- total net PnL at the descriptive notional, profit factor and win rate;
- maximum drawdown, worst trade and longest losing streak;
- MFE, MAE and time-to-extreme where path fidelity permits;
- entry impact, modeled exit impact, fees and funding separately;
- fillable share, simultaneous-position peak and capital-occupancy time;
- single-asset, single-venue and single-week concentration;
- asset-cluster bootstrap interval, leave-one-asset-out, leave-one-venue-out and
  leave-one-week-out sensitivities;
- a table of every tried cell, including losers and invalid cells.

The report must distinguish a tail strategy from a typical-trade strategy. A negative
median does not alone reject a positively skewed continuation strategy, but a positive
mean supplied by one asset or one week cannot nominate a candidate.

### Discovery branch decision

This is a routing verdict, not Confirmation. Exactly one branch is recorded for each
direction:

| Branch                    | Required reading                                                                                                                                                                | Next action                                                                                                           |
| ------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------- |
| `stop`                    | Mature exact-path sample, but no cell has positive after-cost mean and profit factor above 1, or every apparent gain disappears without one asset/week                          | Close the direction; no new capture or threshold search                                                               |
| `existing_data_candidate` | One interpretable cell is positive after costs, clears the declared episode/asset/week floor, and keeps its sign across the registered concentration sensitivities              | Freeze exactly that rule for a new prospective cohort on already supported venues                                     |
| `measurement_blocked`     | A pre-declared mechanism cannot be evaluated because its required point-in-time field is absent, while the lower-fidelity screen gives a concrete economic reason to measure it | Write the smallest capture contract for that field and venue                                                          |
| `insufficient_discovery`  | Exact-path/diversity floor is not met and there is no defensible positive or negative economic read                                                                             | Record the missing denominator; collect passively only if its expected information value justifies storage and effort |

The replay PR must declare its episode, asset and week floors before the production run.
The project's default is 100 resolved episodes and 30 asset clusters; any exception for
a bounded venue universe must be justified before results and cannot be introduced to
rescue a positive chart.

## Ten delivery gates

These are ten delivery outcomes, not a promise to manufacture ten PRs. Only gates 1-4
are unconditional. Gate 4 selects one of four branches; the unused branch is deleted
from the active queue. Registered reads continue in parallel and can reorder the queue
when they produce stronger evidence.

|   # | Delivery                                            | Form and proposed branch                                                                                                                                          | Entry condition                                                                                         | Definition of done / stop                                                                                                             | Production impact                                   |
| --: | --------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------- |
|   1 | Synchronize the plan and today's integrity evidence | Documentation PR, `docs/near-term-edge-program`                                                                                                                   | ENG-024 deployed and verified                                                                           | Roadmap, ENG-023/024 status, data coverage, fidelity ladder and this queue agree                                                      | None                                                |
|   2 | Build the exact replay dataset and report           | Analytics PR, `research/extreme-mover-replay-v1`                                                                                                                  | Gate 1 reviewed; discovery cells/floors locked before result query                                      | Unit boundary tests, real-PostgreSQL join test, deterministic artifact, coverage reasons, full economics table; no migration          | Analytics/report only; deploy separately authorized |
|   3 | Produce the single retrospective discovery artifact | Production report run, no PR                                                                                                                                      | Gate 2 merged/deployed from clean main                                                                  | Artifact archived with fingerprints; each direction receives exactly one routing verdict                                              | Read-only production query                          |
|   4 | Portfolio checkpoint                                | Decision record, normally in the result PR/docs                                                                                                                   | Gate 3 artifact plus current registered-cohort readiness                                                | Choose `stop`, `existing_data_candidate`, `measurement_blocked`, or `insufficient_discovery`; remove all incompatible downstream work | None                                                |
|  5A | Freeze an existing-data prospective candidate       | Research-contract PR, `research/extreme-mover-prospective-v1`                                                                                                     | Gate 4=`existing_data_candidate`; Confirmation slot available                                           | Exactly one rule/direction/venue set, untouched cutoff, costs, floors and verdict; no evaluator tuning later                          | No worker or order                                  |
|  5B | Repair cohort integrity needed by a blocked venue   | One bounded fix PR per invariant; first candidates are `fix/mexc-instrument-lifecycle-opening-time-v1` and `fix/pump-scanner-asset-class-and-listing-baseline-v1` | Gate 4=`measurement_blocked` and the named defect affects the chosen cohort                             | Native endpoint semantics evidenced; regression test; changed identities/cohort counts audited                                        | Scanner/data semantics; separately deployed         |
|  6B | Freeze the missing market-path contract             | Data-contract PR, `feat/canonical-market-path-contract-v1`                                                                                                        | Gate 5B complete; exact missing fields named                                                            | Venue capability matrix, raw envelope, timestamps, retention, gaps, checksums, storage-rate budget and named consuming report         | No capture process yet                              |
|  7B | Run the smallest venue canary                       | Collector PR, `feat/extreme-mover-venue-canary-v1`                                                                                                                | Gate 6B reviewed; backup/free-space safety permits it; deployment explicitly authorized                 | One or at most two venues, event-triggered 2-4h capture, bounded resources, restart-safe cursor, no strategy                          | New measurement worker only                         |
|  8B | Prove capture quality before expansion              | Read-only health/report run; at most one evidence-driven repair PR                                                                                                | Enough canary events to measure continuity                                                              | Identity, latency, gaps, event/receive time, depth fill, storage/day, restart and retention checks pass; otherwise repair or stop     | Read-only unless a repair is separately deployed    |
|   9 | Run prospective discovery/confirmation              | Evaluator PR if not already in 5A/6B, then one frozen run                                                                                                         | Either 5A has matured on existing feeds or 8B has sufficient new paths                                  | One candidate at most per family; negative mature EV=`fail`; positive immature=`insufficient_data`; all cells archived                | Read-only report                                    |
|  10 | Implement prospective shadow/paper execution        | Paper PR, `feat/extreme-mover-shadow-v1`                                                                                                                          | Gate 9 candidate, free Confirmation/paper capacity, execution review, explicit production authorization | Exact quote deadlines, bid/ask VWAP, costs, rejected/missed outcomes and worker health; `AUTO_TRADE` remains false                    | Paper-only worker; no real orders                   |

`5A` and `5B-8B` are mutually exclusive branches unless a later independent result
opens a new program. Gate 10 is the end of this ten-gate plan. A live micro-order is
not hidden inside it; that would require a later proposal with execution-safety gates,
capital/risk choices, owner review and explicit live authorization.

### Parallel registered reads

These are not delayed by the replay PR and do not justify peeking early:

- run HYP-027 readiness-only until both groups reach 150 episodes, then perform its
  single formal read before the 2026-09-30 hard window edge;
- run the pump-short maker checkpoint around 2026-09-21 under its own maturity rule;
- run the source-lead forward cohort no earlier than its four-week floor around
  2026-10-01 and only when its episode/diversity/concentration conditions pass;
- let the liquidation-maker upper-bound cohort mature under its own stopping rule;
- reconsider starting HYP-015 hold12h only at a portfolio checkpoint with a free slot
  and separate production authorization.

Each completed read can preempt gates 5-10 if it produces a better money candidate.
It cannot change gate 2's already locked cells or retroactively reclassify viewed data.

## Branch response after the first replay

The next engineering action is determined by evidence, not by the original list:

- **Both directions stop:** archive the result, build no venue collector, and move the
  primary slot to the strongest due registered line. Extreme movers remain a radar/UI
  observation, not a strategy.
- **Only current Binance/Bybit data works:** freeze a prospective rule on those venues.
  Do not add LBank merely because its missed denominator is large.
- **BingX/MEXC/Gate endpoint economics look useful but path rules are blocked:** capture
  only the feature and venue named by the result. Prefer a price/BBO path over trades or
  depth if price/BBO alone can answer the question.
- **Only LBank looks interesting without native outcomes:** perform the existing
  LBank-first lifecycle/asset-class/path-contract prerequisites. No same-ticker proxy
  may promote it.
- **One token, venue or week creates the gain:** classify it as concentration, not an
  edge; keep the case study for mechanism work and stop promotion.
- **Positive gross, negative net:** stop the taker version. A maker version is a new
  execution hypothesis and may proceed only if existing maker evidence supports it;
  it is not a free cost toggle in the same report.
- **Insufficient because collection is recent:** estimate events/week and calendar time
  to the declared floor. Continue passive collection only if the wait is bounded.

## Implementation and verification budget

Gate 2 is intentionally one coherent report PR: one repository query/builder, one pure
evaluation module, one CLI/Make target, tests and the contract update. It has no schema,
worker, API or UI work. Split it only if the real-PostgreSQL dataset builder proves too
large to review safely; do not split a non-running scaffold from its only consumer.

For conditional capture work:

- one PR changes one data invariant or one worker boundary;
- a collector PR must name the report it unlocks and estimate rows/bytes per event and
  per day before deployment;
- no canary may reduce backup headroom below the recovery requirement or silently use
  unbounded retention;
- no multi-venue expansion before one-venue identity, continuity and restart evidence;
- no UI PR until an operator decision actually lacks visibility; raw tables are not a
  product requirement by themselves.

Verification is proportional to the boundary: pure selection tests, a real-PostgreSQL
join/selection regression, deterministic manifest/fingerprint tests, missing-path and
duplicate-episode cases, and production read-only smoke after deployment. Capture adds
restart, gap, timestamp, rate-limit and storage tests. Paper adds execution-safety
review; live remains out of scope.

## Portfolio balance and stopping rules

Use the existing WIP limit: one profit/evidence implementation and one bounded support
implementation. Passive cohorts and due report runs do not occupy those slots. Keep at
most two active Confirmation lines; this program remains Discovery until a prior line
finishes or is parked.

Over these gates, target roughly 60% of implementation effort at economic evidence,
20% at irreversible data capture/reliability, 10% at operator visibility and 10% at
contracts/documentation. Safety, corruption and non-recoverable capture loss may
preempt that mix. Do not let discretionary cleanup consume more than two consecutive
PRs, and do not let a promising chart bypass identity, costs or out-of-sample gates.

Progress toward money is measured by decisions retired as well as strategies promoted:

- a negative replay saves a capture and execution build;
- a coverage failure names the smallest data feature to add;
- a positive discovery result freezes one rule rather than opening a larger search;
- only positive untouched, after-cost paper evidence can justify an execution proposal;
- capital size, leverage and live mode are never inferred from research results.

At every gate, update a compact portfolio scorecard:

| Field               | Required answer                                                         |
| ------------------- | ----------------------------------------------------------------------- |
| Evidence status     | observation, discovery, confirmation, paper, failed or parked           |
| Next money decision | what one report or worker can authorize                                 |
| Opportunity rate    | independent episodes/assets/weeks and expected time to floor            |
| Net economics       | per-signal and per-fill EV, profit factor and cost breakdown            |
| Risk                | drawdown, worst trade, losing streak, overlap and capital occupancy     |
| Capacity            | recorded notional/depth only; larger sizes remain scenarios             |
| Data debt           | exact missing field, affected denominator and whether it is recoverable |
| Engineering spend   | PRs since last economic answer and next stop condition                  |

The weekly portfolio review is short and evidence-driven: due formal reads first;
then the primary economic gate; then one support issue that prevents data loss,
execution safety or the current report. Cosmetic refactors and broad data platforms do
not enter the queue while the primary gate can still answer the money question.
