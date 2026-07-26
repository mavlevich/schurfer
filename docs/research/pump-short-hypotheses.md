# Pump-short hypotheses register

Observations and hypotheses about the pump-short strategy. The first eight paper trades
(through 2026-07-21) are the discovery sample that generated these hypotheses; they do
not count toward confirmatory evidence. Before evaluating a challenger, lock its exact
experiment manifest and confirmation-cohort start in this file. This prevents fitting
an explanation to whatever the numbers happen to show.

Rules:

- The locked go/no-go thresholds, cohort boundary, multiple-comparison rule, temporal
  invariants, and reproducibility requirements live in
  [episode replay protocol v1](episode-replay-protocol-v1.md). If this register and the
  protocol differ, the protocol governs confirmatory claims.
- An observation is a fact from data or code. A hypothesis is a claim we have not
  proven. Keep them separate.
- Do not change production config on a hypothesis. Test it as a virtual challenger on
  the same recorded decisions first (see the outcome resolver / virtual-variants work).
- Decisions about a new champion are made on **eligible** situations (candidates that
  passed the gates), not on all decisions. A first directional look is allowed at 50
  eligible episodes; the first formal analysis is locked to 100 eligible episodes and
  the diversity/CI rules in the protocol. Current sample (8 paper trades) is anecdote.
- **Cohort cutoff:** the first eight paper trades and all decisions through
  `2026-07-25T23:59:59.999999Z` are discovery-only. The first confirmatory cohort
  starts at `2026-07-26T00:00:00Z`. Each experiment manifest also locks its exclusive
  data cutoff before outcomes are queried.
- **Unit of inference:** a token-episode (the direct `pump_event_id` foreign key), not
  an individual decision or trade. Replay all decisions inside an episode
  chronologically so each variant still observes the real score/gate/seen-TTL
  sequence, then aggregate outcomes and confidence intervals by episode. Repeated
  decisions are not independent N; repeated episodes of the same asset are clustered
  as specified by the protocol.
- **Status:** the generic outcome windows are locked by `forward_v1` (15m, 30m, 1h,
  4h, 8h, 24h, 72h, 7d). The baseline selection, costs, and exit simulation are locked
  by `virtual_strategy_report_v2`. The HYP-002 entry family below is locked by
  `entry_confirmation_family_v1`. Other experiment details remain intent until their
  own manifests are committed.

Current baseline (as deployed, `pump_short_v1`): `PUMP_MIN_PCT=30`, `SCORE_THRESHOLD=6`,
`REQUIRE_RED_CANDLE=false`, `MIN_RETRACE_PCT=0`, leverage 3x, fixed $50 notional; exits
scale with pump size (initial SL 8-12%, trail activation 8-15%, trail 12-20%, max hold
180-360 min). See `docs/strategies/pump_short_v1.md` for the full description.

The execution-safety successor `pump_short_v1_market_quality` does not claim a new
alpha model: it keeps the v1 signal/entry rules but removes mechanically untradeable
books using the recorded spread, two-sided depth, and VWAP impact. Analyze its eligible
cohort separately from the original v1 cohort.

---

## OBS-001 — exit asymmetry: full SL on losers, undershoot on winners

Fact, from `app.trades.notes` over 8 paper trades (Jul 18-21): losses were 100%
`initial_sl` (-9.05 / -11.28 / -11.10%); wins were `max_hold` timeouts (~+4%) or
`trailing_stop` exits that gave back part of the move. Gross price-return expectancy was
~-1.4%/trade and gross profit factor ~0.64 before fees, funding, and slippage; breakeven
win rate was ~72% vs actual 62.5%. The existing notes do not preserve best price, so MFE
cannot be reconstructed from these eight trades and must come from the outcome resolver.

### HYP-001a — a no-progress timeout beats a fixed clock

Replacing `max_hold` (time since entry) with a no-progress timeout (exit only if no new
favorable extreme in N minutes) keeps a still-falling short open and improves captured
move without raising the loss rate.

### HYP-001b — protecting breakeven after activation raises net expectancy

After the trail activates, moving the stop to no worse than breakeven + costs turns the
current "gave it all back" trailing exits into small wins, improving expectancy.

### HYP-001c — partial take-profit + a runner captures the tail

Closing 30-50% at a modest target and trailing the rest wider banks the early favorable
move (mean-reversion reliability is itself unproven — that is what we are testing) while
still capturing the continuation that the current exit misses.

Experiment (all of OBS-001): replay v1 vs challengers on identical decisions.
Primary metric: net expectancy after fees/funding/slippage.
Secondary: captured_move (realized / MFE), average loss, max drawdown, win rate.

---

## OBS-002 — entry confirmation is disabled while score rewards near-peak price

Fact (prod env verified): `REQUIRE_RED_CANDLE=false`, `MIN_RETRACE_PCT=0`,
`SCORE_THRESHOLD=6`, and the retrace-from-peak score component tops out (its max points)
while the price is still near the peak. So a passing score can fire while the pump is
still running, with no reversal confirmation.

### HYP-002 — entry confirmation cuts the initial-SL rate

Requiring a closed red candle and a minimum retrace before entry lowers the rate of
shorting a still-continuing pump, and therefore the `initial_sl` rate, improving net
expectancy after costs.

Registered experiment family (`entry_confirmation_family_v1`), committed before its
first query:

