# Bybit spot/perpetual carry: execution feasibility v1

Status: protocol and reader prepared **before** any run. No result artifact is
included in this change. This is a read-only public-market canary, not a
strategy, historical return test, funding-income estimate, or order gate.

## Question and decision

Can Bybit's public catalogs and books show repeatable $50 spot/perpetual pairs
with four usable execution sides and published order limits? The $300 bank and
an illustrative +300% adverse move are reported as context, not a margin-safety
gate. A public-market canary cannot establish that an isolated short survives
that move.

If fewer than 10 distinct exact catalog pairs pass in at least two of the three
rounds, stop the **$50 Bybit-only carry** line at its book/order-minimum gate.
Otherwise, the only permitted next decision is whether to design an account-mode
and margin-risk preflight together with separate prospective funding and basis
capture. No canary result authorizes orders, establishes margin safety, or
claims a positive carry return.

## Frozen collection

- Public V5 `instruments-info` spot catalog (no pagination) and every page of
  the linear catalog. Require `retCode=0`, a complete non-repeating cursor
  traversal, `Trading` USDT spot and `Trading` USDT-settled
  `LinearPerpetual`, one instrument per exact native `baseCoin` on each side,
  and equal native symbols. Pre-listing, special spot products, ambiguous
  bases, missing limits, or unavailable 1x are excluded with reasons.
- This is **catalog matching only**. It is not the approved asset identity
  required to place an order. No synthetic symbol construction is used.
- All catalog pairs are counted. To bound API requests, sample at most 50 by
  ascending SHA-256 of the native spot symbol; no funding rate, pump history,
  price, spread, or future result enters selection.
- Three rounds 60 seconds apart, at most four pairs in flight. Fetch spot and
  linear 50-level books concurrently for each selected pair. At most 300 book
  requests plus catalog pages. No authentication, database access, service,
  WebSocket, or order endpoint. One write-once JSON artifact stores the native
  catalogs, native book responses, timestamps, filters, reasons and code state;
  its SHA-256 is printed. A second run must use a new named artifact and is
  descriptive, not another attempt at the gate above.

## Fixed feasibility arithmetic

- Book age at receipt must be in [-1000, 2000] ms; spot/perp book timestamps
  must differ by no more than 1000 ms. Empty, crossed, unordered, malformed,
  nonpositive and insufficient-depth books fail closed.
- Use one identical base quantity on both legs. Round down to a common multiple
  of spot `basePrecision` and perpetual `qtyStep`, keep each of the four
  book-side notionals at or below $50, then check spot `minOrderAmt`, perpetual
  `minOrderQty` and `minNotionalValue`, and published market-order maxima. An
  unknown mandatory minimum is an exclusion, not a pass. A missing spot market
  maximum is recorded as unknown; a book pass is not an order authorization.
  Spot `minOrderQty` is retained from the native catalog when present but is
  **not** a gate: Bybit's current V5 specification marks it deprecated and says
  to check `minOrderAmt` instead. The documented maximum fields are spot
  `maxMarketOrderQty` and linear `maxMktOrderQty`; the first live catalog remains
  necessary to confirm their presence for the sampled instruments.
- The simultaneous four-trade crossing cost is spot ask minus spot bid plus
  perpetual ask minus perpetual bid for that same quantity, plus a **scenario**
  fee of 10 bps on each of the four notionals. It includes the four book sides;
  do not add a second spread. Report dollars and bps of the short-entry notional.
  This is an entry/exit friction snapshot, not a realized round trip.
- Illustrative cash is spot ask spend + four times the current perpetual ask
  notional + the four fees. With all four notionals capped at $50, it cannot
  exceed about $250.20 and therefore cannot test a $300 bank. It is descriptive
  only and never contributes to the pass/fail result. Free USDT does not protect
  an isolated short unless an account feature or explicit position-margin
  allocation actually transfers that reserve to the position. Even then,
  maintenance margin and mark-price liquidation need an account-specific test.
  The next preflight must establish the selected margin mode, the permitted
  mechanism and its limits, collateral availability, and a conservative
  liquidation bound before any carry order is considered.

## Research boundary

The CLI refuses any run before **2026-10-31T00:00:00Z**, the existing research
reassessment date. This fixed boundary does not depend on whether HYP-012 v2
has enough qualified episodes for a formal read. The canary selects from the
contemporaneous public catalog by native-symbol hash; it never queries HYP-012
captures or results, nor does its result amend that cohort. If v2 is still
blind on that date, its registered diagnostics and formal read keep their own
order. The canary does not read funding settlements, predict funding rates, or
rank coins by them. A later
funding study must freeze its cohort, timestamp semantics, missing-settlement
rule, fees, basis risk and independent promotion gate before seeing outcomes.

Bybit's official V5 documentation specifies that spot instruments are not
paginated, linear instruments require cursor pagination, spot uses
`basePrecision` and `minOrderAmt` (and deprecates `minOrderQty`), the market
maximum fields are `maxMarketOrderQty` for spot and `maxMktOrderQty` for linear,
and both categories return native book `ts`,
`u`, `seq` and `cts`: [instruments-info](https://bybit-exchange.github.io/docs/v5/market/instrument),
[orderbook](https://bybit-exchange.github.io/docs/v5/market/orderbook).
