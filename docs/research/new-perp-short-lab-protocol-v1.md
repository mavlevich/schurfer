# New-perp short, laboratory protocol v1 (DRAFT, revision 2, 2026-11-16)

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

**Revision 2** (after the colleague's review of `80802d3`) fixes five defects of the
experiment model and adds staging; the list is in section 13.1.

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

| Window                                              | Dates (UTC)              | Role here                                                                                                                                                                                                                  |
| --------------------------------------------------- | ------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Discovery year                                      | 2024-10-01 to 2025-09-30 | open, used for development; the laboratory. A result here is a hypothesis for confirmation, never evidence by itself                                                                                                       |
| Backward holdout                                    | 2023-02-01 to 2024-09-15 | one shared single shot (section 9). In this protocol it is read only at **coverage level**, only by stage 1b, after stage 1 passes and on a separate go; its ticker list stays sealed outside Git as in the catalyst study |
| HYP-031 test year                                   | 2025-10-01 to 2026-09-28 | excluded and unassigned. Not used here without a separate reassignment (section 9)                                                                                                                                         |
| HYP-012 v2 blind window                             | from 2026-09-29          | closed to every price read. Nothing here touches it                                                                                                                                                                        |
| Anything else (before 2023-02-01, 2024-09-16 to 30) | -                        | discarded                                                                                                                                                                                                                  |

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
- **No outcome-dependent exclusion.** Inclusion is decided from information at or
  before `E` and from non-price identity evidence (section 4). Nothing the price does
  after `E` removes an episode: a halt, a delisting, a squeeze or a crash is an outcome.
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

These are **methods with criteria**. None is executed by this document. **Stage 1
runs on the discovery year only**, needs the owner's separate go and a registry row, and
is a data-recoverability check: launch semantics, coverage, and identity by independent
evidence. The backward holdout is touched only by **stage 1b**, after stage 1 passes and
on a further separate go, with a sealed output (aggregate counts and hashes). The checks
read **timestamps, file lists, catalog fields and announcements**, never returns.

**Principle: no outcome-dependent exclusion.** Whether an episode is in the sample is
decided only from information dated at or before its entry time `E`, plus non-price
identity evidence that is **also dated at or before `E`**. Nothing the price did after
`E` may remove an episode, and neither may non-price information that becomes known
after `E`: an announcement on day 10 about a trade opened on day 7 describes an
already open position. Such an event is scored in the result (section 5.3), not deleted. A jump, a crash
or a squeeze after entry is exactly what the strategy must survive, so a price move
alone never removes a trade. A halt or a delisting after `E` is an outcome of the
episode, handled in section 5.3, not an exclusion.

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
  - symbols missing more than 5% of the days of their planned path are at most 5% of the
    catalog set. A missing last file is **not** taken as an end of trading: the days
    after it are a data gap unless a separate source confirms an end (5.3, item 4).
- **Fail code.** `incomplete_coverage`, with the three shares named. An archive-only
  symbol is **never** added to the cohort by hand after the check: the cohort is the
  catalog as registered. The share is reported because delisted perps are the likely
  best outcomes of a short, so their absence biases the result **against** a short, in
  a way that is visible but not repairable here.
- **Known unknown.** The trade archive held 1,130 USDT symbols on the 2026-10-30 check,
  against 715 distinct catalog symbols. The gap is expected to come from symbols
  launched after 2025-09-30, which the fetch dropped by design, and from dated futures;
  it is not verified. M2 is exactly where it gets resolved, inside the window only.

### M3. Identity per launch, by independent evidence known at entry

Under the [identity cases](instrument-identity-cases.md), a same-ticker match is not
proof of the same asset. Identity is judged **only by evidence that is not a price**:
catalog fields (symbol, base coin, contract size and quantity filters, where snapshots
exist), Bybit announcements with a machine publication time, the trade-archive file
list, and the Bybit spot catalog. A price series is never evidence of identity.

**Timing rule.** Evidence counts only if it was **published or dated at or before
`E`**, the entry time of the episode (known-at-entry evidence). Evidence that appears
after `E` is never used to exclude the episode; it is an event of an open position and
is scored (section 5.3, item 4). Each launch receives one status:

| Status                   | Meaning (evidence known at `E`)                                                                                                                                   |
| ------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `resolved`               | one catalog symbol for the base at `E`, no earlier Bybit linear perp of the same base, no contradicting evidence                                                  |
| `relist`                 | the same base had an earlier Bybit linear USDT perp (any time, including before 2023); the launch is not new                                                      |
| `multiplier_ambiguous`   | two catalog symbols of one base existed at `E` (for example a `1000` prefix and a plain ticker)                                                                   |
| `ticker_reuse_suspected` | a catalog row whose `launchTime` is months later than the symbol's first archive file, or a file-list gap longer than 30 days before the launch                   |
| `change_known_at_entry`  | a machine-timed announcement **published at or before `E`**, or a catalog field at `E`, of a symbol, contract-size or multiplier change effective inside the path |
| `identity_unverifiable`  | no independent source can speak for this launch (for example its announcements are untimed). **The episode stays in the sample** and is counted                   |

- **Pass.** `identity_unverifiable` is at most 20% of the launches that survive M1 and
  M2, the four contradicting statuses (`relist`, `multiplier_ambiguous`,
  `ticker_reuse_suspected`, `change_known_at_entry`) together are at most 10%, and at
  least 100 episodes remain.
- **Fail code.** `identity_unresolved`.
- **Per-launch decision.** `resolved` and `identity_unverifiable` launches are traded in
  the primary read. The four contradicting statuses are excluded from the primary
  cohort **before entry**, on evidence available at `E`, and counted by status.
  `relist` is not a new perp.
- **After `E`: events, not exclusions.** A contract conversion, symbol migration,
  multiplier change, halt or delisting that becomes known after `E` is an
  **instrument event after entry**. The episode stays in the sample and is scored by
  the scenario rule of 5.3, item 4. It is never deleted afterwards.
- **Known limit.** Catalog fields exist as a current snapshot, and the announcement feed
  that carries contract changes (`product_updates`) has no machine publication time. A
  change that no source dates stays in the sample as an ordinary episode. This is
  accepted as a stated limit and is not repaired from prices.
- **Price diagnostic, outcome-level, gates nothing.** At stage 4 only, the report lists
  episodes whose 1-minute close ratio between consecutive bars falls outside [1/8, 8]
  after the first hour, with their share and their contribution to the result. It is
  labelled **outcome-level** (it reads prices), prompts a later evidence search, and
  is **never used to exclude an episode from the result**.
- **Diagnostic recorded, gates nothing.** For each resolved base, the Bybit spot
  `launchTime` if any and the age of the spot market at the perp launch. This is the
  token-newness stratum of section 3.

### M4 to M6. Coverage of what the net needs (also before any return)

| Check | Method                                                                                                                                                                                                                                                                                                        | Pass                                                   | Fail code             |
| ----- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------ | --------------------- |
| M4    | Funding history coverage proven by pagination for the entry-to-planned-exit span of every episode (the HYP-036 rule: a full page covers only from its oldest row; a hold not inside one contiguous union of covered spans is incomplete).                                                                     | at least 95% of resolved episodes covered              | `incomplete_coverage` |
| M5    | 1-minute trade bars, 1-minute mark-price bars and hourly mark bars at settlements, listed by existence (no content), over the planned path. A missing last file is **not** an end of trading: bars missing after the last archive file are a data gap unless a separate source confirms an end (5.3, item 4). | at least 95% of episodes have their bars and mark bars | `incomplete_coverage` |
| M6    | Static capital feasibility known at `E`: contract minimum order value, quantity step, maximum leverage, risk limit and position cap of the symbol, from the catalog snapshot, for notional USD 50 per slot.                                                                                                   | the order opens for at least 90% of episodes           | `capital_infeasible`  |

**Order books are not a gate.** Existence and depth of the order-book archive are
**recorded, not required**: the share of episodes with a book at entry and at exit, at
each order size, is reported. It decides only which cost source scenario L uses for
each fill (section 6.2); it does not stop the study. The archive's status as Bybit's
official source is still to be confirmed (registry, coverage facts of 2026-10-30), and
that confirmation is part of stage 1. If it is not confirmed, book coverage counts as
zero and scenario L uses its fixed allowance everywhere, which the report states.

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

- **Why 7 and 7.** A week is long enough that fees of about 20 to 25 bps plus spread are a small part
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

### 5.2 Entry, planned exit and the actual exit

- **Entry:** a taker sell at the 1-minute bar that starts at `E = T0 + age`. The
  quantity `q = N / F_e` is fixed at entry, rounded down to the contract's quantity
  step (checked in M6). Reference price `P_e` is that bar's open.
- **Planned exit:** a taker buy of the same `q` at the 1-minute bar that ends at
  `X = E + 7 days`. Reference price `P_x` is that bar's close.
- **Actual exit.** The position ends at the first of: the planned exit, a stop, a
  liquidation, or an event after entry (5.3). Its time is `t_exit`: `X` for the planned exit,
  and the **end of the triggering bar** (`bar start + 1 minute`) for the other three.
  **Everything downstream is tied to `actual_exit`**: the exit fill and its fee (on
  the value of `q` at that fill), the funding settlements counted (`E < s < t_exit`,
  strict, as in HYP-036), the benchmark interval `[E, t_exit]` of section 7.3, the
  release of the slot, and the capital-time used in section 6.4.
- **No other exit.** No discretionary exit, no re-entry.

### 5.3 Right-tail rule (short squeeze)

A short has unbounded loss on paper; a new perp can pump several times in hours. The rule
is fixed now and is part of the strategy, not an overlay. **No loss ceiling is promised**:
the rule defines what the model does, and the report measures the loss distribution.

**What Bybit's primary pages say (read in the browser pane on 2026-11-16).**

- [The New Margin Calculation](https://www.bybit.com/en/help-center/article/Understanding-the-Adjustment-and-Impact-of-the-New-Margin-Calculation)
  (last updated 2026-06-18). From 2025-09-02 the margin algorithm changes for perpetual
  and expiry contracts, **rolled out gradually** (the page gives no per-account date).
  For isolated margin, the maintenance margin uses the **mark price** and the initial
  margin keeps the entry price; the risk-limit tier then moves **dynamically** with the
  mark price. The short liquidation price **before** the change is
  `E + (IM - MM) / q + extra margin / q`; **after** it, for a USDT contract,
  `[E*q + E*q/lev + extra/(1+taker) + MM deduction] / [q + q*MMR]`. With no extra
  margin and no deduction this is `E * (1 + 1/lev) / (1 + MMR)`: at 2x and an `MMR` of
  0.5% about `1.4925 * E` after the change and about `1.495 * E` before it (the
  fee terms cancel in the old formula). The maintenance rate sits in the
  **denominator**, as the reviewer stated, and my earlier formula was not this.
- [Insurance Fund](https://www.bybit.com/en/help-center/article/Insurance-Fund) (last
  updated 2026-03-24). A liquidation is triggered when the **mark price** reaches the
  liquidation price, and the position is **always settled at the bankruptcy price**, the
  level where no initial margin is left, whatever the market price. A better market
  execution leaves the surplus with the insurance fund, a worse one is covered by it,
  and if the fund runs out the loss goes to auto-deleveraging.
- Two older pages, "Liquidation Price, USDT Contract" and the isolated-mode page for the
  unified account, answered "not supported on this site" and are **not cited**. The
  partial-liquidation step I described earlier was not in the pages read and is **not
  modelled**.

**Consequence.** Liquidation here is a **scenario model**, not a reproduction of the
exchange's engine. Reasons: the formula changed during the discovery year and the
rollout date is unknown, so no single historical formula applies to every episode (the
holdout is wholly under the old logic); the maintenance rate and deduction are
symbol-and-tier specific and the tier now moves with the mark price; and the initial
margin also reserves a closing-fee amount that the 50% simplification ignores.

**The model.**

1. **Isolated margin and the liquidation scenario.** Each position holds its own
   margin, **50% of its notional** (2x leverage per position). The liquidation trigger
   is a **scenario parameter** `T_liq`, applied to the **1-minute mark-price high**:
   primary `T_liq = 1.49 * F_e` (between the two formulas at an `MMR` of 0.5%), and a
   sensitivity `1.46 * F_e` (an `MMR` of 2.5%, plausible for a days-old perp). The
   stage 2 contract records each symbol's maintenance rate and deduction as far as the
   data show them; the scenario value is not claimed to be what the exchange computed.
   Modelled result: the position settles at the bankruptcy price, so the **whole
   position margin is lost** (`-5,000 bps` of entry notional before funding and
   fees), whatever the fill. No separate liquidation fee is assumed; none is stated on
   the pages read.
2. **Stop.** A stop-market order on the **last trade price**, triggered when a 1-minute
   trade-price high reaches `S = P_e * 1.30`, from the entry bar onward. The fill is a
   **scenario assumption**, not a bound (6.2): scenario L fills at `max(S, bar open)`
   (gap aware), scenario H at the bar's **high**. A stop can lose more than 30%.
3. **Order inside one bar.** One OHLC bar does not say what came first. Fixed
   conventions, the same in both scenarios:
   - if the same bar reaches both the stop trigger and the liquidation trigger, the
     **liquidation applies** (the worse outcome for the short);
   - if a bar **opens** at or beyond the stop level (a gap), the stop triggers at that
     bar, and the fill is `bar open` in L and `bar high` in H;
   - if a bar's mark price opens at or beyond `T_liq`, liquidation applies at once.
4. **Events after entry.** A halt, delivery, delisting, contract conversion or symbol
   migration after `E` is an **event of an open position**, not an exclusion.
   - **Confirmation.** The end of the archive is **not** the end of trading: a missing
     last file may mean an incomplete archive. An instrument event is established only
     by a **separate source**: the instrument's status in `instruments-info` (for
     example `Delivering` or `Closed` with a delivery time) or a machine-timed Bybit
     announcement. Without confirmation it is a **data gap**: the episode is
     `unresolved` (item 5), counts against the 95% bars coverage of M5 and the 5% gate
     below, and is never called a status exit.
   - **Scoring of a confirmed event.** The last trade before a halt is **not an
     executable fill** and is never assumed to be one. The episode stays in the sample
     and is scored in two stated scenarios, **L:** the last trade price before the
     event (labelled non-executable) and **H:** the whole margin lost. A recorded
     delivery price is reported beside them. Their share is reported by event type.
5. **Unresolved after `E`.** An episode whose bars are missing after `E` without a
   confirmed event is `unresolved`. It is counted in the denominator, never dropped:
   **L** excludes it from the mean and **H** gives it the whole margin lost. Their share
   must be at most 5%, else `incomplete_coverage`.
6. **Why +30% and 2x.** A 30% stop limits the typical loss of a squeeze and a 50%
   margin leaves a buffer so that the stop is expected to fire before the exchange
   liquidates. The values are chosen once, by that purpose and not by any price, and
   **they do not bound the loss**: gaps, fills beyond the stop and liquidation
   mechanics can cost more than 30%. The price of the protection is measured by a
   diagnostic "no stop, liquidation only" beside the primary (section 7, component
   `STOP`).
7. **Reported, not capped.** The report gives the distribution of per-episode loss
   beyond 30%, the number of stops, liquidations and events after entry, and **the worst
   simultaneous stop-out observed**, with no hard cap assumed.

**Tail at the portfolio level.** Young alts pump together. Sizing keeps gross notional at
most 1x of reserved capital, which is a sizing rule and **not a loss ceiling**: stop fills
beyond +30% and gaps can make the loss of a market-wide squeeze larger than 30% of
reserved capital. The portfolio report shows the worst case that occurred.

### 5.4 Sizing and slots

- **Reserved capital `C`.** All capital held for the strategy, including idle margin and
  slots with no position. It is the denominator of the hurdle (section 6.4).
- **Slots `k`.** Fixed **before any price read**, from the resolved launch times of the
  verified cohort and the V1 timing: the smallest integer such that at least 90% of
  episodes find a free slot, computed on metadata. A planning guess: launches arrive
  about 5 a week and each occupies a slot for a week, so `k` is expected near 8.
- **Slot notional** `N = C / k` (gross leverage at most 1x of `C`); margin per slot is
  `0.5 N`; the remaining capital is an unallocated buffer that still counts in `C`. A
  slot is released at `actual_exit`.
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

For an episode with entry fill `F_e` (sell), exit fill `F_x` (buy back at the actual
exit), quantity `q = N / F_e` fixed at entry, the settlements `s` with
`E < s < t_exit` (strict, as in HYP-036), and a taker fee rate `phi` per side:

```text
price_usd   = q * (F_e - F_x)
funding_usd = sum over s of  r_s * q * mark(s)        (a short receives when r_s > 0)
fee_usd     = phi * q * F_e  +  phi * q * F_x         (each fill is charged on its own value)
net_usd     = price_usd + funding_usd - fee_usd - borrow_usd
net_bps     = 10,000 * net_usd / (q * F_e)            (bps of the entry notional)
```

- **The fee is charged on each fill's value, not as a fixed 22 bps.** The exit is charged
  on the value of the position being closed. At `phi = 11 bps` and a 30% price rise
  the round trip costs `11 + 11 * 1.3 = 25.3 bps` of entry notional; after a 30% fall
  it costs `11 + 11 * 0.7 = 18.7 bps`. In bps of entry notional the fee is
  `10,000 * phi * (1 + F_x / F_e)`. The model works in quantity and in the cost of
  each fill, including the exit VWAP for the same `q`.
- **Funding is actually accrued**, from the settlement history of that symbol, at the
  rate and settlement times of each row, not an assumed rate, on the position value
  `q * mark(s)` at each settlement. New perps change funding interval and cap; the
  schedule is what the history says. `mark(s)` is the open of the mark-price bar that
  starts at `s`. A hold with no settlement inside it gets zero funding only if M4
  proved its coverage. Funding stops at `t_exit`.
- **Borrow and other short costs.** A linear perp short has no borrow fee on Bybit:
  `borrow_usd = 0`, written down as a stated zero with its reason, never an omission.
  The other short-specific costs are modelled where they are real: the stop and
  liquidation rules (5.3), the event-after-entry scenarios (5.3), and the loss of the return
  on reserved capital (6.4). A spot-margin short is a different instrument and is not
  part of this protocol.

### 6.2 Cost model and its two scenarios

1. **Fee rate `phi`.** The Innovation Zone non-VIP **taker rate of 0.1100% (11 bps) per
   side** and maker rate of 0.0400% are stated on Bybit's
   [Innovation Zone FAQ](https://www.bybit.com/en/help-center/article/FAQ-Perpetual-Trading-Innovation-Zone)
   (read 2026-11-16, last updated 2026-08-07). The standard non-VIP taker rate of 0.055%
   (5.5 bps) is a reported sensitivity. The same page says a contract can **migrate**
   between the Innovation Zone and the main board in either direction, so which launches
   sat in the zone, and for how long, is not recoverable from history; 11 bps per side
   is an assumed rate for every episode, not a bound. The
   [fee group endpoint](https://bybit-exchange.github.io/docs/v5/market/fee-group-info)
   lists the zone's symbols but returns only the current membership and applies to Pro
   and market-maker clients. The fee schedule is mutable external data: it is
   snapshotted durably with the run, and the owner's own account tier is confirmed from
   the account. Maker fills are not modelled.
2. **Fill prices are scenario assumptions, not bounds.** A market order in a thin
   days-old book can fill worse than any figure computed from candles or a snapshot.
   Two scenarios are fixed, and the report shows both:

| Fill               | Scenario L (book, else allowance)                                                                                                 | Scenario H (bar extremes)  |
| ------------------ | --------------------------------------------------------------------------------------------------------------------------------- | -------------------------- |
| Entry (sell `q`)   | order-book **VWAP** for `q` from the bids at the entry instant, where a book exists; else `P_e` less a fixed allowance of 9.5 bps | the entry bar's **low**    |
| Planned exit (buy) | order-book VWAP for `q` from the asks at the exit instant, where a book exists; else `P_x` plus 9.5 bps                           | the exit bar's **high**    |
| Stop               | `max(S, bar open)`, plus 9.5 bps                                                                                                  | the trigger bar's **high** |
| Liquidation        | whole margin lost                                                                                                                 | whole margin lost          |
| Event after entry  | last trade before the event (non-executable)                                                                                      | whole margin lost          |

- **Neither scenario dominates the other.** They are **independent assumptions**, and
  neither is a bound. In a synthetic case with flat bars at 100 and no book, L sells at
  99.905 and buys back at 100.095, while H sells at 100 and buys back at 100, so H is
  better than L. In a wide bar H is worse. The document therefore never calls one
  "the lower-cost scenario", and section 8 treats a disagreement in sign, in either
  direction, as `scenario_dependent`.
- **Allowance of 9.5 bps per side (19 bps round trip)** is the spread and slippage
  figure used for established perps in the
  [venue cost check](venue-cost-check-v1.md); days-old books are likely worse, so it is
  a floor for L and not an estimate.
- **VWAP** is computed at order sizes USD 50, 250, 500 and 1,000 with no extra
  half-spread added (it is already inside the VWAP). Where the book has less depth
  than the size, that size is `depth_absent` for that fill and is **not extrapolated**.
  The USD 50 primary size takes the fixed allowance where no book exists; a larger size
  with no depth is left out of that size's net and counted.
- **Mixed cost sources.** Scenario L mixes book VWAP episodes and allowance episodes
  in one mean; the report therefore also gives L split by cost source, and the share
  of each. They are never presented as one evidence class.

**How the scenarios decide.** Section 8. Both at or below zero is a fail **under the
assumed costs**; opposite signs, in either direction, are `scenario_dependent`; both above
zero opens the guards. A fail under assumed costs may stop spending, but it is **not
proof that no edge exists**. Any pass is still a hypothesis: real spread and depth must be
measured before any order.

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

with `window_days` the days of the discovery year that episodes may occupy, each
episode occupying its slot until `actual_exit`. Idle slots,
the unallocated buffer and the days between trades count in `C`. A result must exceed
5% a year **in both scenarios**, L and H (section 6.2). At about 263 episodes
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

### 7.3 Market-adjusted return and the five-part decomposition

The primary measure is the cluster-mean **net** (as traded). Beside it, per the program
plan's rule 2, the **market-adjusted net** must stay positive:

```text
net_adj_bps = net_bps + 10,000 * R_bench       (R_bench over [E, t_exit]; a hedge that costs nothing)
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
M    = -10,000 * beta * R_bench                           market (beta = 1 primary)
S    = -10,000 * (R_ref - beta * R_bench)                 selection: the idiosyncratic drift of the coin
FND  =  10,000 * funding_usd / (q * F_e)                  funding actually accrued, to actual_exit
STOP =  10,000 * (pnl_ref_rules - (-R_ref))               effect of the stop, liquidation and event-after-entry rules
EXE  =  10,000 * (pnl_ref_rules - price_usd / (q * F_e))  execution: fills against the reference prices
FEE  =  10,000 * fee_usd / (q * F_e)                      fees on each fill's value
```

with `R_ref = P_x / P_e - 1` (the planned-exit reference return), `R_bench` over the
**actual** holding interval `[E, t_exit]`, and `pnl_ref_rules` the price result at
reference prices after the rules of 5.3 (the stop level `S`, the whole margin for a
liquidation, `P_x` for a planned exit). A positive result that is all `M`, or all
`FND`, is not a new-perp effect: the report states which part carries the sign. The
portfolio report (slot occupancy released at `actual_exit`, every skip with its reason,
daily PnL on `C`, drawdown, losing streaks, worst simultaneous stop-out) is mandatory
next to the per-episode net.

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

| Order | Code                       | Condition                                                                                                                                                                                                                                                             | Stage |
| ----- | -------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----- |
| 1     | `not_reconstructible`      | M1 fails (`launch_time_unverified`), or the lifecycle of a launch cannot be rebuilt from native data for more than 15% of episodes                                                                                                                                    | 1     |
| 2     | `incomplete_coverage`      | M2, M4 or M5 fails (bars, mark bars and funding; **order-book coverage is not a gate**), or more than 5% of episodes are `unresolved` after `E` (an end of archive without a confirming source counts here)                                                           | 1, 4  |
| 3     | `identity_unresolved`      | M3 fails                                                                                                                                                                                                                                                              | 1     |
| 4     | `insufficient_events`      | fewer than 100 resolved episodes, or fewer than 26 weeks, after the stages above                                                                                                                                                                                      | 1     |
| 5     | `capital_infeasible`       | M6 fails, or the order cannot be opened within `C` (minimum order, quantity step, risk limit, or no depth at USD 50 **at entry** for more than 20% of episodes; depth at exit never removes an episode)                                                               | 1     |
| 6     | `insufficient_power`       | calibrated MDE above 400 bps (7.4), returns unread                                                                                                                                                                                                                    | 3     |
| 7     | `negative_net_after_costs` | the V1 cluster-mean net is at or below zero in **both** scenarios L and H, on at least the evidence floor. A mature negative is a **fail under the assumed costs** and is not masked by thin diversity; it is not proof that no edge exists                           | 4     |
| 8     | `scenario_dependent`       | the sign of the V1 cluster-mean net **differs between L and H**, in either direction. Neither a pass nor a fail                                                                                                                                                       | 4     |
| 9     | `positive_not_robust`      | net is above zero in both, but a guard fails in L or in H: market-adjusted net not positive for both benchmarks; a top-5 removal or half-year flips the sign; the 90% bound is not above zero; `roc_annual` is not above 5%; or the sign comes only from `M` or `FND` | 4     |
| 10    | `lab_positive_hypothesis`  | all of the above hold in both L and H                                                                                                                                                                                                                                 | 4     |

- **`lab_positive_hypothesis` is not confirmation.** It means only that the candidate may
  be considered for a frozen exam (section 9). The discovery year is open and about 124
  variants of other families were already read on it; a laboratory finding here has the
  status of a hypothesis. It is not execution-validated: neither scenario is a bound.
- **Two independent assumptions, no hierarchy.** L and H are not ordered by cost, so
  neither is "the optimistic" nor "the conservative" case. An idea is rejected only
  when it fails under both, and accepted for the guards only when it passes under
  both. A disagreement is reported as such, and the report shows which fills (entry,
  exit, stop) drive it. Neither the book-versus-candle choice nor the stop fill may be
  picked after the read.
- **Lower bound.** The block-bootstrap one-sided lower bound of the V1 cluster-mean net is
  taken at **90%** for the laboratory (stricter than a point estimate, laxer than the
  exam's 97.5%, because the laboratory is explicitly a filter before the exam; question
  Q6). It is evaluated in both scenarios.
- **Reasons, not bare zeros.** Every stop reports its counters: launches by status,
  M1 classes, M2 shares, M3 statuses, unresolved episodes by reason, censored launches,
  stops, liquidations and events after entry, book coverage by size.
- **One corrective recomputation** is allowed after any first verdict: a proven defect
  against this contract, a regression test that fails on it, the same sealed inputs and
  no new request, both results kept. Changing the strategy, the rule or the sample after
  a result is not a correction.

## 9. Confirmation: where a positive laboratory result could be confirmed

State plainly: **no fresh, powered confirmation window exists for I25 today.** A
positive laboratory result is a hypothesis and would be exactly that until one of the
routes below is opened.

| Route                                       | Status                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| ------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Discovery year                              | open and used for development. Cannot confirm.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| Backward holdout, 2023-02-01 to 2024-09-15  | **The only route inside the program clock.** 254 launches over 74 weeks were counted, about the size of the discovery year. It is one shared single shot for the whole program (plan, stage 6): I25 does not spend it alone. It would be spent as one **frozen batch** (I25 alone or with other candidates, with a correction for the number of candidates), after the stage 4 result, with parameters unchanged. Its checks M1 to M6 must pass on it first, as stage 1b: run sealed at coverage level, only after stage 1 passes on the discovery year and on the owner's separate go. 2023 launches also carry the larger identity risk (ticker reuse) and the shakier catalog of delisted perps. Memory of famous launches in that period must not shape the rule: the rule is already frozen here. |
| HYP-031 test year, 2025-10-01 to 2026-09-28 | excluded and unassigned. It is a possible second window **only** if the owner and the colleague reassign it, and I25's launches in it are **not counted**: the fetch dropped instruments launched from 2025-10-01 by design. Counting them is a new metadata read (instruments launched 2025-10-01 to 2026-09-28 only, never from 2026-09-29), needing a registry row and the owner's go. Its prices were partly exposed to HYP-031's other mechanism; this must be assessed before assigning.                                                                                                                                                                                                                                                                                                         |
| Forward data after HYP-012 v2 is terminal   | renewable but late. v2's terminal deadline is 2027-03-31 and every price read from 2026-09-29 is blind until then. At about 263 launches a year, one forward quarter holds about 66 launches, **below the 100-episode floor**; two quarters hold about 130. The program stop rule is 2027-02-11. So a forward confirmation does not fit the program clock. Recording launch **metadata only** during the blind window would need a formal R1 amendment at the 2026-11-30 checkpoint.                                                                                                                                                                                                                                                                                                                   |

What would have to happen for a confirmed result:

1. stage 1 to 3 pass on the discovery year, then stage 4 reaches `lab_positive_hypothesis`;
2. stage 1b: the holdout's M1 to M6 pass, sealed, on the owner's separate go;
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

| Stage | Data                                                                                                                                                                                                                                                                                                                                             | Planning size                                                              |
| ----- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------- |
| 1     | discovery year only: trade-archive file listings and the first daily file per launch; catalog fields; machine-timed Bybit announcements already in hand; Bybit spot catalog; existence lists for trade bars, mark bars, funding and books                                                                                                        | on the order of 1,000 requests and a few GiB                               |
| 1b    | backward holdout, after stage 1 passes and on a further go: the same checks, sealed output                                                                                                                                                                                                                                                       | on the order of 1,000 requests; output is aggregate counts and hashes      |
| 3     | 1-minute trade bars for the pre-entry proxy window                                                                                                                                                                                                                                                                                               | about 263 symbols x 1 request                                              |
| 4     | 1-minute trade bars and 1-minute mark-price bars for days 3 to 21 (about 26 requests per symbol each); hourly mark bars at settlements (1 per symbol); funding history (about 1 to 3 pages per symbol); liquid-pool hourly bars (about 400 requests); order-book snapshots for V1 entry and exit (up to about 526 symbol-days, each 1 to 460 MB) | about 14,000 API requests and a book-archive volume that needs its own cap |

**Exposure for the seen-windows registry** (row added with this document; it reads
nothing):

- stage 1: **coverage** level, discovery year only (timestamps, file lists, catalog
  fields, announcements). No price is read;
- stage 1b: **coverage** level, backward holdout, sealed aggregate output, only after
  stage 1 passes and on a separate go;
- stage 3: **features** level (pre-entry return dispersion and dependence, no mean,
  discovery year only);
- stage 4: **outcomes** level for the discovery year only, on the cohort of resolved
  launches and their paths ending by 2025-09-30. After that read those paths count as
  read at outcome level for this trigger. The price-scale diagnostic of M3 belongs to
  this stage and is outcome-level;
- holdout: coverage only until the frozen exam; the exam read, if it happens, is its
  single outcome read;
- never read: any price from 2025-10-01 (test year) or from 2026-09-29 (blind window).

## 11. Stages and what needs the owner's separate go

| Stage | What                                                                                                                                   | Gate                                             |
| ----- | -------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------ |
| 0     | this document, draft PR                                                                                                                | colleague's design discussion                    |
| 1     | **data-recoverability check, discovery year only:** M1 launch semantics, M2 and M4 to M6 coverage, M3 identity by independent evidence | owner's go per fetch, registry row, request list |
| 1b    | the same checks on the backward holdout, sealed                                                                                        | stage 1 passed **and** the owner's separate go   |
| 2     | contract freeze: JSON contract, request list with hash, code with synthetic tests, snapshots of the exchange rules, registry update    | colleague review, owner's go for the fetch       |
| 3     | power calibration (dispersion only)                                                                                                    | owner's go for the price read                    |
| 4     | laboratory read of V1, V2, V3 and the portfolio report                                                                                 | owner's go for the read of returns               |
| 5     | decision on a frozen exam batch                                                                                                        | owner and colleague                              |

**Separate go of the owner is required for each of:**

1. any fetch, including trade-archive listings and the first-file downloads of M1 to M3;
2. any price, funding, mark-price or order-book read, including the stage 3 calibration;
3. the laboratory read of returns (stage 4);
4. **stage 1b**, any read of the backward holdout beyond what stage 1 needs, and the
   exam itself;
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

## 13. Revisions, and open design questions for the colleague

### 13.1 Revision 3: what changed after the second review (`1f43ade`)

Four defects remained after revision 2. Where each is now handled:

1. **Look-ahead survives without prices.** An evidenced change "dated inside the path"
   removed the whole launch. Now (section 4, M3 and the principle at its start;
   section 5.3, item 4): only evidence **published or dated at or before `E`** may
   exclude a launch, before entry (`change_known_at_entry`). A change that becomes known
   after `E` is an **event of an open position** and is scored in scenarios L and H,
   never deleted.
2. **Liquidation formula.** My formula was wrong. I read the primary pages myself
   (section 5.3): in the short formula the maintenance rate sits in the **denominator**,
   `E * (1 + 1/lev) / (1 + MMR)`, about `1.4925 * E` at 2x and 0.5%; Bybit changed the
   margin calculation from 2025-09-02, rolled out gradually with no stated per-account
   date, so no single historical formula applies. Liquidation is now stated plainly as a
   **scenario model** with `T_liq = 1.49 * F_e` primary and `1.46 * F_e` sensitivity on
   the mark-price high, without the words "per the exchange formula"; whole margin lost
   follows from settlement at the bankruptcy price. No loss ceiling is promised.
3. **Scenario H can be cheaper than L.** Right: a flat bar gives H a better fill. The
   scenarios are now **independent assumptions with no ordering** (section 6.2, with the
   synthetic example). Section 8 declares `negative_net_after_costs` only when both are
   at or below zero, `scenario_dependent` when the sign differs in either direction, and
   says a fail under assumed costs is not proof of no edge.
4. **End of archive is not end of trading.** M2's missing-day count no longer treats days
   after the last file as a status exit, M5 says a missing last file is not an end of
   trading, and section 5.3, item 4 requires a **separate source** (instrument status
   or a machine-timed announcement) before any halt or delisting is called an event.
   Without it the episode is `unresolved`, counts against the 95% bars coverage and the
   5% gate, and ends in `incomplete_coverage` when too many.

Also corrected after reading the primary pages: the Innovation Zone fee page was opened
(non-VIP taker 0.1100%, maker 0.0400%, updated 2026-08-07) and says a contract can
migrate between zones in either direction (section 6.2). The earlier statement that
the pages could not be read is replaced by the list of what was read and what was not
(section 5.3).

### 13.2 Revision 2: what changed after the first review (`80802d3`)

1. **Look-ahead selection in M3.** The price-based flag is gone. Identity is judged by
   non-price evidence, and a launch no source can speak for stays in. A price-scale list
   remains only as an outcome-level diagnostic that gates nothing and never excludes.
2. **Loss bound (section 5.3).** The "bounds one episode to about 3,000 bps" claim was
   removed. No loss ceiling is promised; the loss distribution beyond 30% and the worst
   simultaneous stop-out are reported without a cap.
3. **One actual exit (sections 5.2, 6.1).** `actual_exit` drives the funding window, the
   exit fill and fee, the benchmark interval and slot release.
4. **Tape extreme is not a bound (sections 6.2, 8).** `tape_ceiling` is gone; fills are
   scenario assumptions in L and H; order-book coverage is not a gate.
5. **Fee on each fill (section 6.1).** `10,000 * phi * (1 + F_x / F_e)` bps of entry
   notional, so `11 + 11 * 1.3 = 25.3 bps` after a 30% rise.

Staging, as the colleague asked: stage 1 is a data-recoverability check on the
**discovery year only**; the backward holdout is touched only by stage 1b, after stage 1
passes and with the owner's separate go (sections 2, 9, 10 and 11).

### 13.3 Open questions

1. **Q1. Age grid.** V1 at 7 days, V2 at 3, V3 at 14, hold fixed at 7 days. Is a fixed
   hold right, or should the primary be a longer hold (14 days) given funding and cost
   wall? My position: 7 and 7; a longer hold raises overlap and squeeze exposure.
2. **Q2. Right-tail rule (changed).** Isolated margin at 50% of notional, a stop at +30%
   on the last trade price, fills in scenarios L and H, liquidation as a **scenario
   model** (`T_liq` of 1.49 or 1.46 times entry on the mark-price high). Is the stop level right, and should a no-stop variant
   be a counted variant instead of a diagnostic? Should the stop trigger on the mark
   price instead of the last price?
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
9. **Q9. Costs (changed).** Are L (book VWAP, else a fixed 9.5 bps per side) and H (the
   minute's extreme) the right pair of independent assumptions, with `negative_net` only
   when both fail and `scenario_dependent` when the sign differs? Is 9.5 bps per side
   the right floor for a days-old perp? Is 11 bps per side the right Innovation Zone
   assumption, given that zone membership of a historical launch is unknown and a
   contract can migrate?
10. **Q10. Market adjustment.** `beta = 1` against BTCUSDT and the median of 20 liquid
    perps. Is a pool of 20 right, and is the pooled `beta_hat` enough as a check?
11. **Q11. Staging (settled by the review).** Stage 1 on the discovery year only, then
    stage 1b on the holdout after a pass and a separate go. Open only: should stage 3
    (power calibration) wait for stage 1b, or run on the discovery year first?
12. **Q12. Independent identity evidence.** Contract changes sit in a feed with no
    machine time, and catalog fields exist only as a current snapshot. Is
    `identity_unverifiable` staying in the sample (up to 20%) the right treatment, or is
    a larger unverifiable share a stop?
13. **Q13. Events after entry.** The scenario pair for a confirmed halt, delisting or
    conversion (last trade in L, whole margin lost in H), and the rule that an
    unconfirmed end of archive is a data gap. Is a recorded delivery price better as
    the primary where it exists?
14. **Q14. Exchange rules as sources (changed).** The margin-change notice, the Insurance
    Fund page and the Innovation Zone fee page were read in full; two older liquidation
    pages are no longer served. Agree that stage 2 snapshots the pages with a hash, and
    that the liquidation scenario is kept as a scenario model rather than reproduced from
    a formula whose parameters vary by symbol, tier and date?
15. **Q15. Liquidation scenario values.** `T_liq` of 1.49 (primary) and 1.46
    (sensitivity). Is the maintenance rate assumption of 0.5% and 2.5% reasonable, and
    should the stage 2 contract read the symbol's risk-limit tiers where they are
    available?

## 14. Not in this document

- any fetch, price, return, spread, depth, funding or open-interest value;
- any implementation, contract JSON or request list (stage 2);
- a second venue, Hyperliquid launches (`I22`) or a cross-venue leg;
- spot-margin shorts, options, or any instrument other than Bybit linear USDT
  perpetuals as the only execution venue;
- any order, paper run or LIVE_PROBE.
