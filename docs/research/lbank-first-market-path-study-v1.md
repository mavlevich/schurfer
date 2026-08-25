# LBank-First Market-Path Study v1

Status: planned, discovery only. This contract does not authorize PAPER or LIVE.

## Money question

When an eligible crypto perpetual is first detected on LBank before every other
observed venue, does a trade entered after a realistic detection and quote delay have
positive net expectancy as either:

1. continuation long; or
2. reversal short?

The report is allowed to reject both sides, nominate at most one pre-registered
segment for prospective shadow confirmation, or return insufficient data. It may not
promote the best historical cell directly.

## Why this lane is bounded

The production audit on 2026-08-25 found 1,251 sole-LBank pump events across 83 assets.
There are 1,224/850/114 events mature for 1d/7d/28d, but zero exact complete LBank
perpetual outcomes at those horizons. This is a measurement gap over an already large
denominator, not permission to add unrelated feeds or build a general ML platform.

Every data PR in this lane must unlock the next named report or forward decision. A
platform improvement without a blocked consumer is parked. Existing registered
early-momentum and pump-short prospective checkpoints continue unchanged.

## Confirmed integrity blockers

1. The broad scanner records `market_type=swap` but does not share the Bybit/Binance
   asset-class allowlist. Tokenized equities can enter a crypto pump cohort.
2. Fresh listing tickers can report extreme 24-hour percentage changes because their
   comparison baseline has just initialized. Listing/open age is a feature and cohort
   boundary, not an ordinary pump return.
3. MEXC `CATE_USDT` proves venue lifecycle fields are not interchangeable: the live
   contract detail reports a 2024 `createTime` and a 2026-07-27 `openingTime`, while
   the scanner currently persists `createTime` as `onboarded_at`.
4. LBank has current perpetual market data but no supported historical perpetual path
   in the current resolver. Scanner event snapshots are not continuous outcomes.
5. Same-ticker cross-venue recovery is not identity proof. A proxy requires a reviewed
   canonical asset link and remains separate from native LBank execution evidence.

## CATE case study: motivation, not a selected rule

Schurfer first observed MEXC `CATE_USDT` on 2026-07-27 06:40:06 UTC at `0.00926`, 40
minutes after the official futures open. A read-only pull of 707 official MEXC hourly
contract candles on 2026-08-25 produced:

| Path point                                |    Price | Return from first Schurfer observation |
| ----------------------------------------- | -------: | -------------------------------------: |
| 1 day                                     | 0.003976 |                                 -57.1% |
| 7 days                                    | 0.038187 |                                +312.4% |
| 28 days                                   | 0.037870 |                                +309.0% |
| Current ticker at 2026-08-25 19:21:59 UTC | 0.074325 |                                +702.6% |
| Lowest hourly low                         | 0.002765 |                                 -70.1% |
| Highest hourly high                       | 0.099584 |                                +975.4% |

The path invalidates both naive stories. An unprotected short eventually faces an
extreme adverse move; a leveraged long can be liquidated before the later gain. The
candidate phenomenon is listing/price discovery, not an already established trade
direction.

## Canonical input contract

Keep raw/native observations and normalized analytics separate. The normalized
envelope includes:

- venue, native market id, canonical instrument id, asset class, market type, and
  contract variant;
- instrument-created, announced, trading-open, first-observed, suspension/resumption,
  and delisting timestamps with source provenance;
- market event time, exchange publish time when available, local receive time, and
  persistence time;
- bar interval, capture/schema/universe/classifier versions, completeness, gaps, and
  coverage kind;
- a path provenance of `exact_native`, `same_asset_proxy`, `third_party`, or
  `unrecoverable` plus an immutable content fingerprint.

Typed price bars, trades, BBO/depth, OI, funding, liquidations, and announcements keep
their own semantic contracts. Missing venue capabilities are NULL with an explicit
reason, never fabricated zeros. Spot data never substitutes for a perpetual.

## Cohort and methods

- Keep three mutually exclusive source cohorts: `lbank_first_strict` when LBank leads
  the next valid venue by more than the registered scan/alignment tolerance,
  `first_source_tie` inside that tolerance, and `lbank_only` when no other valid venue
  appears in the observation window. Do not call scanner polling order venue lead.
- Build episodes before splitting repeated alerts. Deduplicate one underlying move;
  retain suppressed/reopened observations as diagnostics.
- Segment at minimum by asset class, listing state, exact/proxy path, liquidity,
  first-source tie status, and calendar week. Tokenized equities are a separate study.
- Freeze discovery/validation/test time boundaries before aggregate outcome reads.
- Pre-register a small entry grid such as 0/1/5/15 minutes after the actionable quote;
  do not optimize every second.
- Evaluate 15m, 1h, 4h, 1d, 7d, and 28d forward paths. Report both long and short net
  returns, MFE, MAE, time-to-extreme, fees, funding, spread/slippage, fill capacity,
  drawdown, losing streak, concurrency, and capital occupancy.
- Missing paths and delistings stay in the denominator with explicit terminal reasons.
  Proxy results are a sensitivity appendix, not native LBank promotion evidence.

## Money-first PR sequence

1. `fix/mexc-instrument-lifecycle-opening-time-v1`: separate creation/open timestamps,
   version the mapping, and audit changed identities.
2. `fix/pump-scanner-asset-class-and-listing-baseline-v1`: reuse the normalized asset
   class, retain non-crypto research events separately, and mark fresh listing
   baselines instead of treating them as ordinary pumps.
3. `feat/canonical-market-path-contract-v1`: immutable path/coverage artifact plus
   official MEXC backfill and forward LBank perpetual price/BBO capture. Do not add
   every possible feed.
4. `analysis/lbank-first-market-path-v1`: freeze the complete denominator and run this
   event study. Publish rejected cells and unresolved paths as well as any candidate.
5. `feat/lbank-first-prospective-shadow-v1`: only if one validation/test segment is
   positive after costs and robust across assets/weeks; freeze it before the forward
   cutoff.
6. A bounded live probe is discussed only after the prospective evidence floor and
   the existing execution/risk checklist pass. Otherwise park the lane.

ML may consume the same frozen feature/label artifacts after the transparent event
study establishes a stable target and adequate sample. ML is not a substitute for
identity, lifecycle, path, cost, or prospective evidence.
