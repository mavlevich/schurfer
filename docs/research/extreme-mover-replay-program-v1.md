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

## Ten delivery gates

These are ten outcomes, not a promise to manufacture ten PRs. Report runs and passive
collection are deliberately used as cheap gates before code or infrastructure.

1. **Planning PR:** record this contract, the ENG-024 production result, current data
   coverage and the ordered program. No production mutation.
2. **Replay PR:** implement and test the bounded existing-data report above, including
   exact-venue/resolver checks, visible coverage, costs and a machine-readable artifact.
3. **Discovery run:** archive one production artifact and decide `stop`,
   `prospective_capture_needed`, or `prospective_candidate`. A negative broad result
   stops this line before capture work.
4. **HYP-027 gate:** wait until both frozen age groups meet their 150-episode floor,
   then perform its one formal read. This is a run, not a new implementation PR.
5. **HYP-015 passive evidence:** when a Confirmation slot is free and with separate
   production authorization, start the already registered 12h paper sibling and
   compare it prospectively with the contemporaneous baseline. It does not block the
   extreme-mover replay.
6. **Capture-contract PR, conditional:** only if gate 3 identifies a plausible but
   unmeasured mechanism, define event-triggered 2-4h ticker/trade/depth capture,
   provenance, retention, gaps and capacity. Measurement only; no strategy.
7. **Venue-canary PR, conditional:** implement the smallest canary for the venues that
   block the selected mechanism, initially BingX/LBank unless gate 3 points elsewhere.
   Deployment requires separate authorization.
8. **Capture-quality run:** measure continuity, exact identity, latency, storage cost,
   entry/exit-book availability and restart behavior. Repair only observed failures;
   do not expand venues while the canary is unreliable.
9. **Prospective-discovery PR and run:** evaluate the pre-declared continuation and
   exhaustion mechanisms on newly collected observations. Freeze at most one candidate
   in each family, or stop both.
10. **Shadow/paper PR, conditional:** only after a candidate survives prospective
    discovery and a Confirmation slot is free, implement a no-live-order forward
    contract and paper worker. Live micro-trading remains a later, separately reviewed
    and authorized safety step.

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
