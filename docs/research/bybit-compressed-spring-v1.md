# HYP-032: compressed spring, sell absorption, long (protocol v1, draft)

Status: **draft for the design touchpoint, revised 2026-11-06 after the first review.**
Nothing is fetched and no price, open interest, funding or trade value of the discovery
window has been read for this question. It registers nothing until committed after
review; any later change needs a dated amendment before any return is read. It is step 3
of the [research strategy v2](research-strategy-v2.md) order ("one pump-prediction
question").

## Question

On Bybit USDT perpetuals, while an instrument's price range is compressed, its open
interest in coins grows and its funding is at or below zero, does net aggressive selling
away from the hour boundaries (the minutes :05 to :55 of each hour) that fails to push
the price down predict a move up that clears costs, on public history?

## Assumed mechanism, stated before any outcome

Funding at or below zero with growing open interest is consistent with new positions
opening while shorts pay, or at least do not receive, carry: a crowded short side. If net
taker selling keeps arriving and the price does not fall, someone absorbs it with passive
bids. When the shorts' stops or the absorber's demand meet a thin offer, the shorts buy
back into it: a squeeze.

This is an assumption, not something the data can establish. Growing open interest and
non-positive funding do not show who opened the positions (a long hedged elsewhere looks
the same), and net selling with a non-negative 6-hour return can also be an early rise
that later fails under that selling. The test asks only whether the configuration
predicts returns beyond the price state.

Competing explanations, which the baselines and the matched control exist to separate:

- **Price state alone.** A quiet range that is not falling may be followed by a move up
  whatever open interest, funding and flow do (matched control, baseline B3).
- **Market beta.** Small caps rise together with BTC (baseline B2, matched control at
  the same t).
- **Liquidity selection.** Compressed, quiet instruments may simply be the ones whose
  next move is large in either direction; a long-only test of a symmetric effect would
  still lose on average after costs.

Buy absorption (buying that fails to lift price) points to a large seller and is not
part of this setup. A large taker-buy breakout out of the range is a separate variant
(V1) and is never pooled with the primary.

## How it differs from closed lines

The strategy requires a stated difference from every closed line on the same inputs.

| Closed line      | Its object                                                                                                                                   | Difference here                                                                                                                                                                         |
| ---------------- | -------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Abnormal-flow v1 | A 60-minute OI-and-taker-flow imbalance with limited price movement, long, fixed 720-minute horizon                                          | The flow is net **selling**, not buying; a compressed range over a day is required; funding at or below zero is required. Long and a 12 h horizon are shared, so they are no difference |
| HYP-031          | Short reaction in the minutes after a settlement with r_t <= -0.30%                                                                          | The trigger is not a settlement: funding enters only as the sign of the last settled rate, as a filter; the hold is hours; HYP-031's reserved tape is masked (Data boundary)            |
| HYP-024          | Whether 10-minute taker imbalance adds information to an already triggered `pump_short_v1_market_quality` decision, short, 60-minute outcome | No pump decision exists here; the flow is part of the trigger, over 6 hours, before any move; long                                                                                      |
| HYP-030          | Long after a closed 1-minute bar of at least +5% on at least 5x turnover (a burst under way)                                                 | Nothing has moved at the decision: the range is compressed and the 6-hour return only needs to be non-negative; hourly grid                                                             |

If discovery shows that the full setup does not beat the price-state matched control
(reading condition 6), the answer is "nothing beyond price", and the line stops even if
the full setup is net positive.

## Data boundary

- Only Bybit public data: V5 market endpoints and the trade archive
  (`public.bybit.com/trading`). No Schurfer capture, cohort or result is read.
- **Discovery: 2024-10-01T00:00Z to 2025-09-30T23:59Z.** Every datum (lookbacks, ranking
  histories, decision, hold, exit bar) lies inside it. The ranking histories (next
  section) make **2024-10-16T00:00Z the first decision hour**; the last is the latest t
  whose exit bar for the chosen H ends by 2025-09-30T23:59Z.
  - 2025-10-01 to 2026-09-28 is not used: it is HYP-031's reserved test year
    ([registry](seen-windows-registry.md)).
  - The holdout 2023-02-01 to 2024-09-15 is read only in the strategy's stage 2 batch,
    never in this protocol's discovery.
- **HYP-031 tape mask: fixed, the minutes around every hour.** The trade tape around
  HYP-031's settlements is reserved for its seconds-level variant, whose draft
  ([seconds protocol](bybit-post-funding-seconds-v1.md), 2026-11-05) reads the tape
  interval [s - 60 s, s + 60 s), rounded out to full minutes, of sampled events
  (r_s <= -0.30%) and of controls settling at the same s. (The [s - 60 min, s + 65 min)
  windows of the minute protocol are 1-minute kline windows, not tape.) Every Bybit
  settlement is on a whole hour, so this study drops, on every instrument and in every
  hour, the minutes [h - 5 min, h + 5 min) around each whole hour h: minute :55 is the
  first dropped, :05 the first kept. The mask does not depend on any rate, so a failed
  fetch cannot open a reserved window, the publication time of a rate cannot change S,
  and the same S can be computed live (where no reservation exists).
  - **What it masks:** only trade tape. No tape minute inside the mask enters any
    feature, rank history or diagnostic other than the sensitivity check below. The
    fetcher streams each daily archive file, records the file's SHA-256 and size,
    drops every trade in a masked minute (counting the dropped trades and their USD,
    nothing more), stores only per-minute aggregates of the kept trades, and discards
    the raw file.
  - **What it does not mask:** klines, open interest, funding rates and mark prices.
    HYP-031's minute discovery already read the klines and rates of its windows; the
    reservation is the tape. A hold is never excluded for overlapping a mask.
  - **Gates before any tape is fetched (power step 2):** (a) every settlement time in
    this study's funding is on a whole hour (power step 1 stops otherwise); (b) every
    event and control window of the frozen HYP-031 discovery index, by its SHA-256,
    rounded out to full minutes, lies inside the mask, or the fetch stops; (c) a unit
    test fixes the boundaries, :55 masked inclusive and :05 kept.
  - **Registry:** the row that reserves the discovery year's whole native tape for
    HYP-031 (on the HYP-031 provenance branch, not yet on main) is narrowed to these
    minutes before the first tape request; the HYP-031 test year stays reserved whole.
- **Registry rows, before the data they cover.** One row (features level: klines, open
  interest, funding, tape aggregates, counts) is added in the same change that adds the
  fetcher, before the first request. A second row, for the power step's unconditional
  return dispersion (step 4), is added before that step runs.
- **Universe:** every USDT-settled `LinearPerpetual` that traded in the window, live and
  delisted (`instruments-info?status=Closed`, with `deliveryTime`), as in HYP-031. Every
  request end is capped at `deliveryTime`.

## Inputs and what each feature depends on

All decisions use only data strictly before t, except the entry and exit bars and the
funding marks inside the hold.

| Item                      | Source                                                | Interval needed for decision t                                                 |
| ------------------------- | ----------------------------------------------------- | ------------------------------------------------------------------------------ |
| Liquidity (24 h turnover) | 1 h perp klines                                       | [t - 24 h, t)                                                                  |
| C and its rank            | 1 h perp klines                                       | value: [t - 24 h, t); rank history: [t - 15 d, t)                              |
| G and its rank            | 1 h open interest in coins                            | value: records stamped t - 1 h and t - 25 h; rank history: from t - 15 d - 1 h |
| Funding sign              | `/v5/market/funding/history`                          | the last settlement strictly before t                                          |
| S, its sign and its rank  | trade tape, per-minute taker buy and sell USD, masked | value: [t - 6 h, t); rank history: [t - 14 d - 6 h, t)                         |
| R and its scale           | 1 h perp klines                                       | value: closes at t - 6 h and t; scale: [t - 14 d - 6 h, t)                     |
| Entry and exit bars       | 1 min perp klines                                     | the bar from t + 1 min and the bar ending t + 1 min + H                        |
| Funding in the hold       | funding history and 1 min mark-price klines           | each settlement s with entry < s < exit, and the mark bar starting at s        |
| B1, B2                    | 1 h perp klines, BTCUSDT 1 h klines                   | [t - 24 h, t)                                                                  |
| B4 matching               | 1 h perp klines                                       | turnover [t - 24 h, t); hourly-return volatility [t - 14 d, t)                 |

- **Open interest known at t.** Bybit stamps a 1-hour open interest record with a time
  but does not say whether the value is taken at the stamp or at the end of its hour.
  OI(t) is therefore the record stamped t - 1 h. This assumes the record is available
  by the t + 1 min entry: Bybit documents the stamp, not when a record is first
  published, and G = OI(t) / OI(t - 24 h) - 1 uses the records stamped t - 1 h and
  t - 25 h. Its rank history reaches the record stamped t - 15 d - 1 h, one hour before
  the window opens for the first decision hour; that one history value is undefined
  (nothing before the window is fetched), which the 80% rule absorbs.
- The binding ranking history is 15 days of hourly klines, hence the first decision
  hour 2024-10-16T00:00Z.
- A rank is defined only when at least 80% of its 336 history values (t - 1 h back to
  t - 336 h) are defined; otherwise the feature is `undefined` with reason
  `rank_history_short`. The percentile is the mid-rank: (values below + half the values
  equal) / defined values.
- **S over 300 minutes.** The 6-hour interval of S keeps 300 of its 360 minutes (the
  mask drops 10 per hour); that is the nominal S, for the value at t and every value in
  its rank history alike. S is defined when every daily archive file holding a kept
  minute is complete; otherwise it is `undefined` (`tape_missing`). A kept minute with
  no trades is a genuine zero, not a gap. The 269 of 336 rule for the rank history is
  unchanged.
- **The mask changes the feature, not only its sample.** The share and the side of the
  dropped flow vary from window to window, so S with and without the mask can differ in
  size and even in sign. The question above is stated on the masked S for that reason;
  the sensitivity check in power step 2 measures how much the mask changes S.
- Open interest is used at 1-hour resolution for the setup, the controls and the
  baselines alike.
- Every native response or file is hashed once (SHA-256) and the report reads only
  verified snapshots (the carry and HYP-031 pattern).
- **Tape scope.** Tape is fetched per instrument for the union of [t - 14 d - 6 h, t) over
  every liquid decision hour passing conditions 1 to 3 (the primary scope). The four
  ablations need condition 4 wherever two of conditions 1 to 3 hold, a much larger scope;
  step 1 of the power step reports both, and the ablations dropping condition 1, 2 or 3
  run only if their scope is fetched. They decide nothing either way.

## Event definition (fixed now)

Decision times: every hour close t on the grid. Each ranked feature is the instrument's
own value at t ranked against its own previous 14 days of hourly values (a percentile in
[0, 1]).

**Liquidity:** perp turnover over the 24 hourly bars ending at t is at least
**USD 2,000,000**.

**Primary setup, all four at t:**

1. **Compressed range:** C = (max high - min low) / last close over the 24 hours ending
   at t. Its own 14-day percentile is at most **0.20**.
2. **Growing open interest:** G = OI(t) / OI(t - 24 h) - 1, in coins. G > 0 and its own
   14-day percentile is at least **0.80**.
3. **Funding at or below zero:** the last settled rate strictly before t is <= 0.
4. **Sell absorption** over the 6 hours ending at t:
   - S = (taker sell USD - taker buy USD) / (taker sell USD + taker buy USD);
   - R = close(t) / close(t - 6 h) - 1, also expressed in units of the instrument's 14-day
     standard deviation of 6-hour returns (used for matching);
   - absorption holds when **S > 0** (net selling), S's own 14-day percentile is at least
     **0.80** (unusually strong for this instrument), and **R >= 0** (the price did not
     fall).

**Determinability.** At each liquid decision hour each condition is `true`, `false` or
`undefined` with a reason (`kline_gap`, `oi_gap`, `no_settlement`,
`tape_missing`, `rank_history_short`). The setup is determinably false when any condition
is false, and undefined otherwise when any condition is undefined. Undefined hours are
counted, never treated as false.

**Entry:** long at the open of the 1-minute bar starting t + 1 min (taker).

**Exit (primary):** at the close of the 1-minute bar ending t + 1 min + H. H is chosen from
{4 h, 12 h, 24 h} by the power step (below), before any return, and then fixed.

**Independence:** after an event, the same instrument starts no new event until its hold
has ended (so the event set depends on H). Cluster = symbol-day; every statistic weights
clusters equally.

**Completeness:** an event whose entry or exit bar, or a funding mark inside its hold, is
missing is counted by what was missing and stays in the denominator as `incomplete`; none
is dropped silently.

**Funding during the hold.** Bybit charges funding on position value at settlement:
contracts times the mark price at that time. For each settlement s with entry < s < exit
(entry and exit fall at minute 1 of an hour, settlements on whole hours, so neither
boundary can coincide), the long's funding return is -r_s x mark(s) / entry price, where
mark(s) is the open of the 1-minute mark-price kline starting at s. The long receives it
when r_s is negative. Net is reported with and without funding; the reading uses net with
it, and the matched excess (condition 6) uses price returns only.

## Costs

Taker entry and exit: 5.5 bps fee per side plus a **9.5 bps per side** spread and
slippage scenario, **30 bps per round trip** primary, **50 bps** sensitivity, as HYP-031
(same order type). History has no quotes, so the break-even round-trip cost (mean gross)
is reported too. Compressed, quiet instruments may have wider spreads than the HYP-031
population: a pass earns a live quote measurement before paper, not belief.

## Baselines, ablations and the matched control (fixed now)

Each uses the same liquidity filter, entry, exit, H, costs and independence rule.

- **B1 plain momentum:** at the same t, long the instruments whose 24 h return is in the
  top 10% of the liquid universe.
- **B2 relative momentum:** the same, on 24 h return minus BTCUSDT's.
- **B3 compression breakout (rule against rule):** condition 1 and close(t) at the
  24-hour high; no open interest, funding or tape. B3 and the primary are both evaluated
  on the same instrument-hours: liquid hours where the primary is determinable (true or
  false), so a B3 firing in an hour where the primary is undefined is excluded and
  counted. Each rule's mean is its cluster mean over the whole window; a week in which a
  rule has no event contributes nothing to that rule's mean and is listed. The per-week
  difference and its week-block bootstrap are reported; the reading gates only on the
  point difference (condition 5).
- **B4 matched random:** for each event, one draw at the same t from liquid instruments
  where the setup is determinably false, matched on 24 h turnover decile and then nearest
  14-day volatility of hourly returns (log ratio). It needs no tape.
- **Ablations:** the primary setup without one condition at a time (4 versions), within
  the fetched tape scope. Amended 2026-11-06: only the ablation without condition 4 runs
  (it needs no tape); the three that drop condition 1, 2 or 3 would need the 706 GB
  ablation scope and are recorded as `not run`. B3 needs no tape, and every matched
  control candidate whose status depends on S passes conditions 1 to 3, so the primary
  scope covers both.

**Price-state matched control (condition 6).** For each event, at the same t: liquid
instruments for which condition 1 holds, R >= 0, and the setup is determinably false
because at least one of conditions 2, 3 or the S part of condition 4 is false (an
instrument whose only failing candidate is an undefined S is not a control). Of those
within 0.5 standard deviation of the event's R, the at most 5 nearest in R, ties by 24 h
turnover log ratio. Controls exit on the event's own exit bar; a control without that bar
has no value. The event's excess is its price return minus the mean price return of its
controls (no funding; both legs pay the same costs). So the control has the event's time,
compression and recent price path, and lacks the open interest, funding or flow
configuration: this is the test of "more than price".

## Power step (outcome-blind, before any setup return)

1. **Features and counts, no tape.** Fetch the catalog, funding, 1-hour klines and 1-hour
   open interest for the discovery universe; stop if a settlement time is not on a whole
   hour. Report the
   funnel of instrument-hours (grid, liquid, each condition true, false or undefined by
   reason) by hour of day, symbol and ISO week; the decisions passing conditions 1 to 3
   for each H's last decision hour, with clusters, distinct symbols, ISO weeks and the
   top-5 symbol share; the HYP-031 event times (any r_s <= -0.30%), as a count; and the
   symbol-days of the primary and ablation tape scopes.
   No feature value, price or return is printed, only counts.
   - **Archive volume.** The archive's directory listings (file names only) give which
     scope days have a file. Their sizes come from HEAD requests (Content-Length, no file
     content) on a fixed sample: per symbol, the scope days with the lowest SHA-256 of
     `bybit_compressed_spring_v1|SYMBOL|YYYY-MM-DD`, 1 in 20 and at least 2. The
     estimate is each symbol's mean sampled size times its scope days with a file. If the
     primary scope's estimate exceeds 1 TB, or is unknown because a symbol with files has
     no sampled size, an amendment changes the S ranking before
     any tape is fetched.
2. **Tape.** Only after the mask gates (Data boundary) pass. Fetch the primary scope,
   masked as above, with a hard byte cap of 200 GB counted during the download, and
   report the funnel after condition 4 and the share of events with a matched control.
   If coverage or the sample falls below the floor, the study stops there, before any
   return. Amended 2026-11-06, before any tape is fetched:
   - **Event hours** are the union of this study's funding and the frozen HYP-031
     index's (any r_s <= -0.30%), so a fetch gap in either cannot unreserve a boundary.
     The gate stops the run if any settlement of either snapshot is off the hour.
   - **Per file.** Each day file is streamed to a temporary file while its gzip EOF and
     CRC are checked, aggregated per kept minute and taker side (USD and base) with
     DuckDB, and deleted. Rows of reserved minutes collapse into one row count inside
     the query; their sums never leave it. A row with an empty or wrong symbol, an empty
     or unknown side, a time outside the day, or a size, price or notional that is
     missing, non-finite or not positive makes the whole day `invalid` (also inside a
     reserved minute, counted without any value), as do a damaged
     gzip stream and a format error.
   - **Reconciliation, per hour.** Every UTC hour of a file gets a status. An hour whose
     two mask boundaries are both unreserved is fully known (kept plus unreserved masked
     minutes): it is `matched` when its tape base volume is within 0.1% of the hourly
     kline volume plus one unit of that volume's last published decimal, and `mismatch`
     otherwise. Hours next to a reserved boundary are `unchecked_reserved`; hours
     without a kline are `missing_kline`. A `mismatch` hour makes undefined
     (`tape_mismatch`) exactly the S values whose 6-hour interval uses it; a `missing`
     or `invalid` day makes undefined every S touching it (`tape_missing`). These gaps
     then pass through the 269 of 336 rank rule. The pilot and the full run report, over
     `ok` days only, the share of hours matched (of all hours and of checked hours) and
     the relative gaps in fixed bins (0.01%, 0.05%, 0.1%, 0.5%, 1%), with rejected days
     counted apart; no threshold changes on them.
   - **What S means.** The daily CSV has no block-trade field, and early files no RPI
     field, so S is the taker-side imbalance of the archive's published trades.
     Reconciliation finds gaps and duplicates; it does not confirm the trade classes.
   - **Restartable and bound.** Each file's result is written as its own part; an
     interrupted run skips the files that have one. Before its first part a run
     directory is bound (`run.json`) to the scope, features and HYP-031 index SHA-256
     values, the pilot size and the rule constants; a resume or a `seal` with anything
     else is refused. The HYP-031 index is pinned to SHA-256
     `f48fe09f7023a476e524cf70c6ec1b629eee30a9a07cff41c9d149dfb59e0cf5`. `seal`
     publishes the manifest of all parts with their SHA-256 values once every scope day
     has a part.
   - **Hard byte cap.** Each download attempt reserves its whole Content-Length before
     its first byte is read, by appending it (fsynced) to the run's ledger; the spent
     total is the ledger's sum, shared by the worker processes and kept across resumes.
     A broken attempt counts in full, a crash loses nothing, and an attempt that would
     pass the cap is refused before reading.
   - **Pilot first.** A fixed pilot of 30 scope days (lowest SHA-256 of
     `bybit_compressed_spring_v1|pilot|SYMBOL|YYYY-MM-DD`) runs before the full scope:
     it checks the gate, the per-file checks, the reconciliation rate and the restart,
     and reads no return.
   - **Mask sensitivity (features only, decides nothing).** Around hour boundaries that
     are not HYP-031 event times nothing is reserved, so for those boundaries only the
     fetcher also keeps the masked minutes' aggregates, in a separate file that no
     feature reads. On the S windows whose seven boundaries are all of this kind it
     reports the share of turnover in the masked minutes (median, 90th percentile), how
     often S changes sign with the mask, and, for decisions passing 1 to 3 whose S and
     rank history are all of this kind, how often condition 4's S part agrees with and
     without the mask, with the counts behind each. It is reported before any return;
     a large change is a reason for the team to discuss an amendment then, not a rule.
3. **Events per H.** For each H in {4, 12, 24} h, apply that H's independence rule and
   report the events' clusters, symbols, weeks, top-5 share, and condition 4
   determinability among decisions passing 1 to 3.
4. **Dispersion only (its registry row first).** At random liquid decision hours, compute
   the standard deviation and the cluster design effect, no means, of (a) H-hour net long
   returns and (b) the difference between a random compressed instrument-hour (condition
   1 only) and its price-state matched control mean. From these and step 3's counts,
   estimate for each H the minimum detectable effect at 80% power, one-sided 5%, for the
   primary mean net (a) and for the matched excess (b).
   Amended 2026-11-07, after step 3 and before step 4 runs:
   - **Pseudo-samples shaped like the events.** For each H, a replicate moves that H's
     events week by week: one random whole-day shift inside the ISO week (never zero
     when the week has another day in that H's decision range, wrapping inside the
     week), so events sharing an hour stay together. Each event symbol-day (a source
     cluster) gets one instrument for all its events, available at every one of its new
     hours. An instrument belongs to at most one event symbol per week, fallbacks
     included, so two source clusters never become one symbol-day: an instrument this
     symbol already holds if one is available, else a random one no symbol holds yet. A
     source cluster with no such instrument is lost whole, never split. Only the events'
     weeks, days, hours of day and symbol identities are used, never a price. 200
     replicates per H, each seeded by its label.
   - **No path near an event.** A draw, on either leg, is never an instrument-hour
     whose path [t, t + H] comes within one hour of any condition 4 event's longest
     path [t_e, t_e + 24 h] on the same instrument, whatever the H; a control with such
     a path is not a candidate.
   - **(a)** draws from liquid instrument-hours; the value is the H-hour net long return
     at 30 bps with funding in the hold. **(b)** draws from liquid hours where condition
     1 holds and R >= 0 (the events' own price state; condition 1 alone would draw
     falling hours that the R caliper then rarely matches); the value is the price
     return minus the mean of its price-state matched controls (the step 2 rule, tape S
     part included), and a draw without a control has no value and is counted.
   - **Hourly prices.** Entry is the hourly close at t and exit the hourly close at
     t + H; the reading's minute bars start one minute later, and funding uses the
     hourly close at s for the mark.
   - **Minute-bar check, decided now.** An H is `near` when its floor is met, both legs
     are estimable and the larger effect lies in [45, 55] bps, both bounds included
     (the check could change its verdict). Step 5 walks H from the shortest; a
     `near` H stops it with `pending_minute_check`, even when a longer H is testable.
     The check then takes, for each `near` leg, the lowest-index usable replicate of
     that H and recomputes it on exactly its draws with the reading's prices (entry at
     the open of the 1-minute bar from t + 1 min, exit at the close of the bar ending
     t + 1 min + H, funding marks from 1-minute mark-price klines), the bars fetched for
     those instrument-hours only under their own registry row; a draw without minute
     bars is dropped from both versions of that replicate. The ratio of its minute to
     its hourly effect scales the leg's median effect, and step 5 runs again on the
     scaled effects, with no second check: one replicate measures the sensitivity to
     the minute shift, never replaces the median of 200. The calibration counts only
     when, after the unpaired draws are dropped, both versions still keep a value for at
     least 90% of that H's source clusters and the hourly effect is positive; otherwise
     the next usable replicate in index order is tried, and when none qualifies that H
     is not testable (`minute_check_failed`) and step 5 goes on to the next H. The first
     qualifying replicate's ratio is used whatever it shows, and a next H that is also
     `near` gets its own check.
   - **The error is the reading's.** Draws are averaged within each symbol-day first;
     clusters weigh equally. The standard error of their mean is the ISO-week block
     (cluster-robust) error, the variance the reading's week-block bootstrap estimates;
     the design effect is that variance over the independent one. The minimum detectable
     effect is (1.645 + 0.842) times it.
   - **Enough replicates.** A replicate is usable when it keeps a value for at least 90%
     of that H's own event clusters (source clusters, not the clusters they become); a
     leg is estimable when at least 95% of the 200 replicates are usable. Reported per H and leg: the usable count, the lost, incomplete and
     no-control shares, and medians over the usable replicates of the standard deviation,
     error, design effect and effect, with the effect's 90th percentile. No mean is
     reported. Step 5 compares the unrounded medians with 50 bps; an H with a leg that is
     not estimable is not testable, and a `near` H holds the choice until its minute-bar
     check (above) is done.
