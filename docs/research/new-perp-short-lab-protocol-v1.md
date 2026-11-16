# New-perp short, laboratory protocol v1 (DRAFT, 2026-11-16)

Study identifier: `new-perp-short-v1`. Idea: `I25` in the
[consilium ideas list](../consilium/ideas.md). A hypothesis identifier (`HYP-039` is the
next free number at the time of writing) is assigned only when the contract is
registered (stage 2, below).

Status: **DRAFT protocol for the colleague's design discussion. It is a design document
only.** It reads no data, fetches nothing, registers nothing, authorizes no order and
changes no code. The checks it describes (launch-time meaning, catalog against trade
archive, identity per launch, power calibration, the laboratory read) are written as
**methods with pass and fail criteria and status codes**; each is run later, on the
owner's separate go. Merge of this document is also a separate decision of the owner
and the colleague.

Chain of evidence behind it:

- the count of new Bybit linear USDT perpetuals passed its planned gate: 263 launches
  over 53 ISO weeks in the discovery year, 254 in the backward holdout
  ([result](catalyst-feasibility-v1-result-2026-11-16.md), section "New perp launches"),
  from the `instrument_launches` field of the
  [report](evidence/catalyst-feasibility-v1/report.json.gz);
- that count read no price. It measures the coverage of Bybit's API catalog, not a
  verified full history, and `launchTime` is not shown to be the moment trading opened
  (the limits listed there are carried into this protocol as checks, section 4);
- the idea's own paper screen ([A, 2026-11-15](../consilium/screens/a-2026-11-15.md))
  gave a working hurdle above 41 bps plus funding paid and demanded a fixed protocol
  before any price, with at most three variants. This document follows that.

This protocol is not a claim that a short of new perps has an edge. The mechanism is
widely known and the market has a price for it: funding. The edge exists only where the
drift exceeds funding, costs and the squeeze risk, which is a narrower claim than "new
listings fall".

## 1. Question

Can a short of a newly launched Bybit USDT perpetual, opened a fixed time after launch
and held a fixed time, make a **net** return that is positive on all reserved capital
after costs from real execution, funding actually accrued, the cost of the right-tail
risk rule and the 5% annual hurdle, and is it **testable at all** with about 263
launches that are not independent?

Two answers are wanted, in this order, and the order is the point of the document:

1. **Is a correctly testable candidate there?** (stages 1 to 3: reconstructibility,
   identity, coverage, capital feasibility, power). Most of the stop codes in section 8
   can end the study here, **before any return is read**.
2. **If yes, what does the laboratory say?** (stage 4) and where could a positive
   laboratory result be confirmed (section 9).

The study does not look for a positive result at any cost. A stop at stage 1 to 3 is a
useful, cheap result.

## 2. Windows and what each may do

| Window                                              | Dates (UTC)              | Role here                                                                                                                                                         |
| --------------------------------------------------- | ------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Discovery year                                      | 2024-10-01 to 2025-09-30 | open, used for development; the laboratory. A result here is a hypothesis for confirmation, never evidence by itself                                              |
| Backward holdout                                    | 2023-02-01 to 2024-09-15 | one shared single shot (section 9). In this protocol it is read only at **coverage level**, and its ticker list stays sealed outside Git as in the catalyst study |
| HYP-031 test year                                   | 2025-10-01 to 2026-09-28 | excluded and unassigned. Not used here without a separate reassignment (section 9)                                                                                |
| HYP-012 v2 blind window                             | from 2026-09-29          | closed to every price read. Nothing here touches it                                                                                                               |
| Anything else (before 2023-02-01, 2024-09-16 to 30) | -                        | discarded                                                                                                                                                         |

**Path cut at the window end (fixed now).** An episode belongs to a window only if its
**whole path**, from launch to the exit time of the variant, lies inside the window. A
launch whose path would run past the window end is `window_censored`: counted, listed
with its reason, never priced. For the discovery year the last eligible launch is
2025-09-16 for the primary variant (7 d age plus 7 d hold) and 2025-09-09 for the
longest (14 d plus 7 d). This keeps every price read out of the HYP-031 test year and
the blind window, at the cost of about 5 to 12 of the 263 launches. The same cut applies
at 2024-09-15 for the holdout. The cohort is not extended to compensate.

## 3. Cohort

- **Unit:** one launch is the first Bybit linear USDT perpetual of a base asset (after
  stripping a multiplier). Every launch is its own base (263 of 263 in the count).
- **Source:** Bybit `instruments-info` for `Trading`, `Delivering` and `Closed`, by
  `launchTime`, as counted. Delisted perps are included, so survivors are not the only
  cohort. Whether the catalog really holds all delisted perps is check M2.
- **No entry filter.** Every resolved launch is entered. No signal, no price filter, no
  funding filter, no market filter, no size filter. A filter would be a new variant and
  needs a new registration. This is what keeps the protocol testable: it is a calendar
  event, not a pattern.
