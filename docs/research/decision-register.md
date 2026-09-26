# Decision register

Canonical go/no-go state per research line. It records confirmed facts with their
references, one decision card per line (what we measure, when it can be read, what
continue means, what stop means), and which reads are available retrospectively now
versus which need an untouched forward cohort. It promotes nothing and changes no
production configuration.

The organizing principle (agreed in review) is decision triggers and stop criteria,
not a count of PRs: every line must say what it measures, when it becomes readable,
and what continue and stop each mean, so we do not spend months building good
infrastructure without approaching a trading decision.

**Scope and single source of truth.** This register holds DECISIONS and POLICY only.
The fast-path operational status lives in `ROADMAP.md` (Current focus); the two must not
disagree, and ROADMAP wins on status. Live operational readouts (running containers,
disk, "latest run") belong in a dashboard/runbook, not here; where such a fact is quoted
below it carries a `verified_at` date and its source, and is a point-in-time note, not a
maintained truth. A "continue" here means a candidate advances to its next registered
step; it never means a proven, promotable trading edge. Every metric is standalone
after-cost economics unless explicitly labelled a paired difference.

## Confirmed facts (with references)

- **HYP-012 cohort v2 registered; v1 closed (2026-09-26).** Qualification v4 selects the
  venue only among venues the owner can trade (`TRADABLE_VENUES = ("bybit",)`), on registry
  v4. The estimand is `standalone_early_entry_net_return_tradable_venue_v2`, and the cohort
  starts 2026-09-29T00:00Z. Every v1 evaluation rule, floor and cap is unchanged
  (`source-lead-forward-cohort-v2.md`). The v1 cohort (Binance, 15 of 100 episodes) is
  closed without a formal read. A `candidate` v2 verdict is necessary, not sufficient: it
  allows a broader confirmatory cohort and a separate, owner-approved live execution test on
  Bybit capped at USD 50, which measures real fills against the cost assumptions and does
  not establish the edge. Larger capital needs the confirmatory cohort.
- **HYP-012 v4 identity approval policy (2026-09-25).** Binance futures are not
  available to the owner (Poland), so v4 makes Bybit the primary execution venue and keeps
  Binance as a descriptive comparison (estimand registered in PR D). Identity for v4 is
  decided by one written rule (`source_lead_identity_rule_v4`, see
  `source-lead-identity-registry-v4.md`), fixed before any route was decided and applied
  per route to a hashed candidate snapshot (window end 2026-09-25T20:00:00Z). The v3
  checklist item "a second person independently confirmed the link" is replaced for v4:
  there is no second person, so the owner confirms the rule and the full decision list
  once, and the reviewer independently re-derives every approved route from the raw
  evidence. `build-registry` refuses without an approval that names the exact
  `decisions_sha256`.
  Result (2026-09-26): 272 candidates (window plus the 14 v3 assets), 544 route decisions,
  99 approved routes for 85 assets (Bybit 44, Binance 55), `decisions_sha256=c7f782ec...`.
  The reviewer re-derived all 544 decisions from the stored source snapshots with an
  independent script (0 mismatches). Registry v4 fingerprint `7d5f635a4ed0...`. Not active
  until PR D.