5. **Choose H** as the shortest horizon whose minimum detectable effect is at most
   **50 bps** for both (a) and (b), and whose step 3 counts meet the floor. If none is,
   the result is `insufficient_power` or `insufficient_data`: the question changes by an
   amendment **before** any setup return is read. Relaxing condition 4 to a rank 0.70, or
   dropping to conditions 1 to 3, counts as one more variant in the
   [discovery ledger](discovery-ledger.md).

The power step speaks for conditions 2, 3 and 6 of the reading. The point comparison
with B3 (condition 5) has no power estimate and is a size condition only.

The counts, the funnel and the dispersion are reported with their snapshot hashes.

## Reading (fixed now)

Discovery passes only if all hold, at the chosen H:

1. **Floor:** at least 150 clusters, 40 distinct symbols and 26 distinct ISO weeks; at
   least 95% of events complete; condition 4 determinable for at least 90% of the liquid
   decisions passing conditions 1 to 3; top-5 symbols at most 30% of clusters.
   Otherwise `insufficient_data` (including when the warm-up leaves too little).
2. **Size:** cluster mean net at 30 bps is at least **+30 bps**.
3. **Uncertainty:** the 5th percentile of the ISO-week block bootstrap of the mean net at
   30 bps is above 0 (10,000 resamples, seed 32; whole weeks with every cluster in them).