- **What "new" means.** A new **perp** is not always a new **token**. Many launches are
  of assets that have traded on spot for months. The late-buyer and unlock-seller
  mechanism fits a new token best. The cohort is therefore recorded with the base's
  prior Bybit spot history (check M3), and token newness is a **pre-declared diagnostic
  stratum that gates nothing** (my position; see question Q3).
- **Pre-market.** A perp may trade before its regular launch, in a pre-market phase.
  Whether `launchTime` marks that start or the regular launch is the core of check M1.
- **Counts that matter.** The 263 is an upper bound. Plan with about 220 resolved
  episodes after the end cut, identity and coverage losses (a planning number, not a
  measurement).

## 4. Verification methods, before any return (stage 1)

These are **methods with criteria**. None is executed by this document. Each needs the
owner's separate go, a registry row, and, for the holdout, a sealed output (aggregate
counts and hashes only). They read **timestamps, file lists and catalog fields**, not
returns. Exceptions, stated where they apply (M3), are binary flags with a fixed
threshold, never a return.

### M1. What `launchTime` means versus the first public trade

Lifecycle timestamps are distinct and are kept distinct: instrument creation, announced
listing, `launchTime` (catalog field), first public trade, first 1-minute bar with
volume, first usable order book, suspension, resumption, delivery or delisting. A
conveniently named field is not evidence that trading opened then.

- **Method.** For each launch in the discovery year, take the first public trade
  timestamp from the Bybit trade archive (the first file of the symbol and its first
  row) and the first 1-minute bar with non-zero volume. Compute `d = first_trade -
launchTime`. Read the catalog's pre-launch indicators if the endpoint returns them for
  closed instruments, and record when it does not.
- **Classes of `d`.**
  - `aligned`: first trade within [-5 min, +60 min] of `launchTime`;
  - `delayed_open`: first trade later than +60 min and up to +24 h;
  - `premarket_or_ambiguous`: first trade earlier than -5 min, or later than +24 h.
- **Pass.** `premarket_or_ambiguous` is at most 10% of the launches counted in the
  window, and `aligned` plus `delayed_open` is at least 90%.
- **Fail code.** `not_reconstructible` with reason `launch_time_unverified`.
- **Consequence for the clock.** The age at entry is counted from **T0 = the first public
  trade**, which is known at the entry time (point in time, past by day 3). `launchTime`
  is kept as the catalog reference and as the cohort key. An episode in
  `premarket_or_ambiguous` is `t0_ambiguous`: it stays in the denominator as unresolved,
  with its reason, and is never silently dropped or re-anchored after seeing a price.

### M2. Reconciliation of the instruments-info catalog against the public trade archive

The catalyst study states that the archive was not reconciled. This check does it.

- **Method.** For the window, build two sets by symbol: **catalog** (launches by
  `launchTime`) and **archive** (symbols whose first daily file falls in the window,
  from file listings, no content). Compare by exact symbol and by date. Also record, per
  catalog symbol, the number of days in its span with no archive file.
- **Pass.**
  - catalog symbols with no archive file within 1 day of `launchTime` are at most 5% of
    the catalog set;
  - archive symbols that are absent from the catalog (typically delisted symbols that
    `Closed` no longer returns) are at most 5% of the union;
  - symbols missing more than 5% of the days of their path are at most 5%.
- **Fail code.** `incomplete_coverage`, with the three shares named. An archive-only
  symbol is **never** added to the cohort by hand after the check: the cohort is the
  catalog as registered. The share is reported because delisted perps are the likely
  best outcomes of a short, so their absence biases the result **against** a short, in
  a way that is visible but not repairable here.
- **Known unknown.** The trade archive held 1,130 USDT symbols on the 2026-10-30 check,
  against 715 distinct catalog symbols. The gap is expected to come from symbols
  launched after 2025-09-30, which the fetch dropped by design, and from dated futures;
  it is not verified. M2 is exactly where it gets resolved, inside the window only.

### M3. Identity per launch

Under the [identity cases](instrument-identity-cases.md), a same-ticker match is not
proof of the same asset. Each launch receives one status:

| Status                   | Meaning                                                                                                                                              |
| ------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| `resolved`               | one catalog symbol, one continuous archive path, no earlier Bybit linear perp of the same base, no redenomination flag                               |
| `relist`                 | the same base had an earlier Bybit linear USDT perp (any time, including before 2023); the launch is not new                                         |
| `multiplier_ambiguous`   | two symbols map to one base (for example a `1000` prefix and a plain ticker), or a redenomination between them                                       |
| `ticker_reuse_suspected` | the archive path has a gap longer than 30 days followed by a new start, or a catalog row with a `launchTime` months later than the first file        |
| `redenomination_flag`    | the 1-minute close ratio between consecutive bars falls outside [1/8, 8] after the first hour. This is a **binary flag only**; no return is computed |

- **Pass.** `resolved` is at least 90% of the window's launches that survive M1 and M2,
  and at least 100 episodes remain after all removals.
- **Fail code.** `identity_unresolved`.
- **Per-launch decision.** Only `resolved` launches are traded in the primary read.
  Every other status is reported by count and reason. `relist` is not a new perp: it is
  excluded from the primary cohort and counted.
