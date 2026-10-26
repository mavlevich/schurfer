# Bybit spot and perpetual carry: economics screen v1 (planning, discovery)

Status: **rules fixed before the first fetch; reading v1.1 fixed 2026-10-25, before the
v1 result was read** (see the last section). A planning report on public history,
like the [cost and power planning report](cost-power-planning-v1.md). It is discovery
for the carry line: it can say whether stage B of the
[carry preflight design](bybit-spot-perp-carry-preflight-v1.md) is worth designing,
and what must be true for it. It is not a registered cohort, a verdict, an order
gate or an expected return. It changes nothing in the
[canary](bybit-spot-perp-feasibility-v1.md) or stage A: their selection, gates and
run date stay as they are.

## Question

On Bybit's public history, does a simple funding carry (long spot, short the USDT
perpetual, same base quantity) clear its costs, how often, on how many pairs, and how
many dollars does it give the owner's USD 300 bank with USD 50 per leg?

## Data boundary

- Decisions from **2024-10-01T00:00Z**; every request ends no later than
  **2026-09-28T23:59:59.999Z**. The fetcher refuses a later end before any request.
  No Schurfer capture, cohort or result is read; only Bybit public V5 market data.
- Pairs: the canary's own catalog matching (`select_pairs`: `Trading` USDT spot and
  USDT-settled `LinearPerpetual`, one instrument per exact native base, equal native
  symbols, no special spot products, 1x available). The catalog is today's, so pairs
  delisted before today are missing (survivorship) and pairs listed later have shorter
  history. Both are reported, never corrected.
- Per pair: every settlement from `/v5/market/funding/history`, and 4 h klines
  (`/v5/market/kline`, interval 240) for spot and linear. Every native response is
  stored once in a gzip JSON-lines snapshot whose SHA-256 the report records.

## Fixed arithmetic

- **Grid.** Decisions at every 4 h UTC boundary t. `price(t)` is the close of the 4 h
  bar that ends at t, on each leg separately.
- **Signal.** `F24(t)`: the sum of settled funding rates with timestamp in
  (t - 24 h, t], in bps per day. Unavailable when the contract has less than 24 h of
  settlements before t, or when two consecutive settlements in that window are more
  than 8 h apart (a gap is missing, never zero).