4. **Not one corner:** without the 5 symbols with the largest total net, the mean net
   stays above 0; each half (decisions before 2025-04-01, and from it) has a positive
   mean net.
5. **More than the breakout rule:** the primary's mean net exceeds B3's by at least
   **20 bps** (point estimate, both rules on the common instrument-hours above).
6. **More than price:** the cluster mean excess over the price-state matched control is
   at least **+30 bps**, its week-block bootstrap 5th percentile is above 0, and at least
   80% of complete events have a control.

A negative mean net at 30 bps is `fail` whatever else holds. A pass makes the primary
rule a stage 1 candidate (purged walk-forward, top-5 removal, 50 bps stress, baselines
B1, B2, B4) and then a candidate for the strategy's frozen holdout batch; never orders.

## Variants (each read once, reported, never the primary)

- **V1 taker-buy breakout:** conditions 1 to 3, plus taker buy USD over the last hour in
  its own 14-day top 10% and close(t) at the 24 h high. Separate family; not pooled.
- **V2 barrier exit:** take profit 3 ATR, stop 1.5 ATR (14-day hourly ATR), time stop at
  H.
- **V3 shallow gradient-boosted model** on the same features, purged walk-forward by
  month, depth 3, 200 trees; decisions in its top decile.
- **V4 other horizons** of {4, 12, 24} h, beside the chosen H.

