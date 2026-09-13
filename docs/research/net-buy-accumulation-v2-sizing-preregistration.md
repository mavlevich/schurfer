# net-buy accumulation v2: sizing and economics pre-registration (DRAFT, for review)

Frozen BEFORE any return is read, so the sizing rule and the stop/continue criterion
cannot be chosen after seeing which cutoff looks good. This exists because the first
executability cut used the fire minute's own volume (look-ahead) and stated an edge
verdict without reading returns; both are corrected here. Companion evidence (the
outcome-blind capacity curve) is `evidence/net-buy-accumulation-v2-liquidity-floor.*`.

Nothing here freezes the v2 contract or authorizes orders. It fixes the parameters of
ONE cheap economics pass; the returns read that uses them is gated on methodology
review and on the mandatory report-metrics gate landing first.

## Why dynamic sizing, not a fixed-size gate

A binary "drop fires where 1500 USD is over 10% of volume" filter is wrong twice: it
sizes on look-ahead volume, and it throws away fires that a smaller position could
still trade. Instead we size each fire to the flow it can bear and keep it while that
is economically worthwhile.

## Point-in-time flow estimator (frozen)

- `conservative_trailing_flow_usd = min(p25(trailing 15m), p25(trailing 60m))` of
  per-minute traded notional (`buy_total_notional_usd + sell_total_notional_usd`),
  over minutes STRICTLY BEFORE the fire minute (`RANGE ... AND 1 MINUTE PRECEDING`,
  the same boundary the W and B windows use). p25 (not median/mean) because sizing
  should lean on flow we can rely on, not flow inflated by bursts; the min of the two
  horizons is the pessimistic read.
- The fire minute's own traded notional is a DIAGNOSTIC only. It is not known at
  `decision_at` and is never used to size or gate.

## Dynamic sizing (frozen)

```
deliverable_notional = min(target_notional, participation_cap * conservative_trailing_flow_usd)
tradeable            = deliverable_notional >= min_economic_notional_usd
```

## Frozen parameters

| parameter                             | value              | status                                                               |
| ------------------------------------- | ------------------ | -------------------------------------------------------------------- |
| participation_cap                     | 0.10               | CANDIDATE (market-impact rule of thumb; real ceiling from L2 shadow) |
| min_economic_notional_usd             | 50                 | CANDIDATE (old real-money mechanics floor; needs owner sign-off)     |
| max_notional_usd                      | 1500               | from ECONOMICS.md (300 USD bank at the 5x ceiling)                   |
| target_notionals_usd (capacity curve) | 50, 100, 300, 1500 | frozen curve points                                                  |
| round_trip_cost_bps                   | 22                 | from ECONOMICS.md (the after-cost bar the edge must clear)           |
| concurrency / total margin            | NOT SET            | OPEN, must be fixed before any portfolio PnL read                    |

`participation_cap` is the ONE primary cap registered; other caps and sizes are
reported only as sensitivity, never promoted after the fact.

## Stop / continue criterion (frozen, evaluated on the returns pass)

Continue to a prospective cohort plus a minimal event-driven top-of-book/L2 shadow
only if, for at least one pre-registered target size, ALL hold:

1. after-cost net EV per execution is positive against the 22 bps round-trip bar;
2. the tradeable fire rate still clears the contract diversity floor
   (>= 4 fully-covered weeks each >= 20 fires, or its calibration-window rate proxy);
3. break-even slippage (the extra cost the edge absorbs before net EV crosses zero) is
   above a plausible modeled impact at the delivered size.

If no pre-registered variant clears all three, close the direction on economic
non-viability and move to HYP-024, per ECONOMICS.md.

## Sequencing (outcome-blind now; returns read gated)

1. OUTCOME-BLIND now: the capacity curve (fire rate, tradeable rate, delivered
   notional, assets, full weeks after dynamic sizing) across the target sizes. Reads
   no return. This is the committed evidence artifact.
2. GATED returns read (after this pre-registration is approved and the report-metrics
   gate lands): net EV after fees, break-even slippage, portfolio PnL in dollars for
   the 300 USD bank, split by venue and primary.
3. Decision by the criterion above.
