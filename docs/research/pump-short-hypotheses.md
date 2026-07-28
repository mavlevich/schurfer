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

Current baseline (as deployed, `pump_short_v1`): `PUMP_ENTRY_MIN_PCT=30`,
`SCORE_THRESHOLD=6`, `REQUIRE_RED_CANDLE=false`, `MIN_RETRACE_PCT=0`, leverage 3x,
fixed $50 notional; exits scale with pump size (initial SL 8-12%, trail activation
8-15%, trail 12-20%, max hold 180-360 min). See
`docs/strategies/pump_short_v1.md` for the full description.

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

Registered experiment family (`exit_policy_family_v1`), committed before its first
outcome query:

- the prospective cohort starts at `2026-07-29T00:00:00Z`;
- baseline is the locked production exit with its pump-size-specific initial stop,
  activation, trailing, tightening, and 180/240/360-minute maximum hold;
- `breakeven_after_activation_v1` keeps the baseline clock but, after activation,
  caps the trailing stop at the entry price adjusted for both taker fees,
  decision-time bid/ask impact, and accrued conservative funding;
- `no_progress_60m_step_0_5_extension_120m_v1` removes the baseline clock as an
  immediate exit, closes at a complete five-minute bar close after 60 minutes
  without a new favorable low at least 0.5% below the previous registered low, and
  has an absolute limit of baseline max-hold plus 120 minutes;
- `breakeven_no_progress_60m_step_0_5_extension_120m_v1` combines the previous two
  policies without changing their parameters;
- `recent_progress_30m_step_0_5_extension_60m_trail_5_v1` extends once for 60
  minutes only when the baseline trail has activated and a new favorable low at
  least 0.5% below the previous registered low was recorded within the last 30
  minutes at the baseline max-hold boundary. During the extension the trail tightens
  to 5%, and the position closes at the absolute extended boundary;
- a favorable low becomes observable only at that five-minute bar close. It cannot
  reset a timeout or authorize an extension earlier inside the bar;
- stop and activation ambiguity inside one bar remains
  `conservative_stop_first`. If activation and the cost-adjusted protective stop are
  both reachable in one bar, the replay exits at the protective stop rather than
  assuming unobserved continuation;
- all policies reuse the same point-in-time selected decision, next-complete-bar
  entry, exact venue, liquidity snapshot, fees, funding model, and candle path;
- every non-baseline policy has an explicit absolute hold limit. No challenger may
  keep a position open indefinitely;
- missing any candle through the longest required policy window leaves the episode
  unresolved for the paired family rather than shortening the path or substituting
  another venue.

The four challengers are one multiple-comparison family. Formal inference uses the
locked first 100 chronological eligible episodes, at least 30 asset clusters, 10,000
deterministic whole-cluster bootstrap iterations, Holm correction at family alpha
0.05, simultaneous Bonferroni paired intervals, and leave-one-cluster-out
sensitivity. A passing challenger becomes a forward shadow candidate only. It does
not change the production exit.

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
- a `pump_event` starts and remains live at the +20% measurement floor; all +30%
  crossings inside that event are one correlated inference unit, while baseline
  eligibility, signal age, and OI baseline begin at its immutable first
  `entry_qualified_at`. This conservative clustering rule is locked before the cohort
  begins and prevents a retrace/re-pump sequence from inflating N;
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

`PUMP_ENTRY_MIN_PCT=30` is a heuristic. Decisions recorded before the measurement-floor
split can test **raising** the effective threshold (e.g. 35 / 40 / 50%), but they cannot
test lowering it because observations below 30% were not collected.