Each variant's result enters the [discovery ledger](discovery-ledger.md) with the variant
count (primary plus 4, plus one if the power step relaxed the setup).

## Diagnostics (reported, decide nothing)

- Return per dollar-hour beside the primary metric.
- Net without funding, and funding received in the hold as a share of net.
- Dose response on the S percentile (0.70, 0.80, 0.90) and on the compression percentile
  (0.10, 0.20, 0.30).
- Regression slope of 5-minute returns on 5-minute net flow over the 6 hours (an
  alternative absorption measure).
- Maximum adverse excursion in the hold (99th percentile).
- By month, by BTC 24 h return sign, by hour of day.
- Dollars at USD 50, one position at a time (the event with the largest S at each t):
  trades per month and net USD per month.

## Decisions at the touchpoint (2026-11-06)

- **D1 window.** Discovery is 2024-10 to 2025-09 only. The reserved year stays HYP-031's;
  the old holdout stays the stage 2 batch's. If the warm-up leaves the floor unmet, the result is `insufficient_data` for this design.
- **D2 HYP-031 reservation.** The reservation is narrowed to tape minutes around
  HYP-031 settlements; the test year stays reserved whole. The mask is fixed: the
  minutes :55 to :05 around every hour, on every instrument, in history and live alike,
  covering every event and control window of the seconds draft; it covers tape only and
  never excludes a hold. Its cost is that S keeps 300 of 360 minutes and is a different
  feature from the unmasked S; the question is stated on it and step 2 measures the
  change. A rate-based mask (around extreme settlements only) was rejected: a fetch
  failure could open a reserved window, a rate published after t could change S, and
  live S would differ from historical S.