- **Diagnostic recorded, gates nothing.** For each resolved base, the Bybit spot
  `launchTime` if any and the age of the spot market at the perp launch. This is the
  token-newness stratum of section 3.

### M4 to M6. Coverage of what the net needs (also before any return)

| Check | Method                                                                                                                                                                                                                            | Pass                                                                                 | Fail code             |
| ----- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------ | --------------------- |
| M4    | Funding history coverage proven by pagination for the entry-to-exit span of every episode (the HYP-036 rule: a full page covers only from its oldest row; a hold not inside one contiguous union of covered spans is incomplete). | at least 95% of resolved episodes covered                                            | `incomplete_coverage` |
| M5    | 1-minute trade bars, mark bars at settlements and, for the primary variant, an order-book snapshot at entry and at exit, listed by existence (no content).                                                                        | bars for at least 95% of episodes; book at the primary size for at least 70% of them | `incomplete_coverage` |
| M6    | Static capital feasibility: contract minimum order value, quantity step, maximum leverage, risk limit and position cap of each symbol at the time, from the catalog snapshot, for notional USD 50 per slot.                       | the order opens for at least 90% of episodes                                         | `capital_infeasible`  |

The order-book archive's status as Bybit's official source is still to be confirmed
(registry, coverage facts of 2026-10-30). M5 includes that confirmation. If it is not
confirmed, book coverage is zero and the net uses the tape ceiling (section 6) only,
which is stated in the report.

## 5. Trade definition (fixed before any return)

All times UTC. Prices are fractions, results are bps of notional.

### 5.1 Entry age and holding grid

**Three variants, no more.** They differ only in the age at entry; the hold is fixed.
(Why the age and not the hold: the age is what the idea is about; a hold grid would
multiply variants on one path.)

| Variant | Entry age from T0 | Hold   | Role                                                |
| ------- | ----------------- | ------ | --------------------------------------------------- |
| `V1`    | 7 days            | 7 days | **primary. The only variant that can decide**       |
| `V2`    | 3 days            | 7 days | sensitivity: earlier, launch-day noise partly gone  |
| `V3`    | 14 days           | 7 days | sensitivity: later, more of the unlock selling done |

- **Why 7 and 7.** A week is long enough that a fee of 22 bps plus spread is a small part
  of a plausible drift; it is short enough that the squeeze exposure and the overlap
  between episodes stay bounded. The first days (extreme spreads, pre-market conversion,
  launch-day volatility) are skipped on purpose. The values are chosen by mechanism and
  by cost, not by any price. The primary is the middle of the grid so that no end of the
  grid can be picked after the read.
- **V2 and V3 do not decide.** They are reported to show the shape across age. A pass
  of V2 or V3 with a failing V1 is recorded as a hypothesis and decides nothing. They
  are counted in the [discovery ledger](discovery-ledger.md): +3 variants.
- **Excluded variants** (each needs a new registration): any hold other than 7 days;
  filters on the price since launch, on funding level, on spot age or on market state;
  maker entries; scaling in; trailing stops.

### 5.2 Entry and exit

- **Entry:** a taker sell of the whole slot notional at the 1-minute bar that starts at
  `E = T0 + age`. Reference price `P_e` is that bar's open.
- **Exit (time):** a taker buy at the 1-minute bar that ends at `X = E + 7 days`.
  Reference price `P_x` is that bar's close.
- **No other exit** except the rules below. No discretionary exit, no re-entry.

### 5.3 Right-tail rule (short squeeze)

A short has unbounded loss on paper; a new perp can pump several times in hours. The rule
is fixed now and is part of the strategy, not an overlay:

1. **Isolated margin.** Each position holds its own margin, equal to **50% of its
   notional** (2x leverage per position). The loss of one episode cannot exceed that
   margin: if the trade-price high of any 1-minute bar reaches the liquidation
   threshold (entry price x 1.45, a buffer under the 50% margin for the maintenance
   rate), the episode is `liquidated` and its loss is the whole margin, **-5,000 bps of
   notional**, plus fees. The liquidation threshold is a stated approximation from
   trade prices; the mark price is not read for it.
2. **Stop.** If the trade-price high of any 1-minute bar after entry reaches **+30%**
   above the entry reference price `P_e`, the position is closed at the stop. Primary
   fill: that bar's **high** (a ceiling; the stop is a market order into a falling
   book). Sensitivity, reported and gating nothing: fill at `max(stop level, bar open)`.
3. **Status events.** If the instrument leaves `Trading` before `X` (halt, delivery,
   delisting), the position is closed at the last trade price of the archive before the
   status change, marked `status_exit`. A short that cannot be priced is `unresolved`,
   counted in the denominator and never given a return.
4. **Why +30% and 2x.** A 30% stop bounds one episode to about 3,000 bps of notional,
   and a 50% margin leaves a buffer so that the stop fires before the exchange
   liquidates. The numbers are chosen once, by the loss they bound, not by any price.
   Their cost is measured: the diagnostic "no stop, margin cap only" is reported beside
   the primary, so the price of the protection is visible (section 7, component `STOP`).