- confirmation cohort starts at `2026-07-29T00:00:00Z`;
- baseline is `pump_short_v1_replay_v1`;
- `entry_red_candle_v1` requires a red last closed candle;
- `entry_retrace_1_5_v1` requires at least a 1.5% close retrace from the six-bar high;
- `entry_red_candle_retrace_1_5_v1` requires both conditions on the same candle;
- each variant may wait from zero through 60 minutes in 5-minute steps;
- every check uses six complete 5-minute candles and a one-full-bar execution gap, so
  a candle closing at the entry timestamp is never used to obtain that timestamp's
  open;
- the entry price is the selected future bar open, while exit bands, costs, exact
  venue, within-bar policy, and baseline episode selection remain unchanged;
- decision-time bid/ask impact is held constant across the paired variants because a
  historical order book at the delayed entry cannot be reconstructed; this controls
  the comparison but does not prove delayed-entry executability;
- the selected baseline episode remains eligible throughout the wait; score,
  market-quality, and balance gates are not reconstructed at the delayed timestamp,
  so this experiment isolates confirmation timing and is not an end-to-end production
  strategy replay;
- no confirmation within 60 minutes is a valid zero-return cash episode, not a missing
  result, and records a censored effective wait of 60 minutes;
- a missing candle, venue path, or cost input is unresolved and excluded from the
  paired comparison rather than silently treated as no entry.

The three challengers are one multiple-comparison family. Their formal inference uses
the exact first 100 chronological eligible episodes, at least 30 asset clusters,
10,000 deterministic whole-cluster bootstrap iterations, null-centered paired tests
with Holm correction at family alpha 0.05, conservative 98.333...% Bonferroni paired
intervals, and leave-one-out sensitivity for the five most frequent clusters. Formal
intervals and verdicts are withheld until the locked sample is completely resolved.
Passing these checks creates a live-shadow candidate only; it does not alter production
entry settings.

Primary metric: net expectancy after costs.
Secondary: initial-SL rate, average loss, MFE, missed winners, eligible-entry count.

---

## HYP-003 — the 30% pump threshold is unmeasured

`PUMP_MIN_PCT=30` is a heuristic. Important asymmetry: the scanner only records
candidates at or above 30%, so recorded decisions can test **raising** the effective
threshold (e.g. 35 / 40 / 50%) — does concentrating on stronger pumps improve
expectancy? — but they **cannot** test lowering it below 30%, because there is no data
there. Testing a lower floor should **not** be done by lowering `PUMP_MIN_PCT` — that
changes the baseline itself. Instead, later separate a **measurement floor** (e.g. 20%)
from the **strategy entry floor** (30%): record 20-30% candidates as skips so the data
exists, without opening them on v1. Do not change the live entry floor until the upward
sweep on recorded decisions shows a clear direction.

---

## HYP-004 — conviction-based sizing (fractional Kelly) once the score is calibrated

Sizing notional up when the expected edge is higher is optimal in principle (Kelly: bet
in proportion to edge). It is premature now: it requires a
**calibrated** mapping from score to win probability, which we do not have. Sizing on an
uncalibrated score amplifies losses on false confidence, and full Kelly is too
aggressive for fat-tailed pump moves. Gated on (a) proven edge, (b) score calibration
from the decision-quality analysis by score bucket; then use fractional (e.g. 1/2 or
1/4) Kelly. With the current fixed notional, leverage does not scale dollar P&L; it
changes required margin, margin ROE, and liquidation distance. Choose leverage only
after notional sizing, as a risk/implementation constraint. Belongs to Phase 4.

---

## HYP-005 — a concentrated blow-off mean-reverts differently than a grind

Example (ERA): the pump ran on two sharp 15-minute candles, then a single candle
retraced almost the whole move at once. This example contains two potentially distinct
signals: concentrated pump formation (blow-off versus grind) and the strength of the
closed reversal candle. They must be measured separately so one is not credited for the
other.

Second observation (`草根文化`, Gate, 2026-07-22): a single-venue market produced
successive 15-minute expansion candles, a 24h move peaking near +235%, spread snapshots
of 983.61 and 108.72 bps, and insufficient visible 50-level depth to fill $100 on
either side. A short decision at 0.0003 had raw forward returns of -6.2% / -63.3% /
-120.0% at 15m / 30m / 60m and 151.3% MAE; a later decision at 0.0005046 still had
49.4% MAE. This is a manipulation-like/illiquidity observation, not proof of intent.
It demonstrates that reversal probability and mechanical tradability must be modeled
as separate dimensions: a token may eventually mean-revert and still be ineligible.

Features to derive retrospectively for every decision from OHLCV candles fully closed
by the decision timestamp (ATR-normalized so tokens stay comparable): candle body/range
vs prior ATR, volume z-score against a prior window, upper-wick-to-range ratio, pump
concentration (share of the positive move in the largest 1-2 candles), bearish reversal
body/ATR, and the share of the pump returned by the last closed candle.

Hypothesis: blow-off concentration and reversal strength each separate short outcomes,
and their interaction may be stronger than either alone. If confirmatory analysis shows
stable separation, one or both can become an entry gate or score component.

Experiment: derive the input features only from the pre-decision window, then split
post-decision outcomes across a 2x2 view (blow-off/grind x strong/weak reversal).
Primary metric: net expectancy and captured_move by pre-registered buckets.

Do not build a candle-anomaly detector as a live signal before this split shows the
separation.