- **D3 absorption form.** S > 0, S percentile at least 0.80, R >= 0. The 5-minute slope
  is a diagnostic; the squeeze is the assumed mechanism, not an identified one.
- **D4 thresholds.** +30 bps net and +20 bps over B3 stay as discovery thresholds (at
  USD 50: USD 0.15 net and USD 0.10 over B3 per trade; +30 at 30 bps cost is +10 at
  50 bps). The "more than price" gate is the matched excess, +30 bps with its bootstrap
  bound.

## Runbook for power step 1

Run only after this protocol is on main, from a clean checkout of that revision, on the
owner's go. It reads no tape and computes no return.

```bash
uv run --package schurfer-analytics bybit-compressed-spring fetch-features \
  --snapshot runtime/research/hyp032/features-REV.jsonl.gz
uv run --package schurfer-analytics bybit-compressed-spring counts \
  --features runtime/research/hyp032/features-REV.jsonl.gz --features-sha256 SHA \
  --output-dir runtime/research/hyp032/step1 --code-revision REV --no-working-tree-dirty
uv run --package schurfer-analytics bybit-compressed-spring fetch-archive-sizes \
  --scope runtime/research/hyp032/step1/scope.json --scope-sha256 SCOPE_SHA \
  --snapshot runtime/research/hyp032/archive-sizes-REV.jsonl.gz
```