Prospective HYP-003 collection uses `PUMP_MEASUREMENT_MIN_PCT=20` and keeps the strategy
entry floor at 30%. The scanner persists and privately publishes 20-30% candidates;
signal computation and execution record their point-in-time score, liquidity, and
outcomes under `pump_short_measurement_v1`. Execution independently enforces the 30%
hard floor before any order path. The public pump list and Telegram remain filtered at
30% and 60% respectively. The event stores `first_seen_at` for measurement and sets
`entry_qualified_at` once at the first observed +30% crossing. Once qualified, signal
age, OI baseline, and v1 replay boundaries use that second timestamp, preserving the
entry strategy clock. The parent `pump_event` remains open while the token stays at or
above +20%; repeated +30% crossings are intentionally kept in one correlated episode.
Do not combine the lower-floor measurement decisions with the HYP-002
`pump_short_v1_market_quality` confirmatory sample.

Registered experiment family (`entry_threshold_family_v1`), committed before its
first outcome query:

- the prospective cohort starts at `2026-07-27T07:00:00Z`; the first deployment
  smoke sample before that boundary is excluded;
- the baseline entry floor is 30%; registered challengers are 20%, 25%, 35%, 40%,
  and 50%;
- the observation universe is every complete +20% measurement `pump_event` beginning
  at or after the cohort boundary, including events that never reach +30%;
- for each floor, select the first chronological recorded decision whose point-in-time
  `pump_pct` reaches that floor and whose recorded score and market-quality snapshot
  pass the same production gates; never use a later outcome to select an entry;
- no qualifying decision is a valid zero-return cash episode, not missing data;
- a selected decision enters at the next complete 5-minute bar open on its recorded
  exact venue and reuses the locked production exit, fee, funding, and decision-time
  liquidity-slippage models;
- missing exact-venue candles, decision-time costs, or required forward outcomes stay
  unresolved and are never replaced inside the locked formal sample;
- all six floors use the same parent `pump_event_id` observation unit. Repeated
  decisions and repeated +30% crossings inside one event do not inflate N;
- the five challengers form one multiple-comparison family. Formal inference uses the
  first 100 chronological eligible episodes, at least 30 asset clusters, 10,000
  deterministic whole-cluster bootstrap iterations, 95% expectancy intervals,
  null-centered paired tests with Holm correction at family alpha 0.05, conservative
  99% Bonferroni paired intervals, and top-five cluster sensitivity;
- a challenger must have positive own expectancy, a positive familywise paired lower
  bound versus 30%, a Holm-rejected paired null, and positive top-cluster sensitivity
  to become a live-shadow candidate. It does not change production configuration or
  authorize real trading.

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

Position sizing, leverage, and adding to a position are separate experiments:

- size notional from a conservative lower-bound edge estimate, available liquidity,
  per-episode risk, portfolio heat, and strategy drawdown;
- choose the lowest leverage that satisfies the margin budget and liquidation buffer.
  Never treat higher leverage as evidence of a better strategy;
- test any scale-in policy as a locked tranche state machine with one maximum
  episode-loss budget. Every tranche needs a fresh spread/depth/impact check. Do not
  average into an adverse move merely because price moved against the first entry;
- evaluate funding, OI, long/short ratios, liquidations, acceleration, and
  cross-venue confirmation first as point-in-time regime or entry features. They may
  affect eligibility or calibrated edge only after out-of-sample evidence, not act as
  arbitrary multipliers for size or leverage.

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

Registered feature contract (`candle_anomaly_features_v1`), frozen before the first
HYP-005 report query:

- the research cohort starts at `2026-07-29T00:00:00Z`; earlier episodes remain
  implementation and discovery data;
- use the selected baseline decision's exact recorded venue and 5-minute OHLCV;
- the feature cutoff is the latest candle close at or before the decision timestamp.
  Use the preceding 288 fully closed bars as the 24-hour formation window plus 48
  earlier bars as a four-hour warm-up. A missing, duplicate, misaligned, or invalid
  required candle makes the episode unresolved;
- positive move concentration is calculated from positive close-to-close log returns
  inside the formation window. `top_1_positive_move_share` and
  `top_2_positive_move_share` divide the largest one or two positive moves by the sum
  of all positive moves. No positive move makes concentration unavailable;