- **Abnormal-flow v1 is an inconclusive diagnostic result.** The prospective window ([2026-08-30T00:00:00Z, 2026-09-18T11:58:00Z)) was burned due to multiple reads and monkeypatched execution without the formal runner (`docs/research/evidence/abnormal-flow-v1/diagnostic-v1-burned/README.md`). Mean net +0.61%, excess +0.45%, resolved 125/252, missingness 50.4%, control coverage 53%, week concentration 52%, CI lower bounds negative, portfolio +$4.25 / maxDD $17.15. The result is non-promotional and v1 does not advance to live execution. Next step: post-hoc missingness/control-coverage diagnostic on the burned window without promotion claims.
- **Abnormal-flow v1 post-hoc follow-up (2026-09-25): the apparent edge was missingness
  selection; v2 is deprioritised.** The unresolved-reason taxonomy (#443) found that all 127
  unresolved primaries had complete entry and exit bars: v1 lost them only to its rule that
  all 721 path minutes be `price_complete`. The registered entry/exit-bars sensitivity
  (`abnormal-flow-v1-boundary-sensitivity.md`, interpretation fixed before the read) resolves
  251/252 episodes: mean net -0.06%, median -0.36%, 43% winners (gross +0.50% vs ~0.55%
  costs); the 126 newly resolved average -0.72%, so the earlier +0.61% came from a
  completeness-selected subsample. Portfolio net PnL is negative for every K = 1..20 and
  every scenario ($300 bank). Post-hoc on a burned window, not a formal FAIL; per the
  registered meaning it lowers the priority of an abnormal-flow v2. If the family is
  revisited, its contract must use a bounded entry/exit-bars path rule.
- **The executable pump-short is net negative.** `app.research_report_runs`:
  `liquid_taker_candidate_v1` = 802 eligible / 151 tradeable episodes, net expectancy
  -0.224%/episode, 95% CI [-0.455%, -0.0096%] (entirely below zero);
  `liquid_taker_wider_stop_shadow_v1` = paired delta +0.084%, CI [-0.045%, +0.228%]
  (crosses zero). Both `do_not_promote`. (verified_at 2026-09-13 from
  `app.research_report_runs`; these were the only confirmatory rows then.)
- **HYP-012 cross-venue source-lead is the strongest historical hint, NOT a proven
  edge** (`discovery-ledger.md` HYP-012; audit `docs/engineering/audits/2026-09-06/`).
  What is shown: on ticker-matched data (`identity_verified=false`) an EARLY entry beat a
  post-confirmation entry by +1.8 to +3.4 percentage points across four routes,
  Holm-significant. That is a PAIRED DIFFERENCE, not standalone profit: early -1% vs late
  -3% is the same +2pp and still loses money. Standalone after-cost PnL is unproven. The
  live forward test is a DIFFERENT, narrower estimand: one Gate to Binance branch,
  `source-lead-forward-cohort-v1.md`, 14 canonical assets, 100 eligible episodes / 7
  clusters / 4 UTC weeks. (My earlier "5 assets / 30 clusters" was wrong.)
- **HYP-015 hold12h paper worker is live since 2026-09-13** (contract_sha256 `f280bd14`,
  paper only). A gross recheck on 3610 ALREADY-VIEWED baseline probes showed 12h +0.199%
  vs 4h +0.012% (gross). This is EXPLORATORY only: it is a viewed window, cannot inform
  the verdict, and its future cohort must not overlap it. Note: the paper accounting
  models funding as a fixed 5 bps/8h prorated by hold, NOT actual per-coin settlement
  (`packages/performance/schurfer_performance/accounting.py`), so a 12h hold is a flat
  ~7.5 bps regardless of coin; a versioned actual-funding reconciliation is required.
- **Data is already rich.** The cold-bar export is `COPY (SELECT * ...)` of
  `bybit_momentum_bars_1m` (OHLC, bid/ask, microstructure, created_at); OI, funding, and
  liquidations are captured in their own tables. The local 11-column window parquet is a
  hand-cut subset, not the export.
- **#413 accumulation v2 is NOT frozen.** Its published calibration artifact leaves the
  `0.25/0.25/51d` selection open (Rule A stability, lag SLA, economics/MDE still pending),
  and the sizing pre-registration is a draft. A point-in-time capacity look showed P-SHAPE
  ~63% tradeable at a 300 USD target and P-MAG ~20% (no returns read), but neither the
  selection nor a "one pass then decide" is a registered decision yet.
- **Prod disk (verified_at 2026-09-13, `ssh schurfer df -h /` + `docker system df`).**
  Point-in-time note; authoritative liveness is the runbook/dashboard. `/` was at 69%;
  freeing 10.26GB docker build cache took it to 55%. Timeseries hypertables have Timescale
  retention plus compression; the unbounded growth is in plain `app` tables without
  retention, top being `pump_derivatives_context_samples` at 3.0GB.

## Running workers (verified_at 2026-09-14, `ssh schurfer docker ps`)

Point-in-time snapshot; authoritative liveness is the runbook/dashboard, not this file.
Then running: collectors (bars, OI, liquidations, funding), `momentum-watch`, and the
HYP-015 pair (`momentum_flow_paper_v1` baseline plus `hold12h`). All paper, no real
money. POLICY (the durable part): a worker is stopped only when its line is confirmed
dead AND it feeds no active read; negative paper P&L on a control or an active comparison
is the measurement, not a reason to stop it.

## Decision cards

### HYP-012 - cross-venue source-lead LONG (strongest historical hint)

The registered forward test (`source-lead-forward-cohort-v1.md`) asks a NARROWER, more
honest question than the historical 4-route paired difference: does the early Gate to
Binance entry earn STANDALONE positive net after costs on ALL eligible early events,
including those the target venue never confirmed (so we do not select winners)?

- **Measure:** standalone after-cost net return on eligible early Gate to Binance events
  (its registered primary estimand), plus the dollar path (signals/week, executable
  notional, fees/impact, delay-decay, capacity, expected PnL for the 300 USD bank).
- **When readable:** when the identity-verified forward cohort reaches the contract floor
  (100 eligible episodes / 7 clusters / 4 UTC weeks, concentration limits). At the current
  ~10 qualified/week over 14 canonical assets that is weeks-to-months, and only if
  identity coverage keeps admitting events (outcome-blind).
- **Continue:** standalone net-EV lower bound > 0 under leave-one-out and busiest-week
  exclusion, AND a viable after-cost dollar path at feasible capacity.
- **Stop:** standalone net EV not positive, or capacity/impact/delay makes the dollar
  path non-viable for the 300 USD bank.
- **Retro-now vs forward:** the standalone confirmation is forward-only (the frozen v1
  contract). The dollar-path/capacity/delay-decay READ on already-collected leads is
  retrospective-descriptive and can be produced now. The historical +1.8-3.4% is a paired
  difference and is NOT the standalone result.

### HYP-015 - momentum-flow hold12h (active candidate)

- **Measure:** paired net return (12h vs contemporaneous `momentum_flow_paper_v1`) on the
  SHARED WATCH population (unfilled/unresolved kept in the denominator), plus standalone
  after-cost net; MFE/MAE, initial-stop survival, capital occupancy, and signal conflicts.
  Funding must be the versioned actual-settlement reconciliation, not only the 5 bps/8h
  model, and entry-price differences between the two workers must be controlled so hold
  duration is not confounded.
- **When readable:** ONLY after a pre-registered sample floor plus reader, cost model and
  verdict logic are committed (they are not yet); the forward cohort must not overlap the
  already-viewed probes. No sample floor is registered today.
- **Continue:** standalone net EV and the paired delta both > 0 after actual funding on a
  mature, non-overlapping forward sample.
- **Stop:** standalone net EV not positive after funding, or the duration gain vanishes
  once entry-price and capital-occupancy confounds are controlled.
- **Retro-now vs forward:** forward-only for the verdict. The 12h-vs-4h gross recheck on
  viewed probes is EXPLORATORY and does not count. Freeze the reader now to prevent
  fitting it to accumulated results.

### #413 - accumulation v2 (open; contract not yet frozen)

Precondition: the calibration selection (`0.25/0.25/51d`) is NOT frozen (Rule A
stability, lag SLA, economics/MDE still open), and the sizing pre-registration is a
draft. So the pass below is EXPLORATORY discovery, not a registered go/no-go, until those
open items close and the sizing parameters are frozen.

- **Measure:** one exploratory sizing/economics look on the calibration window:
  dynamically-sized net EV after 22 bps, break-even slippage, dollar path for 300 USD,
  split by venue and primary, on the tradeable subset.
- **When readable:** the exploratory look is retro-now; a REGISTERED decision waits on
  freezing Rule A, lag SLA, economics/MDE and the sizing parameters first.
- **Continue:** only after freezing the above, if a pre-registered size shows positive
  after-cost EV at viable capacity, advance to a minimal liquidity shadow plus a
  prospective registration.
- **Stop:** after freezing, if no pre-registered size is economically viable,
  freeze/close the direction.
- **Retro-now vs forward:** the exploratory economics is retro-now and informs whether to
  spend effort freezing the contract; it cannot itself be the go/no-go.

### pump-short (executable) - CLOSED

Confirmed net negative (see facts). Closed. Do not reopen and do not tune entry on the
negative base (the multiple-comparison trap the ledger warns about).

### Universe coverage (enabler, not an edge)

- **Delivery:** [coverage architecture](../architecture/target-platform-v1.md#broad-market-coverage-and-bounded-enrichment)
  and the [conditional PR queue](../../ROADMAP.md#market-coverage-and-architecture-delivery--2026-09-14)
  separate broad observation, continuous baseline and selective enrichment. Existing
  candidate readings keep priority; expansion is not an execution promotion gate.
- **Measure:** the full eligible perpetual universe denominator with a deterministic
  onboarding timestamp; coverage versus where pumps actually occur (LONGXIA was not
  captured at all).
- **When readable:** the catalog audit is retro-now; onboarding is forward and
  deterministic (by pre-set rules, never by adding today's winners).
- **Continue/Stop:** an enabler that removes outcome-selection and coverage gaps, not a
  go/no-go edge of its own.

### Early-precursor family (future discovery)

- **Measure:** ONE point-in-time dataset/scanner feeding SEPARATE pre-registered
  hypotheses (net-buy accumulation, OI growth, source-lead), each with base rates
  P(pump | signal) and P(signal | pump). No execution change.
- **When readable:** base rates are retro-now (descriptive); any predictive claim needs
  its own untouched forward cutoff.
- **Continue/Stop:** per each pre-registered hypothesis's own criterion. The +20/30%
  scanner is a late reactive radar; earlier detection trades precision for earliness and
  must be validated on base rates, never tuned on winners.

### Abnormal-flow economic screen v1 (planned bounded discovery)

**Status: planned, not yet registered or run.** The owning sequence and project-level
budget are fixed in the [near-term edge decision program](../../ROADMAP.md#near-term-edge-decision-program--2026-09-15).
The next implementation PR may combine the frozen protocol with tested replay code,
but it must not contain a result produced by that code. This historical read is
discovery-only and can authorize at most one new prospective cohort.

- **Question:** on the existing exact-native Bybit/Binance research paths, does one
  predeclared abnormal-flow family predict an executable directional return beyond a
  matched baseline and also make standalone money after conservative costs?
- **Before any outcome read:** freeze one primary direction and horizon; feature and
  availability timestamps; eligible-universe denominator; episode formation and
  cooldown; entry/exit mechanics; gaps/unresolved handling; cost model; portfolio
  policy; outcome-blind threshold/calibration procedure; and a rule that can nominate
  at most one candidate. Source-lead remains HYP-012 and is not silently retested as a
  second primary family here. Absorption and raw buy-flow variants remain out of scope.
- **Required outputs:** full coverage/rejection funnel; standalone after-cost net EV;
  matched excess; uncertainty; resolved/unresolved counts; signals/week; asset/week
  concentration and leave-one-out controls; drawdown and losing streak; concurrency
  and capital occupancy; executable notional/capacity; and dollar paths at the current
  research bank plus plausible larger allocations. Report negative mature economics
  before diversity commentary.
- **Continue:** only if the frozen primary is positive on standalone after-cost
  economics and incremental excess, remains viable under the predeclared robustness
  and portfolio checks, and has an economically material capacity path. This earns one
  separately registered untouched prospective cohort, not PAPER or live trading.
- **Stop:** close this signal/data scope if standalone net is non-positive, the
  incremental effect disappears, portfolio risk is unacceptable, capacity makes the
  dollar ceiling immaterial, or opportunity throughput cannot support a decision by
  the program checkpoint. Do not retune the same viewed window.
- **Inference boundary:** a stop applies to the declared Bybit/Binance minute-bar
  mechanism. It is not evidence about uncollected venues or pre-event L2. Those remain
  untested, and may receive new collection only through the conditional coverage/L2
  gates in the roadmap rather than by treating missingness as a positive hypothesis.

### Monster-precursor discovery (next primary discovery line)

The core money question: catch the rare monster pumps (the ones that pay) and cut the
junk. Established on a pump-covering window (bybit/binance 1m bars Aug 10 to Sep 14,
pulled from the prod DB): monsters ARE in capture (forward-3d best LSKUSDT +2179%,
龙虾USDT / LONGXIA +377%, ~58 tokens above +100%); net-buy accumulation-LONG is
net-negative at every hold even with the pumps in window and does NOT separate monsters
from junk (so it is closed as a monster-catcher, see standing decisions); a first-cut
activity feature was ~11x higher for monsters but outcome-selected, so a candidate only.

- **Measure:** define move-onset per episode, then measure point-in-time features
  STRICTLY before onset over ALL episodes (monsters and matched controls); report
  precision/recall/lift. Candidate features: trailing activity/volume ramp, cross-venue
  breadth, OI growth, repeating net-buy bursts (structure not a single spike),
  acceleration, liquidation cascades. Survivorship-guarded; never fit to LSK.
- **When readable:** the retrospective lift study is days (data in hand). Any
  predictive/tradeable claim needs its own untouched forward cohort.
- **Continue:** a feature shows real lift (monsters separable) AND plausible
  executability at the tradeable subset -> pre-register a forward precursor cohort plus a
  minimal execution-feasibility (depth/venue) check.
- **Stop:** no feature separates monsters from junk after costs/executability -> close
  monster-prediction; the pump-domain exit gate below then binds.

**Status: PARKED -- exploratory economics unattractive (2026-09-14). Not a formal FAIL.**

An earlier version of this card reported a "+3.3x monster lift" and a "+1.86% diversified
harvest, robust to excluding the top 25 winners." **Those numbers are RETRACTED**: colleague
review found the discovery scripts had critical bugs (rolling windows applied after an hourly
filter, so 24h activity was ~1.5% of its true value; a global look-ahead rank; the headline
harvest number computed with no cooldown, counting one pump as dozens of dependent episodes;
truncated forward outcomes). They are exploratory/invalidated, not evidence.

A corrected replay (`path_portfolio_v2.py`: minute-level features, within-hour rank, 72h
cooldown, independent full-72h monster label, realistic first-observed-close stop fills,
point-in-time selection, gap control, path-based exit) reversed the optimism. On a frozen
primary variant (K=8 slots on a $300 bank, SL -15%, trailing 35% armed at +20%, 72h
time-stop), the window result was roughly +4.3% / +1.3% / -3.2% at 5 / 15 / 30 bps per-side
slippage, with a ~-38% max drawdown (closed-trade only; true floating drawdown is worse), a
16-trade losing streak, and only ~4 monster catches (underpowered). That corrected simulator
still carries known defects (rank taken after the buyshare/ret24 filter; low-coverage and
unresolved trades handled imperfectly; regex identity; close-only fills), so even those
numbers are not final.

Correct conclusion: after removing the most optimistic assumptions the apparent edge
collapsed toward zero, was slippage-sensitive, and carried an unacceptable drawdown. There is
NO formal out-of-sample FAIL and no mathematical proof that no edge exists; the decision to
park is an EXPECTED-VALUE call -- a further month invested in a tradeable monster harvest is
worth less than HYP-012 / HYP-015. The pump-domain exit gate is therefore NOT formally fired.

- **Kept (exploratory):** trailing activity concentrates future extreme moves and the signal
  appears early and in liquid names -- useful as a cheap RADAR / intelligence feature, marked
  exploratory (its magnitude is not a validated number), never as a trading entry on its own.
- **Data:** keep passively collecting the same bybit/binance bars (already automatic); no L2
  and no execution/trading integration for this line.
- **If ever revisited:** only with a NEW, pre-validated, unit-tested simulator on an UNTOUCHED
  window -- never a re-tune or replay of this Aug-Sep window. `monster-precursor-forward-cohort-v1.md`
  is SUPERSEDED (it was pre-registered against the retracted design) and is not to be started.

## Open questions (before locking PR order)

1. Canonize this register and the ROADMAP first (PR0), so later changes flow from an
   authoritative record. Agreed.
2. Storage: fix growth rate and backup deadline and run an archive-plus-restore drill
   first; enable retention/drop only as a separate later PR; evaluate an added Hetzner
   volume to keep long history for fat-tail work instead of early deletion.
3. HYP-015: normalize the worker under standard gates now and freeze the
   reader/verdict/cost model now, not after results mature.
4. HYP-012: identity approval is strictly chain+contract and outcome-blind; new
   approvals qualify FUTURE leads only, never retroactively; the point-in-time registry
   version is stored on every captured lead.
5. #413: one pre-registered sizing/economics pass, then prospective plus minimal
   liquidity shadow on survive, or stop.
6. Universe expansion by deterministic onboarding of the full eligible universe, never
   manual addition of current winners.
7. Show the dollar path for HYP-012 and HYP-015 (signals/week, executable notional,
   fees/impact, concurrency, capital occupancy, expected PnL for 300 USD); a positive
   percentage without capacity is not a usable edge.

## Standing stop/go decisions

- Executable pump-short: CLOSED (net negative).
- Net-buy accumulation-LONG: CLOSED as a monster-catcher (2026-09-14, net-negative at
  every hold even on pump-covering data; does not separate monsters from junk).
- Monster tradeable harvest: PARKED, exploratory economics unattractive (2026-09-14). Not a
  formal FAIL; the corrected replay's apparent edge collapsed toward zero, was slippage-
  sensitive, and had an unacceptable drawdown, so it loses on expected value to HYP-012/HYP-015.
  Prior +3.3x / +1.86% numbers RETRACTED (buggy). Activity-as-monster-radar kept exploratory;
  keep passively collecting the same bars; no L2 / execution build. See the discovery card above.
- Delayed-short / orderflow (HYP-024): stopped; do not reopen.
- Pump-domain exit gate: if the monster-precursor discovery ALSO fails to find a
  separating, executable precursor, then given pump-short negative, accumulation
  negative, and HYP-012 capacity/identity-limited, step back from the pump domain and
  seek an edge elsewhere rather than iterate more pump variants.
- No new cold-probe screens on already-viewed windows.
- Near-term edge program: finish the already-registered HYP-012/HYP-015 reads and one
  bounded abnormal-flow discovery, then reassess once. If no line clears standalone
  after-cost economics, incremental information, portfolio feasibility and business
  materiality, stop strategy-specific expansion in the pump/early-flow domain. An
  underpowered calendar checkpoint is a throughput/economic no-go, not a statistical
  claim that every unobserved market mechanism has negative EV.
- ML: parked until there is a confirmed structural edge, clean forward data, and a known
  capacity envelope; a better predictor does not solve executability/capacity or
  out-of-sample validity.
- No production or live-mode change is authorized; everything is paper.
