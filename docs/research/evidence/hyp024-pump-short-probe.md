# HYP-024 pump-short probe (EXPLORATORY, reads outcomes)

Companion to `hyp024-pump-short-probe.json` (fingerprint `83e0bea6...`, source parquet
sha256 `cbb72f01edcf...`). A signal-of-life grid scan for the OPPOSITE side of the
accumulation long: after a short-term run-up plus an activity blow-off, SHORT the
exhaustion and measure the forward reversal. It READS RETURNS and scans a threshold
grid, so it is not outcome-blind and NOT a verdict; it is the fast test of whether the
short side is worth formalizing.

## Method

- Pump minute `t`: `ret15 = close(t)/close(t-15) - 1 >= up_thresh` AND
  `spike = activity(t) / mean(activity over [t-60, t-1]) >= spike_mult`.
- Entry: short at `close(t)` (detection completes at end of minute t). Exit:
  `close(t+H)`. One short per instrument per 24h. Entry and exit must be price_complete.
- Short net = `(entry - exit)/entry - 22 bps`. All instruments in the window (not just
  accumulation fires). 11,795 loose candidate minutes before the grid.

## Result (net of 22 bps, best-first)

| up   | spike | hold | n    | mean net | median net | win   |
| ---- | ----- | ---- | ---- | -------- | ---------- | ----- |
| 0.12 | 8     | 240  | 203  | +4.40%   | +6.16%     | 74.4% |
| 0.12 | 5     | 240  | 270  | +2.88%   | +5.49%     | 70.0% |
| 0.12 | 3     | 240  | 315  | +2.69%   | +5.23%     | 70.8% |
| 0.08 | 8     | 240  | 504  | +1.83%   | +3.73%     | 68.5% |
| 0.08 | 3     | 240  | 703  | +1.47%   | +2.93%     | 65.4% |
| 0.05 | 8     | 240  | 1160 | +0.99%   | +2.30%     | 63.8% |
| 0.05 | 3     | 30   | 1579 | -0.21%   | +0.48%     | 55.8% |

(full 36-cell grid in the JSON.)

## Reading

Net short EV is positive across almost the entire grid and rises MONOTONICALLY with
run-up size, blow-off size, and hold length. That dose-response (stronger pump ->
bigger reversal) is much harder to explain as noise than any single cell, and win
rates run 58-74%. It mirrors the accumulation long, which lost 0.80% at 42.5% win:
the exploitable move is the reversal short, not the accumulation long.

This matches the pump anatomy the owner flagged (LSK: +67% run then -13% in two hours;
LONGXIA / 龙虾, not in our captured universe).

## Caveats (why this is signal-of-life, not money yet)

- EXPLORATORY grid scan (multiple comparisons). The monotonic dose-response is the
  reassurance; a pre-registered threshold + hold and an out-of-sample/leave-one-out
  read are still required.
- Cost model is only the 22 bps round-trip. Shorting pumped small-caps has EXTRA costs
  not modeled here: perp FUNDING (can be extreme during a pump), borrow availability,
  and WORSE slippage/executability on exactly the thin tokens the accumulation
  liquidity probe already flagged. The same participation/liquidity gate must be
  applied to short candidates, and short-specific costs added, before the dollars are
  believable.
- These are the same illiquid instruments: n here is pre-liquidity-filter.

## Next

1. Apply the executability/participation gate to the short candidates (can we short
   ~300 USD into these blow-offs without dominating the book?).
2. Add short-specific costs: funding and a slippage assumption; recompute net EV and
   break-even slippage.
3. Pre-register one threshold set + hold, then an out-of-sample read. If it survives,
   HYP-024 becomes the active candidate over the accumulation long.