- true range uses the prior close. Each candle body and range is normalized by the
  simple mean of the 14 true ranges immediately preceding that candle, never an ATR
  containing the candle itself;
- volume z-score uses the 48 volumes immediately preceding each formation candle with
  population standard deviation. Zero variance produces z-score zero. Missing volume
  makes only volume-derived fields unavailable and is reported explicitly; it does
  not erase valid price-derived features;
- upper wick share is `(high - max(open, close)) / (high - low)` for the formation
  candle with the largest bullish body/ATR. A zero-range candle has zero wick share;
- returned-pump share divides the distance from the last close back to the formation
  peak high by the distance from the formation start close to that peak. The equivalent
  formula is `(peak_high - last_close) / (peak_high - start_close)`, where both aliases
  refer to the formation window. It is unavailable when the denominator is not
  positive and is not clipped, so overshoots remain visible;
- classify `blow_off` when top-two positive-move share is at least 60% and the largest
  bullish body is at least 3 prior ATR; otherwise classify `grind`;
- classify `strong_reversal` when the last closed candle has a bearish body of at
  least 1 prior ATR and has returned at least 35% of the formation run-up; otherwise
  classify `weak_reversal`;
- report the four pre-registered cells, feature coverage, net virtual expectancy,
  captured move, MFE, MAE, initial-stop rate, and asset-cluster concentration.
  The first report is descriptive and cannot promote a feature into production.
  Any proposed gate or score weight must be frozen as a concrete challenger and
  validated on a new out-of-sample cohort.

Do not build a candle-anomaly detector as a live signal before this split shows the
separation.

---

## HYP-006 - score 6 may be over-selective

The discovery-only decision-quality report compares score policies on historical
episodes and can suggest where the cutoff may be wrong. It cannot validate a new
cutoff on the same data. This experiment therefore freezes a small prospective
family before querying its outcomes.

Registered contract (`score_threshold_downward_family_v1`):

- the untouched confirmation cohort starts at `2026-07-31T00:00:00Z`;
- use only `pump_short_v1_market_quality` decisions whose parent episode is eligible
  under the replay protocol and has a complete exact-anchor 8-hour outcome;
- keep score 6 as the baseline and compare score 4 and 5 as one registered family.
  Do not add, remove, or tune thresholds after the cohort begins;
- for each policy, select the first chronological recorded decision whose score
  reaches the threshold and whose recorded market-quality requirement passes. Use
  only the decision-time config and liquidity snapshot;
- a threshold never reached contributes a zero-return cash episode;
- enter at the next complete exact-venue 5-minute open and reuse the locked baseline
  exit, taker-fee, funding, and decision-time liquidity-impact models. A missing
  selected-decision path or cost input remains unresolved;
- all policies share one `pump_event_id` observation. Repeated decisions do not
  increase N, and repeated episodes of the same asset remain one bootstrap cluster;
- formal inference uses the first 100 chronological eligible episodes, at least 30
  asset clusters, 10,000 deterministic whole-cluster bootstrap iterations, ordinary
  95% expectancy intervals, Holm correction at family alpha 0.05, conservative
  97.5% Bonferroni paired intervals, and top-five cluster sensitivity;
- a challenger can become a live-shadow candidate only when its own 95% expectancy
  lower bound is positive, its paired 97.5% lower bound versus score 6 is positive,
  the Holm-adjusted paired test rejects at family alpha 0.05, and its minimum
  leave-one-top-cluster-out expectancy remains positive;
- this report cannot change `SCORE_THRESHOLD`, place an order, or authorize real
  trading. Any passing policy needs a new live-shadow and out-of-sample cohort.

Score 7 and 8 are intentionally outside this family. The baseline execution stops
recording later decisions after it opens at score 6 or 7, so stricter thresholds are
right-censored on exactly the episodes needed for a fair comparison. Requiring all
four original arms to resolve would make the locked first-100 sample potentially
impossible to complete. Test score 7 and 8 in the live multi-variant shadow evaluator,
where every policy keeps isolated state after the baseline opens.
