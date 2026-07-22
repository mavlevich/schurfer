# Pump-short hypotheses (pre-registered)

Observations and hypotheses about the pump-short strategy, written down **before**
running the analysis that tests them. Pre-registration is the point: it stops us from
fitting an explanation to whatever the numbers happen to show, and from tuning
production on a handful of trades.

Rules:

- An observation is a fact from data or code. A hypothesis is a claim we have not
  proven. Keep them separate.
- Do not change production config on a hypothesis. Test it as a virtual challenger on
  the same recorded decisions first (see the outcome resolver / virtual-variants work).
- Decisions about a new champion are made on **eligible** situations (candidates that
  passed the gates), not on all decisions, and only after enough of them: a first look
  at ~50, a champion switch at ~100-200. Current sample (8 paper trades) is anecdote.
- **Cohort cutoff:** fix the analysis cohort to decisions on or after a stated start
  date (earliest: when the durable outbox shipped, so the dataset is complete), and
  write the cutoff down before looking at results. Do not extend the window after seeing
  them.
- **Unit of analysis:** one row per token-episode, not per decision — a token that
  re-triggers later is a separate episode, and the per-token `seen` debounce means one
  episode should not be counted many times. Aggregate at the episode level.
- **Status:** this is a hypotheses register. The exact outcome windows and the
  taken-vs-skipped join are locked when the outcome resolver is built; until then, treat
  the experiment specs below as the intent, not a frozen protocol.

Current baseline (as deployed, `pump_short_v1`): `PUMP_MIN_PCT=30`, `SCORE_THRESHOLD=6`,
`REQUIRE_RED_CANDLE=false`, `MIN_RETRACE_PCT=0`, leverage 3x, fixed $50 notional; exits
scale with pump size (initial SL 8-12%, trail activation 8-15%, trail 12-20%, max hold
180-360 min). See `docs/strategies/pump_short_v1.md` for the full description.

---

## OBS-001 — exit asymmetry: full SL on losers, undershoot on winners

Fact, from `app.trades.notes` over 8 paper trades (Jul 18-21): losses were 100%
`initial_sl` (-9.05 / -11.28 / -11.10%); wins were `max_hold` timeouts (~+4%) or
`trailing_stop` exits that gave back most of the move (PONS pumped ~130%, exited +5.8%
on a 20% trail, so MFE was ~+25%). Expectancy ~-1.4%/trade, profit factor ~0.64,
breakeven win rate ~72% vs actual 62.5%.

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

Experiment: champion `pump_short_v1` vs challenger `pump_short_v2_entry_confirm`
(`REQUIRE_RED_CANDLE=true`, `MIN_RETRACE_PCT≈1.5`), replayed on identical decisions,
including skipped ones.
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

Sizing position and/or leverage up when the expected edge is higher is optimal in
principle (Kelly: bet in proportion to edge). It is premature now: it requires a
**calibrated** mapping from score to win probability, which we do not have. Sizing on an
uncalibrated score amplifies losses on false confidence, and full Kelly is too
aggressive for fat-tailed pump moves. Gated on (a) proven edge, (b) score calibration
from the decision-quality analysis by score bucket; then use fractional (e.g. 1/2 or
1/4) Kelly. Note: leverage scales return and risk together, so it does not change
whether an edge exists — it is a sizing decision to optimize last, not first. Belongs to
Phase 4 (risk engine / sizing + regime).
