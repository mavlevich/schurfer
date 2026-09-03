# Pump analytics v2-v9 archive

Status: historical discovery notes; not a reproducible report and not trading
evidence.

The one-off scripts originally added on `research/pump-analytics` are preserved
in git history at commits `4f5d5b5` and `53082d3`. They were removed from the
working tree because they bypassed the repository's current report contracts:
most had no immutable manifest, point-in-time universe, input fingerprint,
matched control, family correction, or explicit discovery/confirmation cutoff.
Keeping them runnable beside production reports would make it too easy to treat
an already-viewed screen as fresh evidence.

## Findings worth retaining

- Minute-level standalone taker flow was economically flat before costs and
  negative after the frozen 25 bps round-trip cost assumption. The v3-v6
  iterations changed denominator and episode construction, but did not produce
  a standalone edge.
- The v7 shadow cut suggested that the hand-picked moderate 15-minute buy-flow
  filter could be harmful: its observed win rate fell materially versus the
  unfiltered detector. Low or sell-heavy pre-flow looked better descriptively.
  This window was already viewed, so that observation cannot be promoted by
  retesting another threshold on the same rows.
- Two-minute OI growth in v9 ranked continuation episodes better than falling
  OI in relative terms, but the reported candidate still remained negative
  after costs (about -0.12% mean net, profit factor about 0.71).
- Negative funding, time-of-day, lifecycle, liquidation-fuel and first-minute
  buy-ratio cuts were descriptive only. Their raw group means were exposed to
  selection, overlap, unequal token mix and outlier distortion; none is a
  registered strategy candidate.
- The 100% Binance-leads-Bybit pump result reflected differing source timestamp
  semantics, not credible eight-second exchange latency. It must not be used as
  lead-lag evidence.
- Only 28 of 8,842 pump events joined to nearby liquidation rows in the first
  liquidation-fuel attempt. That was primarily a coverage/join diagnostic, not
  evidence that liquidations do or do not fuel pumps.

## Replacement

The maintained replacement is the CEX activity discovery report introduced on
this branch. It starts from the existing full-universe bidirectional burst
study, requires continuous 5-minute and trailing 24-hour inputs, resolves an
exact native-market 24-hour path from the next bar, and compares each signal
with a same-instrument/same-UTC-time quiet control. Its viewed window is
discovery-only and can nominate at most one frozen direction for a later
untouched forward shadow cohort.