`counts` stops if a settlement time is not on a whole hour; `fetch-archive-sizes` keeps
listing names inside the window only and sends HEAD requests for the fixed sample only.
The fetch summaries, `counts.json` and the size summary, with their SHA-256 values, are
recorded here before step 2 is proposed.

## Runbook for power step 2 (tape)

From a clean checkout of the merged revision, on the owner's go; reads no return. The
pilot first, then the full scope into its own directory. An interrupted fetch is resumed
by running the same command again.

```bash
COMMON="--scope runtime/research/hyp032/step1-8522f0c/scope.json --scope-sha256 SCOPE_SHA \
  --features runtime/research/hyp032/features-8522f0c.jsonl.gz --features-sha256 FEATURES_SHA \
  --hyp031-index HYP031_INDEX --hyp031-index-sha256 HYP031_INDEX_SHA"
uv run --package schurfer-analytics bybit-compressed-spring-tape fetch-tape $COMMON \
  --out-dir runtime/research/hyp032/tape-pilot-REV --pilot 30 --max-bytes 5000000000
uv run --package schurfer-analytics bybit-compressed-spring-tape seal $COMMON \
  --out-dir runtime/research/hyp032/tape-pilot-REV --pilot 30
uv run --package schurfer-analytics bybit-compressed-spring-tape fetch-tape $COMMON \
  --out-dir runtime/research/hyp032/tape-REV --max-bytes 200000000000
uv run --package schurfer-analytics bybit-compressed-spring-tape seal $COMMON \
  --out-dir runtime/research/hyp032/tape-REV
```

The fetch logs a progress line every minute (files done, GB, MB/s, hours left, statuses).

## Runbook for the step 2 report (condition 4)

After the full tape is sealed, on the owner's go; counts only, no return. It verifies
the manifest and every part's SHA-256, refuses a pilot, a tape bound to other rules
than `tape-v3`, a tape fetched for another scope or features snapshot, and a manifest
whose parts are not exactly the primary scope's symbol-days, and publishes
`condition4.json` once.

```bash
uv run --package schurfer-analytics bybit-compressed-spring-cond4 \
  --features runtime/research/hyp032/features-8522f0c.jsonl.gz --features-sha256 FEATURES_SHA \
  --scope runtime/research/hyp032/step1-8522f0c/scope.json --scope-sha256 SCOPE_SHA \
  --manifest runtime/research/hyp032/tape-REV/manifest.json --manifest-sha256 MANIFEST_SHA \
  --output-dir runtime/research/hyp032/cond4-REV --code-revision REV --no-working-tree-dirty
```

