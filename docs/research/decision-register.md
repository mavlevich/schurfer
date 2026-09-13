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

## Confirmed facts (with references)

- **The executable pump-short is net negative.** `app.research_report_runs`:
  `liquid_taker_candidate_v1` = 802 eligible / 151 tradeable episodes, net expectancy
  -0.224%/episode, 95% CI [-0.455%, -0.0096%] (entirely below zero);
  `liquid_taker_wider_stop_shadow_v1` = paired delta +0.084%, CI [-0.045%, +0.228%]
  (crosses zero). Both `do_not_promote`. These are the only confirmatory runs recorded.
- **The only positive edge candidate is HYP-012 cross-venue source-lead LONG**
  (`discovery-ledger.md`, HYP-012): enter long on Binance/Bybit after MEXC/Gate show the
  pump first. +1.8% to +3.4% across four routes, Holm-significant (p=0.0004-0.0016),
  robust under leave-one-out and busiest-week exclusion. Matching was `base_symbol_v1`
  (ticker), `identity_verified=false`. Under canonical identity v3: 9 qualified / 462
  excluded (391 `source_identity_unapproved`) over ~6 days, so ~10 qualified leads/week
  across 5 approved assets.
- **HYP-015 hold12h paper worker is live since 2026-09-13** (contract_sha256 `f280bd14`,
  paper only). A held-out gross recheck on 3610 accumulated baseline probes: 12h hold
  +0.199% vs 4h +0.012% (gross, funding NOT modeled).
- **Data is already rich.** The cold-bar export is `COPY (SELECT * ...)` of
  `bybit_momentum_bars_1m` (OHLC, bid/ask, microstructure, created_at); OI, funding, and
  liquidations are captured in their own tables. The local 11-column window parquet is a
  hand-cut subset, not the export.
- **#413 accumulation v2 carries a capacity warning.** Point-in-time dynamic sizing on
  the fire set: P-SHAPE ~63% tradeable at a 300 USD target, P-MAG ~20%. No returns read.
- **Prod disk risk.** `/` reached 69%; freed 10.26GB docker build cache to 55%.
  Timeseries hypertables already have Timescale retention plus compression. The unbounded
  growth is in plain `app` tables without retention, top being
  `pump_derivatives_context_samples` at 3.0GB.

## Running workers (2026-09-14)

Collectors (bars, OI, liquidations, funding), `momentum-watch`, and the HYP-015 pair
(`momentum_flow_paper_v1` baseline plus `hold12h`). All paper, no real money. Every
running worker is either raw-data capture or active evidence, and no confirmed-dead
worker is running, so there is nothing to turn off. A worker is stopped only when its
line is confirmed dead AND it feeds no active read; negative paper P&L on a control or
an active comparison is the measurement, not a reason to stop it.

## Decision cards

### HYP-012 - cross-venue source-lead LONG (lead candidate)

- **Measure:** prospective paired early-versus-confirmation net return (delay=0,
  horizon=+30), four routes Holm-corrected, on `identity_verified=true` leads; plus the
  dollar path (signals/week, executable notional, fees/impact, capacity, expected PnL
  for the 300 USD bank).
- **When readable:** when the identity-verified forward cohort reaches at least 100
  pairs, at least 30 clusters, at least 4 weeks. At ~10 qualified/week over 5 assets that
  is months unless the registry is expanded (outcome-blind).
- **Continue:** paired lower bound > 0 under leave-one-out and busiest-week exclusion,
  AND a positive after-cost dollar path at feasible capacity.
- **Stop:** paired delta not positive, or capacity/impact makes the dollar path
  non-viable for the 300 USD bank.
- **Retro-now vs forward:** the confirmation is forward-only. The dollar-path/capacity
  read on the already-collected discovery leads is retrospective-descriptive and can be
  produced now.

### HYP-015 - momentum-flow hold12h (active candidate)

- **Measure:** paired net return (12h vs contemporaneous `momentum_flow_paper_v1`) with
  real fees plus funding, per episode; MFE/MAE, initial-stop survival, capital occupancy.
- **When readable:** when the forward paper cohort (from 2026-09-13) reaches its
  pre-registered sample. The frozen reader, cost model, and verdict logic must be
  committed BEFORE any result is read.
- **Continue:** paired lower bound > 0 after funding and costs on a mature sample.
- **Stop:** paired delta <= 0 after funding (the gross +0.199% does not survive the
  ~1.5 funding intervals of a 12h hold).
- **Retro-now vs forward:** forward-only for the verdict. Freeze the reader now to
  prevent fitting it to accumulated results.

### #413 - accumulation v2 (decision owed)

- **Measure:** ONE point-in-time sizing/economics pass on the frozen window:
  dynamically-sized net EV after 22 bps, break-even slippage, dollar path for 300 USD,
  split by venue and primary, on the tradeable subset.
- **When readable:** now. This is descriptive/economic on an already-frozen window, not
  a predictive confirmation.
- **Continue:** at least one pre-registered size shows positive after-cost EV at viable
  capacity, then a minimal liquidity shadow plus a prospective registration.
- **Stop:** no pre-registered size is economically viable, then freeze/close the
  direction.
- **Retro-now vs forward:** retro-now. This is the pass to run first for #413.

### pump-short (executable) - CLOSED

Confirmed net negative (see facts). Closed. Do not reopen and do not tune entry on the
negative base (the multiple-comparison trap the ledger warns about).

### Universe coverage (enabler, not an edge)

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
- Delayed-short / orderflow (HYP-024): stopped; do not reopen.
- No new cold-probe screens on already-viewed windows.
- ML: parked until there is a confirmed structural edge, clean forward data, and a known
  capacity envelope; a better predictor does not solve executability/capacity or
  out-of-sample validity.
- No production or live-mode change is authorized; everything is paper.