- **Rule H** for H in {5, 10, 20, 40} bps/day: enter when `F24(t) >= H`; exit at the
  first later decision t' with `F24(t') < H / 2` or unavailable. **Fills one bar
  later:** entry at `price(t + 4h)`, exit at `price(t' + 4h)`, so nothing is filled at
  the price the signal saw.
- **Quantity.** q = USD 50 / max(spot entry, perp entry): both legs at most USD 50.
- **Money** (bps of USD 50, and dollars):
  - basis: q x [(spot exit - spot entry) - (perp exit - perp entry)];
  - funding: for each settlement strictly between entry and exit fill,
    q x perp price x rate, the perp price being `price` at the first 4 h boundary at or
    after the settlement (the close, not the mark price: an approximation, labelled);
  - fees: 10 bps on each of the four fill notionals (the canary's scenario fee);
  - spread: closes are not bid and ask. The report gives net at zero crossing cost and
    the **break-even round-trip crossing cost**, the four book sides together, at
    which net is zero. The canary measures the actual crossing on 2026-10-31.
- **Missing price** at a fill: the trade is not formed and is counted by reason.
  A position still open at the end is closed at the last available bar and flagged.

## Views

1. **Funding landscape:** share of positive settlements, per-pair mean funding,
   distribution of `F24`, by month.
2. **All pairs, unlimited slots** per H: trades, distinct pairs and UTC weeks, mean and
   median net bps, profit factor, the funding, basis and fee parts, holding days, the
   top-5 pairs' share of net, maximum concurrent positions (capacity).
3. **The owner's bank, one slot** per H: when the slot is free, take the eligible pair
   with the highest `F24` (ties by symbol). Dollars in total and per month, months
   positive, return on the about USD 210 the design allocates per pair and on the
   USD 300 bank.

## Reading (fixed now)

`stage_b_worth_designing` if, for at least one H, the one-slot portfolio has positive
net after fees, a break-even crossing cost of at least 20 bps, positive net in at least
60% of months, and the all-pairs view at that H has the top-5 pairs below 50% of
positive net. Otherwise `carry_not_worth_stage_b_at_usd_50`. Four values of H are
four looks at one history: a pass permits a stage B design with its own prospective
registration, never orders, and the H that passed is not thereby chosen.

## Reading v1.1 (fixed 2026-10-25, before the v1 result was read)

**Provenance.** The v1 fetch and report ran on 2026-10-22 (snapshot sha256
`24a7f03b...`, 287 pairs, no fetch errors). Neither the owner nor the reviewer read
that result before this section and its code were committed. The v1 rule did not
decide whether the dollars justify more work, and a review found three defects that bear
on a money decision. Version `bybit_carry_economics_screen_v1_1` fixes both and is
computed from the same snapshot; the v1 rule is still reported, for reference only.

**Fixes.**

- **Crossing cost.** Every decision figure is after 20 bps round-trip crossing per trade
  (`net_bps - 20`), not at zero crossing. The zero-crossing v1 figures stay in the report.
- **Missing exit price.** A missing price at the exit fill no longer drops the trade,
  since halts and delistings are when prices go missing and when losses happen. The
  position closes at the first later fill with both closes and is flagged
  `exit_delayed`; with no later prices it is `exit_unresolved`. In the one-slot view
  the slot stays busy until the delayed fill. A missing funding-time price uses the
  next 4 h close within the hold; a missing entry price still forms no trade.
- **Liquidation.** The perp's 4 h highs from the snapshot are kept. A trade whose
  highest perp price between the entry and exit fills reaches 4x the entry fill (the
  +300% the isolated collateral covers) is a `liquidation_breach`. Last-trade highs,
  not the mark price: an approximation, labelled.

**Rule.** Benchmark B = 5% a year, the owner's alternative for idle USDT. An H passes
only if, on the one-slot view at USD 210 allocated and after 20 bps crossing:

1. annual return on USD 210 is at least 10% (2B);
2. each half of the window (trades exiting before 2025-10-01, and from it), annualized
   over its 12 months, is at least 5% (B);
3. without the pair with the largest after-crossing net, the rest is at least 5% a year
   (its trades removed, the slot not refilled);
4. at least 60% of months are positive after crossing, by exit month;
5. the all-pairs top-5 pairs hold less than 50% of positive net (v1 condition);
6. no liquidation breach and no unresolved exit;
7. a neighbouring H in (5, 10, 20, 40) is at least 5% a year on the same view, so a
   single lucky threshold cannot pass alone.

`stage_b_worth_designing` if any H passes; otherwise `carry_not_worth_stage_b_at_usd_50`.
A pass permits a stage B design with its own prospective registration, never orders,
and does not choose that H. The annual return on USD 100 (spot plus 1x collateral, as
cross-collateral would allow) is printed as a diagnostic and decides nothing: stage A
must first show the owner's account supports it safely.

**What 10% on USD 210 means.** USD 21 a year per USD 50 notional, about 42% a year net
on the notional: isolated +300% collateral cuts the return on capital about fourfold.
At the USD 300 bank a pass is about USD 2 a month: a permission to design and scale,
not income.

**Known limits, unchanged.** Today's catalog (survivorship, likely flattering the
one-slot view, which picks the highest funding); exit-month attribution for months and
halves; 4 h closes, not bid and ask; the close, not the mark price, for funding; four
looks at H.

## v1.2 fixes (2026-10-25, after the v1.1 read; the reading rule is unchanged)

A colleague review of v1.1 found two accounting gaps. Neither can turn the verdict
positive; both make the figures honest.

- **Liquidation is now priced, not only flagged.** When the perp high reaches 4x the
  entry fill, the short is bought back at 4x (the collateral is lost) and the spot leg
  sells at the next 4 h close; funding stops there.
- **An unresolved exit keeps the one-slot capital to the end of the window.** It was
  freed at the next bar. An unresolved exit already fails the reading.

- **A position still open at the end with no later bar** (`no_exit_bar`) now also fails
  `no_unresolved_exit`, from a second review. The snapshot read here has no such case in
  the one-slot view, so the result below is unchanged.

The reported third gap, a same-pair re-entry before a delayed exit in the all-pairs
view, cannot occur: a re-entry needs a bar with both closes after the exit signal, and
the delayed exit takes the first such bar. No change there.

## Result (read 2026-10-25)

Snapshot `24a7f03bc934dd73...` (287 pairs, 24 months, no fetch errors; 133 pairs listed
after the window start). Reports from that one snapshot, clean trees:

| Version | Code      | Result sha256 prefix | Reading                             |
| ------- | --------- | -------------------- | ----------------------------------- |
| v1.1    | `e5f7dc6` | `32c4d5c8`           | `carry_not_worth_stage_b_at_usd_50` |
| v1.2    | `cea3a14` | `98132e29`           | `carry_not_worth_stage_b_at_usd_50` |

The v1 report of 2026-10-22 (code `a266ce2`) was never read; the v1 rule, recomputed
inside both reports, also gives `carry_not_worth_stage_b_at_usd_50`.

One slot, after 20 bps crossing per trade, a year on USD 210:

| H bps/day | v1.1 (breach flagged only) | v1.2 (liquidation priced) | Liquidations |
| --------- | -------------------------- | ------------------------- | ------------ |
| 5         | +0.3%                      | -15.3%                    | 1 (4.05x)    |
| 10        | -9.7%                      | -36.1%                    | 1 (7.26x)    |
| 20        | -7.1%                      | -7.1%                     | 0            |
| 40        | -4.9%                      | -4.9%                     | 0            |

No H passes any hurdle condition. Why:

- **Base funding cannot carry this collateral.** The median F24 is 2.55 bps a day (the
  default 0.01% every 8 hours, about 9% a year on notional); 78% of settlements are
  positive. With isolated +300% collateral a pair ties up about 4.2x its notional, so
  base funding is 2 to 3% a year on capital before any cost, below the 5% benchmark.
  Cross-collateral (the USD 100 diagnostic) does not rescue it: -10% to -76%.
- **Chasing high funding churns fees.** At H >= 10 the one-slot holds last 1 to 2 days;
  funding earns 6 to 20 bps against 40 bps of fees.
- **The +300% stress is real.** The slot's highest-funding picks include squeezes to
  4x and 7x; in the all-pairs view at H = 5 there are 10 breaches, up to 9.8x.

**Decision this supports:** do not design stage B for USD 50 isolated carry. This closes
the configuration screened (size, collateral, rules), not every carry strategy.
