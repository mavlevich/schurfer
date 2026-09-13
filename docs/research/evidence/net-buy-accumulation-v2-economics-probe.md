# net-buy accumulation v2: exploratory economics probe (LONG, 240m)

Companion to `net-buy-accumulation-v2-economics-probe.json` (fingerprint
`4a4ee9d3...`, source parquet sha256 `cbb72f01edcf...`). EXPLORATORY: it READS RETURNS,
so it is not outcome-blind and NOT the frozen formal verdict. It is a fast go/no-go on
whether the executable accumulation-long fire set has any after-cost edge at the
contract's 240-minute hold, before investing in the formal report machinery.

## Method

- Fire set: v2 eligibility at theta=0.25, deduped, availability-OFF (on/off parity is
  delta 0). Tradeable filter = the frozen dynamic sizing (`min(300, 0.10 * p25 trailing
flow) >= 50 USD`).
- Entry (economic proxy, rev.7): `close(t)` of the fire bar (approximates the open of
  the first bar after `decision_at`). Exit: `close(t+240)`. Diagnostic upper bound:
  `close(t-1) -> close(t+239)`. Long only. Net = gross - 22 bps round-trip.

## Result

| primary | tradeable fires | mean net | median net | win rate | portfolio $ PnL @300 |
| ------- | --------------- | -------- | ---------- | -------- | -------------------- |
| P-SHAPE | 80              | -0.80%   | -1.09%     | 42.5%    | -133                 |
| P-MAG   | 15              | +0.25%   | -2.90%     | 33.3%    | +34                  |

(economic entry; the diagnostic close(t-1) entry is within ~0.1pp, so the entry-timing
artifact is small at this hold.)

## Reading

The accumulation LONG as specified (240m hold) has NO positive after-cost edge on the
executable set. P-SHAPE, the only primary with a diversity-clearing tradeable fire set,
loses 0.80% per trade with a 42.5% win rate (-133 USD over 80 trades at 300 USD size).
P-MAG's slightly positive mean is noise: the median is -2.9%, the win rate 33%, and n is
15 (and P-MAG fails the diversity floor at 4.4 fires/week anyway).

This matches the anatomy of pumps like LSK (bybit LSKUSDT, 0.074 -> 0.123 -> 0.107 over
2026-09-09..10): ~24h of accumulation and a slow ~10% drift, then a blow-off that
reverses ~13% in two hours. A 240m long opened during accumulation sits straight into
the reversal and gives the gains back.

## Implication

The exploitable move is more likely the SHORT on the pump exhaustion/reversal than the
accumulation long at a 240m hold. That is HYP-024 (pump-short orderflow), whose
feasibility ceiling already passed. Next probe: short at the blow-off on this same data.

## Caveats

- Exploratory: one window, no uncertainty bands, no leave-one-out. P-SHAPE mean -0.80%
  is ~1.4 SE below zero (per-trade std ~5%): read as "no positive edge here", not a
  precise loss estimate.
- A shorter long hold (exit before the blow-off) is a different, untested treatment; it
  is not evidence for the 240m contract horizon.
