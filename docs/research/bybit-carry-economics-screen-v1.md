# Bybit spot and perpetual carry: economics screen v1 (planning, discovery)

Status: **rules fixed before the first fetch.** A planning report on public history,
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