It reports: the S part and condition 4 as true, false or undefined by reason among the
decisions passing conditions 1 to 3, and their determinable share (the floor needs
90%); for each H, the events after that H's independence rule (clusters, symbols, ISO
weeks, top-5 share, first half) and the share with at least one price-state matched
control (R within 0.5 of the event's R, R scaled by the instrument's 14-day standard
deviation of overlapping hourly-stepped 6-hour returns); and the mask sensitivity
(masked-minute turnover share, S sign changes, S-part agreement) on windows whose
boundaries are all unreserved. Entry and exit bars are not fetched yet, so the control
share is `pre_execution_control_share`, over all events; the reading's 80% applies to
complete events with a control that has a usable price path, counted after the bars.

## Runbook for power steps 4 and 5 (dispersion)

After the step 4 registry row is on main, on the owner's go, from a clean checkout of
the merged revision. It reads the same verified inputs as the condition 4 report and
publishes `power.json` once: standard deviations, errors, design effects, minimum
detectable effects and the chosen H, never a mean.

```bash
uv run --package schurfer-analytics bybit-compressed-spring-power \
  --features runtime/research/hyp032/features-8522f0c.jsonl.gz --features-sha256 FEATURES_SHA \
  --scope runtime/research/hyp032/step1-8522f0c/scope.json --scope-sha256 SCOPE_SHA \
  --manifest runtime/research/hyp032/tape-1327ea1/manifest.json --manifest-sha256 MANIFEST_SHA \
  --output-dir runtime/research/hyp032/power-REV --code-revision REV --no-working-tree-dirty
```

## Power step 1 result (2026-11-06)

Counts only: no feature value, price, tape or return was read.

**Features.** `fetch-features` ran from the merged revision 8522f0c, 14:22:49Z to
15:23:00Z: 672 symbols, no errors, 31,324 responses, 22 kline requests recovered after a
retry. `features-8522f0c.jsonl.gz`, 177,952,391 bytes, SHA-256
`a3b54d6ce092a73afbd4911e8ea72862a707846b45fbb23d05ec1d91f71ebab6`. 671 instruments load (LAYERUSDT, live for four hours on 2025-01-24, has no data in the window).

**Counts.** `counts` on that snapshot, 17:39:56Z, 74 s. `counts.json` SHA-256
`a36880e27a4b55ddac0a3c3654cd8832a3503ed10c727b73582bd788708bbc5a`, `scope.json`
SHA-256 `a96ea58b0c9b04c8020615e909b435abcb4b711145c98ed9e21699738a54fce2`.

| Funnel (instrument-hours)                  | Count                        |
| ------------------------------------------ | ---------------------------- |
| Grid                                       | 4,214,277                    |
| Liquid                                     | 2,391,909                    |
| Illiquid / kline gap                       | 1,814,943 / 7,425            |
| Condition 1 true / false / undefined       | 438,331 / 1,894,834 / 58,744 |
| Condition 2 true / false / undefined       | 532,535 / 1,800,125 / 59,249 |
| Condition 3 true / false / undefined       | 373,258 / 2,018,595 / 56     |
| Conditions 1 to 3 true / false / undefined | 8,131 / 2,371,551 / 12,227   |

| Conditions 1 to 3, by H | 4 h   | 12 h  | 24 h  |
| ----------------------- | ----- | ----- | ----- |
| Decisions               | 8,131 | 8,124 | 8,123 |
| Clusters (symbol-days)  | 2,271 | 2,269 | 2,268 |
| Symbols                 | 370   | 370   | 370   |
| ISO weeks               | 50    | 50    | 50    |
| Top-5 cluster share     | 6.9%  | 6.9%  | 6.9%  |
| Events, upper bound     | 2,911 | 2,121 | 1,886 |

- 959 of the clusters fall in the first half (before 2025-04-01).
- One ISO week has no decision: 2024-W49 (2 to 8 December 2024), inside the window.
- Hours of day are even (301 to 366 decisions each).
- HYP-031 event times (any r_s <= -0.30%): 2,549.
- Tape scope: primary 20,673 symbol-days on 370 symbols; ablation 113,826 on 630.

Conditions 1 to 3 alone clear the floor (150 clusters, 40 symbols, 26 weeks, top-5 at
most 30%) with room; condition 4 is still to come and will cut most of them.