**Tail at the portfolio level.** Young alts pump together. The sizing below keeps gross
notional at most 1x of reserved capital, so one market-wide squeeze that stops out every
open slot costs at most about 30% of reserved capital, and a market-wide liquidation at
most about 50%. The portfolio report must show the worst simultaneous stop-out in the
data.

### 5.4 Sizing and slots

- **Reserved capital `C`.** All capital held for the strategy, including idle margin and
  slots with no position. It is the denominator of the hurdle (section 6.4).
- **Slots `k`.** Fixed **before any price read**, from the resolved launch times of the
  verified cohort and the V1 timing: the smallest integer such that at least 90% of
  episodes find a free slot, computed on metadata. A planning guess: launches arrive
  about 5 a week and each occupies a slot for a week, so `k` is expected near 8.
- **Slot notional** `N = C / k` (gross leverage at most 1x of `C`); margin per slot is
  `0.5 N`; the remaining capital is an unallocated buffer that still counts in `C`.
- **Order-size grid for costs:** order notional `N` of **USD 50 / 250 / 500 / 1,000**.
  The primary economics is at **USD 50** (the registered start); the other sizes show
  capacity and decide nothing except the scale-up claim (section 6.3).
- **USD 50 one-slot portfolio.** Reported as the program plan's rule 2 requires:
  notional min(USD 45, cash minus USD 5), simultaneous signals ordered by SHA-256 of
  `new-perp-short-v1|symbol|E`, exits before entries. One slot takes about a fifth of the
  launches. Its skips, occupancy, daily PnL, drawdown and losing streak are reported in
  full. It gates nothing.

## 6. Net economics

### 6.1 Per-episode net

For an episode with fill prices `F_e` (sell) and `F_x` (buy back), notional `N`, and the
funding settlements `s` with `E < s < X` (strict, as in HYP-036):

```text
price_pnl        = (F_e - F_x) / F_e
funding_fraction = sum over s of  r_s * mark(s) / F_e        (a short receives when r_s > 0)
net_bps          = 10,000 * (price_pnl + funding_fraction) - fee_bps - borrow_bps
net_usd          = N * net_bps / 10,000
```

- **Funding is actually accrued**, from the settlement history of that symbol, at the
  rate and settlement times of each row, not an assumed rate. New perps change funding
  interval and cap; the schedule is what the history says. `mark(s)` is the open of the
  mark-price bar that starts at `s`. A hold with no settlement inside it gets zero
  funding only if M4 proved its coverage.
- **Borrow and other short costs.** A linear perp short has no borrow fee on Bybit:
  `borrow_bps = 0`, written down as a stated zero with its reason, never an omission.
  The other short-specific costs are modelled where they are real: the stop and
  liquidation rules (5.3), forced exit on a status change (5.3), and the loss of the
  return on reserved capital (6.4). A spot-margin short is a different instrument and
  is not part of this protocol.

### 6.2 Cost model: real execution first, a stated ceiling otherwise

1. **Fees.** Innovation Zone taker fee **11 bps per side, 22 bps a round trip**, as
   assumed in the paper screen, for every episode (a ceiling: it is not known which
   launches sat in the zone, or for how long, on which date). A 5.5 bps per side
   (11 bps round trip) case is a reported sensitivity. The fee schedule is mutable
   external data: it is snapshotted durably with the run, and the owner's own account
   tier is confirmed from the account, not from history. Maker fills are not modelled in
   the primary.
2. **Spread and depth, preferred source: the order book.** For each episode, the
   executable **VWAP** at entry (selling into bids) and at exit (buying from asks) from
   the reconstructed book at order sizes USD 50, 250, 500 and 1,000, with no extra
   half-spread added (it is already in the VWAP). Where the book has less depth than the
   size, that size is `depth_absent` for that episode and is **not extrapolated**.
