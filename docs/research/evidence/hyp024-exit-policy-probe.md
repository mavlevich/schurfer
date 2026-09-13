# HYP-024 pump-short: exit-policy comparison (EXPLORATORY)

Companion to `hyp024-exit-policy-probe.json` (fingerprint in the JSON, source parquet
sha256 `cbb72f01edcf...`). Tests whether a dynamic exit beats the fixed hold for the
pump-short, on one pre-chosen entry rule (up>=0.08, spike>=3, 710 deduped entries).
READS RETURNS; not a verdict.

## Hard data caveat

The subset has only per-minute close (no high/low). Every exit is evaluated at MINUTE
CLOSES, so intrabar stop/TP fills and gap-through are invisible. This makes backtested
STOPS OPTIMISTIC (a real high would trigger a short's stop-loss more often than a
close does). So the finding "stops do not help" is if anything understated; a faithful
stop backtest needs OHLC or L2.

## Result (short, net of 22 bps)

| policy        | n   | mean net | median net | win   |
| ------------- | --- | -------- | ---------- | ----- |
| fixed 60m     | 710 | +0.58%   | +1.86%     | 63.5% |
| fixed 120m    | 710 | +1.12%   | +2.53%     | 64.5% |
| fixed 240m    | 710 | +1.48%   | +2.93%     | 65.5% |
| tp+3% / sl-3% | 710 | -0.25%   | +2.03%     | 50.3% |
| tp+5% / sl-3% | 710 | -0.00%   | -3.29%     | 44.4% |
| tp+5% / sl-2% | 710 | -0.27%   | -2.46%     | 36.1% |
| trailing -2%  | 710 | +0.36%   | +0.17%     | 51.8% |
| trailing -3%  | 710 | +0.42%   | -0.04%     | 49.7% |

## Reading

Counterintuitively, the fixed longer hold beats every TP/SL bracket and trailing stop
tested. Two mechanisms:

- The pumped token whipsaws hard on the way to reversal, so a stop-loss on the short is
  shaken out on a local bounce before the reversal pays. On real high/low this is worse.
- The edge lives in the fat right tail (big reversals: median winner ~2.9%, many
  larger). A take-profit caps exactly those winners, so capping upside while stopping
  downside destroys the edge (tp5_sl2 falls to a -2.46% median, 36% win).

So the pump-short is a patient mean-reversion trade, not a trailing/scalping trade. The
real levers are entry selectivity (dose-response), position sizing / executability, and
short-specific costs (funding, borrow) - not a tight exit. Risk control is better served
by many small diversified shorts (65% win, law of large numbers) plus a wide
catastrophic-only stop than by a tight stop that kills the edge.

## Not tested here

- Signal-based exit (close when activity/net_buy normalizes) - a real candidate, needs
  the orderflow features on the path.
- Holds beyond 240m, and wide catastrophic stops - need more forward data and OHLC.
- Everything remains pre-liquidity-filter and pre-funding-cost, and exploratory (single
  window, one entry rule, no out-of-sample).