**Archive sizes.** Attempt 1 (17:41:20Z, from 8522f0c) stopped after about 2,730
requests in 4 minutes: CloudFront answered 403 to every request from this IP. Its
partial snapshot (`archive-sizes-8522f0c.jsonl.gz`, SHA-256
`5e7f98f149169ce6d42ee905c2fae4b4e89c1729e842654ad150fe14e403b57e`) is not used.
Attempt 2 ran from 1fedaff (#555, paced at 3 requests per second), 18:10:24Z to
18:47:18Z: 630 listings and 6,003 HEAD requests, all 200.
`archive-sizes-1fedaff.jsonl.gz`, SHA-256
`22e7c1196536a0db420c616138396022a924274aec73b31b84522fc290d41640`.

| Tape scope | Symbol-days | With a file | Estimated compressed bytes |
| ---------- | ----------- | ----------- | -------------------------- |
| Primary    | 20,673      | 20,661      | 160.9 GB                   |
| Ablation   | 113,826     | 113,702     | 705.9 GB                   |

The run itself printed the primary cap status as `unknown` with 47 symbols "without
size": its estimate used only samples that fell on primary days, while the sample is
drawn from the union of both scopes and this protocol's rule is each symbol's mean over
its whole sample. Recomputed offline from the same snapshot with the corrected
`estimate` (no new request), every symbol has a size and the primary scope is 160.9 GB,
`within` the 1 TB cap. At the 4.16 MB/s the HYP-031 tape pilot measured end to end, the
primary scope is about 11 hours of download and parsing, the ablation scope about 47.

## Power step 2 pilot (2026-11-06)

From the merged revision 4c26333 (rules `tape-v2`), 20:48Z; no return read.

- **Gate:** passed. Event hours 2,550: 2,549 from this study's funding, one more only
  in the HYP-031 index, so the union mattered.
- **Files:** 30 of 30 `ok`, no bad row; 5,085,484 kept rows and 282,676 rows in reserved
  minutes (counted only).
- **Reconciliation:** 319 hours matched, all within 0.01%; no mismatch; 401 hours
  `unchecked_reserved` (44% of the hours in `ok` days are checked; the event hours are
  dense).
- **Volume and speed:** 215,431,329 bytes in 19.5 s (about 11 MB/s; the full 160.9 GB
  scope is about 4 hours). Parts are about 33 KB each.
- **Manifest:** `tape-pilot-4c26333/manifest.json`, SHA-256
  `c1a73d29f54445bbe96281f33dfc4da65d2cd3a0e7b0732d37e465302dc6ebbf`.
- **Restart:** a second pilot directory was killed with SIGTERM after 11 files (11
  parts, 11 ledger entries, no temporary file, no worker left); the resumed run
  fetched the other 19 and sealed with the same statuses and hour counts.
- **Found:** parts were not byte-reproducible. Three of 30 differed in one minute at the
  sixteenth digit (DuckDB sums doubles in parallel order) although the source files had
  the same SHA-256, and gzip wrote a timestamp into each part. Fixed before the full run
  (rules `tape-v3`): sums are exact DECIMAL(38, 12) and parts are written with gzip
  mtime 0. The pilot's parts were a check only; the full run uses `tape-v3`.

## Power steps 2 and 3 result (2026-11-07)

Counts only: no price path after t and no return was read.

**Tape.** `fetch-tape` from the merged revision 1327ea1 (rules `tape-v3`), started
2026-11-06 22:24Z. The Mac slept overnight and the run froze after 3,923 files; it was
stopped and resumed at 07:31Z under `caffeinate` and finished 10:01Z (16,741 files in
the resumed run). `seal` passed the gate again (2,550 event hours, 2,549 from this
study's funding) and sealed at 10:02Z.

- **Files:** 20,661 `ok`, 12 `missing` (exactly the 12 symbol-days without a file in
  the listing, all within a few days of a listing: KAITO, LA x2, MICHI, NS x2, PROVE x2,
  SHELL x2, VR x2); no `invalid` day. 4,099,007,045 kept rows and 247,316,801 rows in
  reserved minutes (counted only).
- **Reconciliation over `ok` days:** 221,968 hours matched, 2 `mismatch`, 36
  `missing_kline`, 273,858 `unchecked_reserved`; 44.8% of the hours are checked (the
  pilot had 44%), and all but 2 of the checked hours match. Gap bins: 221,967 within
  0.01%, 1 within 0.05%, 2 (the mismatches) within 0.5%, none above.
- **Bytes:** 168,395,213,408 reserved in the ledger (168,349,027,022 file bytes), under
  the 200 GB cap.
- **Manifest:** `tape-1327ea1/manifest.json`, SHA-256
  `21bdf61989a9aac4ebf76e70d260e1aa3541cb66151c362f3189537389d26d50`.

**Condition 4.** `bybit-compressed-spring-cond4` from the merged revision fdd1f51,
10:02:51Z to 10:04:47Z, clean tree. `condition4.json` SHA-256
`73acbd2f92721e89c14b11e715ab1c1fe02d37e4f293e017af555f76c1bb754f`.

| Condition 4 funnel                  | Count         |
| ----------------------------------- | ------------- |
| Decisions passing conditions 1 to 3 | 8,131         |
| S part true / false                 | 1,789 / 6,342 |
| Condition 4 true / false            | 582 / 7,549   |
| Condition 4 undefined               | 0             |
| Determinable share (floor 90%)      | 100%          |

No decision is undefined. 17 decisions (8 symbols) have a tape gap in their rank
history, 6 to 42 of the 336 values, under the 67 the 269 rule allows; none has one in
its own 6-hour window.

| Events after each H's independence rule | 4 h   | 12 h  | 24 h  |
| --------------------------------------- | ----- | ----- | ----- |
| Decisions                               | 344   | 321   | 314   |
| Clusters (floor 150)                    | 328   | 319   | 314   |
| Symbols (floor 40)                      | 174   | 174   | 174   |
| ISO weeks (floor 26)                    | 47    | 46    | 46    |
| Top-5 cluster share (at most 30%)       | 8.8%  | 8.8%  | 8.6%  |
| Clusters in the first half              | 150   | 146   | 144   |
| Events with a matched control           | 342   | 319   | 312   |
| `pre_execution_control_share`           | 99.4% | 99.4% | 99.4% |

Every H meets the count part of the floor; event completeness (95%) and the reading's
80% control share are counted after the entry and exit bars.

**Mask sensitivity (decides nothing).** On 39,780 S windows whose boundaries are all
unreserved, the masked minutes carry a median 16.8% of the turnover (90th percentile
24.4%), about their 10 of 60 minutes, so trading does not bunch at the hour edge; S
changes sign with the mask in 4,488 of them (11.3%). The S-part agreement compared no
decision: it needs all 343 boundaries of a decision's window and rank history
unreserved, and with 2,550 event hours in the window (one about every 3.4 hours) no
14-day history is. The diagnostic cannot be computed as written; it is recorded empty,
not replaced.

**Next.** Step 4 (dispersion only) adds its registry row first; then step 5 chooses H.

## Effort and stop

Code mirrors the carry and HYP-031 screens: fetch once into hashed snapshots, report
offline (Parquet and DuckDB per the strategy). Week 1: this protocol and power step 1
(features, funnel, counts, tape volume). Stop if the counts fail the floor, if the power
step finds no testable H, if delisted instruments cannot be covered, or if discovery does
not pass.