3. **Fallback, a stated ceiling: the tape.** Where no usable book exists, the fill is the
   **worst trade price inside the minute**: the entry sell fills at the entry bar's
   low, the exit buy at the exit bar's high. This is a deliberately pessimistic bound
   built from trades only (it contains spread, impact and the minute's noise). It is
   never mixed with book fills in one mean: the report keeps the two denominators apart
   (`cost_source = book` and `cost_source = tape_ceiling`) and the decision uses the
   rule in 8.
4. **Fills are not guaranteed.** Market orders in a thin days-old book can move the
   price beyond one VWAP computation; the realized-minus-reference gap is the component
   `EXE` (section 7) and the sensitivity of 1.5x costs (8) is the guard.

### 6.3 Capacity

The net at each order size on episodes where the book has that depth, and the share of
episodes with `depth_absent` per size. A positive net at USD 50 that disappears at USD
250 is a result about USD 50 and says so; it does not support a scale-up. The registered
scaling rule (program plan, section 6) governs size, not this document.

### 6.4 The 5% hurdle on all reserved capital

The owner's reference for money with no trading risk is **5% a year**. The economics is
computed on `C`, not on the notional of open trades:

```text
roc_annual = (sum of net_usd over the window) / C * (365 / window_days)
```

with `window_days` the days of the discovery year that episodes may occupy. Idle slots,
the unallocated buffer and the days between trades count in `C`. A result must exceed
5% a year **and** exceed it with execution costs 1.5 times higher. At about 263 episodes
and `k = 8` the hurdle alone needs only about 15 bps mean net per episode
(`0.05 / (263 / 8 / 10,000)`), far below the statistical bar of section 7. The hurdle
is a second guard, not the binding one. The report also shows the money in dollars for
`C` of USD 50 (one slot), 250, 3,000 and 5,000, so the scale of the prize is visible.

## 7. Dependence, decomposition and power

### 7.1 Why 263 is not 263

New perps arrive about five a week and each path spans two calendar weeks (age plus
hold). Launches in one hot month, or one market wave, share the market's move and the
same funding regime; their returns are positively correlated. Over a year in which alts
fell, a short of anything profits. A naive standard error over 263 episodes understates
the uncertainty, and the effect we look for is small against the dispersion of a young
alt over a week.

### 7.2 Dependence model

- **Cluster:** the ISO week of the launch. All launches of one week are one cluster.
  Primary statistics weight **clusters equally**; episode-weighted means are reported
  beside them.
- **Overlap beyond the cluster:** two episodes whose launches are less than 7 days
  apart overlap in calendar time. The uncertainty is therefore estimated with a
  **moving-block bootstrap over ISO weeks, primary block length 4 weeks** (about one
  market regime), with 10,000 draws and a fixed seed; block lengths 2 and 8 are reported
  as sensitivity and none is picked for its result.
- **Design effect:** `DEFF = (bootstrap variance of the cluster-mean) / (variance under
independence)`. The effective sample size is `n_eff = n_resolved / DEFF`. Both are
  reported with their inputs.
- **Regime view:** the same statistics by half-year and by a market-state split fixed
  now (BTCUSDT's 30-day return at `E`: above or below its median in the window), as
  diagnostics. A result that exists only in one regime is not general.

### 7.3 Market-adjusted return and the four-part decomposition

The primary measure is the cluster-mean **net** (as traded). Beside it, per the program
plan's rule 2, the **market-adjusted net** must stay positive:

```text
net_adj_bps = net_bps + 10,000 * R_bench       (R_bench over the same [E, X]; a hedge that costs nothing)
```

for two benchmarks: BTCUSDT, and the **median of a pool of the 20 most liquid linear
USDT perps** (by trailing 30-day turnover as known before `E`, excluding the instrument
itself; point in time). The pool rule is fixed now, its members are computed at read
time from data older than `E`. Adjustment is a **diagnostic hedge, not a tradable
position**, as in the HYP-036 read. A pooled cross-sectional slope `beta_hat` of
episode return on the benchmark return, over all episodes, is also reported (cluster
bootstrap interval); the primary uses `beta = 1` so that no coefficient is fitted to
the outcome.

Every episode's net is split exactly into five additive parts (the identity is tested):

```text
net = M + S + FND + STOP - EXE - FEE
M    = -10,000 * beta * R_bench                      market (beta = 1 primary)
S    = -10,000 * (R_ref - beta * R_bench)            selection: the idiosyncratic drift of the coin
FND  =  10,000 * funding_fraction                    funding actually accrued
STOP =  10,000 * (price_pnl_ref_with_rules - (-R_ref))  effect of the stop, liquidation and status exits
EXE  =  10,000 * (price_pnl_ref_with_rules - price_pnl)  execution: fills against the reference prices
FEE  =  fee_bps
```

with `R_ref = P_x / P_e - 1` and `price_pnl_ref_with_rules` the price result at reference
prices after the rules of 5.3. A positive result that is all `M`, or all `FND`, is not
a new-perp effect: the report states which part carries the sign. The portfolio report
(slot occupancy, every skip with its reason, daily PnL on `C`, drawdown, losing streaks,
worst simultaneous stop-out) is mandatory next to the per-episode net.

### 7.4 Power plan

- **Statistic:** the cluster-mean net of V1 against zero; one-sided test at 2.5%, power
  80%, so the multiplier is `1.96 + 0.84 = 2.80`.
- **Planning MDE:** `MDE = 2.80 * SD * sqrt(DEFF) / sqrt(n)`. `SD` is the standard
  deviation of one episode's net.
- **What `SD` is.** Unknown. The catalyst contract assumed 300 bps for events of hours;
  a young alt's week is far more dispersed. The grid below uses 1,500, 2,000 and 2,500
  bps as **planning assumptions, not measurements**.
- **Planning table (arithmetic on those assumptions, MDE in bps of net per episode):**

| `n` resolved | `SD` bps | DEFF 1.0 | DEFF 1.5 | DEFF 2.5 | DEFF 4.0 |
| -----------: | -------: | -------: | -------: | -------: | -------: |
|          263 |    1,500 |      259 |      317 |      410 |      518 |
|          263 |    2,000 |      346 |      423 |      546 |      691 |
|          263 |    2,500 |      432 |      529 |      683 |      864 |
|          220 |    1,500 |      283 |      347 |      448 |      567 |
|          220 |    2,000 |      378 |      463 |      597 |      756 |
|          220 |    2,500 |      472 |      578 |      747 |      944 |
|          180 |    2,000 |      418 |      511 |      660 |      835 |

- **Reading the table.** On about 220 episodes an effect below roughly 3% to 6% net per
  weekly trade is not detectable, and it is a **large** effect to find in an open
  market. The study may well end at the power gate. That is an answer, and a cheap one.
- **Calibration before any outcome (stage 3), dispersion only.** In the style of the
  HYP-037 calibration: for each resolved launch the return over `[T0 + 1 d, E)` (the
  six pre-entry days for V1) is a **pre-entry proxy**. Its dispersion gives `SD`
  (inflated by 7/6 as a variance scale, and conservative because volatility decays with
  age), and its week-block bootstrap gives `DEFF`. **No mean, sign or outcome is
  emitted.** The proxy window overlaps V2's hold; this exposure is registered. Requires
  at least 95% defined proxies, at least 100 resolved episodes and at least 26 launch
  weeks, otherwise `not_estimable`.
- **Power gate (fixed now).** The calibrated MDE must be **at most 400 bps** of net per
  episode at the resolved `n`, with `DEFF` from the largest of the block lengths 2, 4 and
  8 (never the favorable one). Above it: stop `insufficient_power`, and **no return is
  read**. At `n = 220` this needs `SD * sqrt(DEFF)` of about 2,100 bps or less. The 400
  bps is a planning limit, chosen as an effect that would be material against costs and
  still believable; it is question Q5.
- **Evidence floor.** At least **100 resolved episodes** (the program default) and at
  least **26 ISO weeks** with a resolved episode. No exception is requested. If fewer
  remain after M1 to M3, the stop is `insufficient_events`, not a smaller test.
- **Guards from the program plan, rule 3:** the result must survive removal of the top 5
  episodes, the top 5 weeks, and hold in both halves of the year.

## 8. Decision rule and stop codes

Checked in this order. The first that applies ends the study at that stage. Stages 1
to 3 read no return.

| Order | Code                       | Condition                                                                                                                                                                                                                                                                  | Stage |
| ----- | -------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----- |
| 1     | `not_reconstructible`      | M1 fails (`launch_time_unverified`), or the lifecycle of a launch cannot be rebuilt from native data for more than 15% of episodes                                                                                                                                         | 1     |
| 2     | `incomplete_coverage`      | M2, M4 or M5 fails                                                                                                                                                                                                                                                         | 1     |
| 3     | `identity_unresolved`      | M3 fails                                                                                                                                                                                                                                                                   | 1     |
| 4     | `insufficient_events`      | fewer than 100 resolved episodes, or fewer than 26 weeks, after the stages above                                                                                                                                                                                           | 1     |
| 5     | `capital_infeasible`       | M6 fails, or the position cannot be opened and held within `C` (minimum order, depth absent at USD 50 for more than 20% of episodes, risk limit)                                                                                                                           | 1     |
| 6     | `insufficient_power`       | calibrated MDE above 400 bps (7.4), returns unread                                                                                                                                                                                                                         | 3     |
| 7     | `negative_net_after_costs` | V1 cluster-mean net is at or below zero after costs, funding and the risk rule, on at least the evidence floor. A mature negative is a **fail** and is not masked by thin diversity                                                                                        | 4     |
| 8     | `positive_not_robust`      | V1 net is above zero, but a guard fails: market-adjusted net not positive for both benchmarks, a top-5 removal or half-year flips the sign, the 90% bound is not above zero, `roc_annual` is not above 5% at 1.0x and 1.5x costs, or the sign comes only from `M` or `FND` | 4     |
| 9     | `lab_positive_hypothesis`  | all of the above hold                                                                                                                                                                                                                                                      | 4     |

- **`lab_positive_hypothesis` is not confirmation.** It means only that the candidate may
  be considered for a frozen exam (section 9). The discovery year is open and about 124
  variants of other families were already read on it; a laboratory finding here has the
  status of a hypothesis.
- **Primary denominator.** If at least 70% of resolved episodes have a book fill at USD 50
  (M5), the decision uses the `book` episodes and reports the `tape_ceiling` set
  separately. Otherwise it uses the tape-ceiling net for all episodes and the verdict
  `negative_net_after_costs` is then labeled "at the ceiling" (a ceiling failure is not
  proof of failure at real cost), while a pass at the ceiling is a strong pass.
- **Lower bound.** The block-bootstrap one-sided lower bound of the V1 cluster-mean net is
  taken at **90%** for the laboratory (stricter than a point estimate, laxer than the
  exam's 97.5%, because the laboratory is explicitly a filter before the exam; question
  Q6).
- **Reasons, not bare zeros.** Every stop reports its counters: launches by status,
  M1 classes, M2 shares, M3 statuses, unresolved episodes by reason, censored launches.
- **One corrective recomputation** is allowed after any first verdict: a proven defect
  against this contract, a regression test that fails on it, the same sealed inputs and
  no new request, both results kept. Changing the strategy, the rule or the sample after
  a result is not a correction.

## 9. Confirmation: where a positive laboratory result could be confirmed

State plainly: **no fresh, powered confirmation window exists for I25 today.** A
positive laboratory result is a hypothesis and would be exactly that until one of the
routes below is opened.

| Route                                       | Status                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| ------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Discovery year                              | open and used for development. Cannot confirm.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| Backward holdout, 2023-02-01 to 2024-09-15  | **The only route inside the program clock.** 254 launches over 74 weeks were counted, about the size of the discovery year. It is one shared single shot for the whole program (plan, stage 6): I25 does not spend it alone. It would be spent as one **frozen batch** (I25 alone or with other candidates, with a correction for the number of candidates), after the stage 4 result, with parameters unchanged. Its checks M1 to M6 must pass on it first, run sealed at coverage level. 2023 launches also carry the larger identity risk (ticker reuse) and the shakier catalog of delisted perps. Memory of famous launches in that period must not shape the rule: the rule is already frozen here. |
| HYP-031 test year, 2025-10-01 to 2026-09-28 | excluded and unassigned. It is a possible second window **only** if the owner and the colleague reassign it, and I25's launches in it are **not counted**: the fetch dropped instruments launched from 2025-10-01 by design. Counting them is a new metadata read (instruments launched 2025-10-01 to 2026-09-28 only, never from 2026-09-29), needing a registry row and the owner's go. Its prices were partly exposed to HYP-031's other mechanism; this must be assessed before assigning.                                                                                                                                                                                                            |
| Forward data after HYP-012 v2 is terminal   | renewable but late. v2's terminal deadline is 2027-03-31 and every price read from 2026-09-29 is blind until then. At about 263 launches a year, one forward quarter holds about 66 launches, **below the 100-episode floor**; two quarters hold about 130. The program stop rule is 2027-02-11. So a forward confirmation does not fit the program clock. Recording launch **metadata only** during the blind window would need a formal R1 amendment at the 2026-11-30 checkpoint.                                                                                                                                                                                                                      |

What would have to happen for a confirmed result:

1. stage 1 to 3 pass on the discovery year, then stage 4 reaches `lab_positive_hypothesis`;
2. the holdout's M1 to M6 pass, sealed;
3. the owner and the colleague freeze the exam batch (members, parameters, correction)
   and give the go for the single read;
4. the exam passes the criteria of 8 with a **97.5%** lower bound above zero and the
   guards, on a power plan computed from the holdout's own calibrated `SD` and `DEFF`;
5. only then does the program plan's paper and LIVE_PROBE path open, with its own safety
   list (O10): isolated margin and stop orders as exchange orders, restart-safe
   reconciliation, deterministic `clientOrderId`, a squeeze circuit breaker and kill
   switch. A paper implementation alone does not make a short with an unbounded tail safe.

If the power gate or the checks fail, the plain statement is that **I25 has no
confirmable form on the data the program may use**, and the record says which stop code
and what would be needed: a longer or additional window, or a second venue's launches.

## 10. Data, budget and exposure (planning only, frozen later)

This section registers nothing. The numbers are **planning estimates**; caps are frozen
in stage 2, in a request list with a hash, before any request, as for HYP-036.

| Stage | Data                                                                                                                                                                                                                                                                                        | Planning size                                                                      |
| ----- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------- |
| 1     | trade-archive file listings and the first daily file per launch; catalog fields; Bybit spot catalog; existence lists for bars, funding, books                                                                                                                                               | on the order of 1,500 requests and a few GiB; holdout output sealed                |
| 3     | 1-minute trade bars for the pre-entry proxy window                                                                                                                                                                                                                                          | about 263 symbols x 1 request                                                      |
| 4     | 1-minute trade bars days 3 to 21 (about 26 requests per symbol); hourly mark bars (1 per symbol); funding history (about 1 to 3 pages per symbol); liquid-pool hourly bars (about 400 requests); order-book snapshots for V1 entry and exit (up to about 526 symbol-days, each 1 to 460 MB) | about 7,000 to 8,000 API requests and a book-archive volume that needs its own cap |

**Exposure for the seen-windows registry** (row added with this document; it reads
nothing):

- stage 1: **coverage** level, discovery year and sealed holdout (timestamps, file lists,
  catalog fields; plus the binary redenomination flag of M3, which reads close ratios
  but computes no return);
- stage 3: **features** level (pre-entry return dispersion and dependence, no mean,
  discovery year only);
- stage 4: **outcomes** level for the discovery year only, on the cohort of resolved
  launches and their paths ending by 2025-09-30. After that read those paths count as
  read at outcome level for this trigger;
- holdout: coverage only until the frozen exam; the exam read, if it happens, is its
  single outcome read;
- never read: any price from 2025-10-01 (test year) or from 2026-09-29 (blind window).

## 11. Stages and what needs the owner's separate go

| Stage | What                                                                                               | Gate                                             |
| ----- | -------------------------------------------------------------------------------------------------- | ------------------------------------------------ |
| 0     | this document, draft PR                                                                            | colleague's design discussion                    |
| 1     | M1 to M6 checks on the discovery year and, sealed, the holdout                                     | owner's go per fetch, registry row, request list |
| 2     | contract freeze: JSON contract, request list with hash, code with synthetic tests, registry update | colleague review, owner's go for the fetch       |
| 3     | power calibration (dispersion only)                                                                | owner's go for the price read                    |
| 4     | laboratory read of V1, V2, V3 and the portfolio report                                             | owner's go for the read of returns               |
| 5     | decision on a frozen exam batch                                                                    | owner and colleague                              |

**Separate go of the owner is required for each of:**

1. any fetch, including trade-archive listings and the first-file downloads of M1 to M3;
2. any price, funding, mark-price or order-book read, including the stage 3 calibration;
3. the laboratory read of returns (stage 4);
4. any read of the backward holdout beyond sealed coverage, and the exam itself;
5. any assignment of the HYP-031 test year and the metadata count of launches in it;
6. any recording of launch metadata inside the HYP-012 v2 blind window (needs an R1
   amendment first);
7. confirming the order-book archive as an official source and committing its volume cap;
8. confirming the owner's own account fee tier (Innovation Zone) for the live case;
9. the merge of this protocol, and of every later PR in the chain;
10. any paper, LIVE_PROBE or order path.

## 12. Selection history carried forward

- The catalog count read **no price**. The idea came from the A-screen and the blind
  ideas round; its mechanism is public knowledge.
- The 2026-11-11 exploratory series read about 124 variants of other families on this
  discovery year, among them candles of many of these coins. The cohort's prices are
  therefore not fresh and a laboratory result is a hypothesis. The ledger count rises by
  3 for V1 to V3 at stage 4, and by 0 at stages 1 to 3.
- A short of a post-hype asset partly mirrors known results: HYP-012b's source-lead
  at-signal entries lost after costs, and HYP-036's short at a 4 h hold ended at -42.80
  bps net. This protocol differs in horizon (a week, not minutes to hours), in costs
  relative to the drift, and in being a calendar event with no signal. It is not shown
  to differ in sign.

## 13. Open design questions for the colleague

My position is given for each; the decision is for the discussion.

1. **Q1. Age grid.** V1 at 7 days, V2 at 3, V3 at 14, hold fixed at 7 days. Is a fixed
   hold right, or should the primary be a longer hold (14 days) given funding and cost
   wall? My position: 7 and 7; a longer hold raises overlap and squeeze exposure.
2. **Q2. Right-tail rule.** Isolated margin at 50% of notional, stop at +30% of entry
   with the **bar high** as the primary fill, liquidation at +45%. Too conservative or
   not enough? Should a no-stop variant be a counted variant instead of a diagnostic?
3. **Q3. Eligibility.** All first perp launches (my position) or only new tokens (spot
   history younger than a fixed age)? The second fits the mechanism better but is
   chosen from information I have not looked at and reduces `n` below the floor if
   spot ages are old. I propose the stratum as a diagnostic that gates nothing.
4. **Q4. Clock anchor.** T0 = first public trade (my position) against `launchTime`.
   How should pre-market perps be treated: excluded as `t0_ambiguous` (my position) or
   given their own stratum with a regular-launch anchor?
5. **Q5. Power gate.** MDE of at most 400 bps per weekly episode, `DEFF` from the largest
   of three block lengths. Is the limit right, and is a pre-entry proxy calibration
   acceptable for a return with different volatility after day 7?
6. **Q6. Laboratory strictness.** A 90% block-bootstrap lower bound above zero plus
   guards, against a point-estimate sign rule as in HYP-036. A stricter laboratory
   protects the single exam shot; a laxer one risks wasting it.
7. **Q7. Path cut at the window end.** Censoring launches whose path would leave the
   window (about 5 to 12 launches) to keep the test year unread. Agree, or allow
   tails into the test year?
8. **Q8. Confirmation route.** Is the shared holdout acceptable as I25's only route? Is
   the owner and colleague's view that the test year could be reassigned to I25, with a
   new metadata count, or kept closed? Is a two-quarter forward cohort after v2
   terminal worth planning now, given it falls after the 2027-02-11 gate?
9. **Q9. Costs.** The Innovation Zone fee of 11 bps a side as an assumed ceiling, and
   the bar-extreme tape ceiling when the book is missing: a better fallback available
   (for example a fixed 30 or 50 bps round trip as in the other studies, shown beside it)?
10. **Q10. Market adjustment.** `beta = 1` against BTCUSDT and the median of 20 liquid
    perps. Is a pool of 20 right, and is the pooled `beta_hat` enough as a check?
11. **Q11. Staging.** Stages 1 and 3 each ending the study cheaply on their own stop
    codes. Is it better to run stage 1 for the discovery year only first and the sealed
    holdout checks only after a pass?

## 14. Not in this document

- any fetch, price, return, spread, depth, funding or open-interest value;
- any implementation, contract JSON or request list (stage 2);
- a second venue, Hyperliquid launches (`I22`) or a cross-venue leg;
- spot-margin shorts, options, or any instrument other than Bybit linear USDT
  perpetuals as the only execution venue;
- any order, paper run or LIVE_PROBE.
