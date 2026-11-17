# Radar pump cases: the owner's observation (HYP-038 intake)

Status: **intake only**, written 2026-11-12, before the research process read any data about these cases. It is not a registration or a protocol and fixes no threshold. The formal registration comes after v2's terminal state, under the rule of the [v2 administrative stop](source-lead-forward-cohort-v2-administrative-stop.md); data dated before that registration is historical for this study (discovery or validation), never prospective.

## The observation

The owner looked at the charts of tokens that reached Schurfer's radar, among them
STAMP, PUSSY, POD and SPACEHOOD. Many had violent candles. The owner's impression:

- our software partly scored these tokens and gave signals before their pumps;
- on some of them a short after the pump looked obvious.

Not recorded: the exact dates, the venue, which radar signal or score fired, and any
entry or exit rule. These are not filled in from data now, because every market value
from 2026-09-29 on lies inside the v2 blind window.

## Two claims to test

- **C1, before the pump.** Radar signals come before large moves more often than matched
  quiet moments of the same instruments.
- **C2, after the pump.** A short opened after a pump earns more than its costs,
  funding included.

## What earlier reads already say

- **HYP-017** (2026-08-18 to 2026-08-27, prospective WATCH decisions). The +25% within
  24 h hit rate was 1.59% for WATCH against 2.65% for quiet controls. The verdict was
  `inconclusive`: in that window the radar did not beat its controls.
- **Post-pump short on the discovery year** (2024-10-01 to 2025-09-30). The 2026-11-11
  lab series read 54 configurations of pump long and post-pump short, with taker and
  model maker entries. The maximum t was 0.97. The price reversal after the pump is
  real, but negative funding on pumped coins and rare large squeezes ate it. HYP-024's
  pump-short line was stopped earlier for related reasons.

The prior for both claims is therefore weak. The new window is still worth one honest
read, because it was never part of these reads.

## How it will be tested, fixed now

1. **Window.** From 2026-09-29 to v2's terminal date. Nothing in it is read before v2 is terminal (`admin_stopped` recorded or the formal read done). Even then the window is historical data for this study, read under the protocol registered after the terminal state.
2. **Closed windows stay closed.** If v2 ends in `admin_stopped`, the stop artifact lists `closed_windows` (each closed episode's canonical asset, from entry to the close of its exit bar). An episode of this study is dropped, never imputed, when any window it uses on that asset intersects a closed window, through any venue: feature lookbacks, controls, labels and outcome horizons alike. Closed windows are not read for any purpose, including the diagnostic named tokens. An instrument that cannot be mapped to a canonical asset is excluded by base ticker.
3. **Denominator.**
   - C1 uses every persisted radar signal in the window.
   - C2 uses every pump event of the existing detector.
   - Selected cases are never the sample.
4. **Known cases.** STAMP, PUSSY, POD and SPACEHOOD, AKE, FRONG and EVAA (added
   2026-11-15), and any other token the owner or the colleague viewed,
   are diagnostic only. They are reported apart and do not count toward a verdict. The owner's viewing dates are unknown, so until they are pinned down each viewed asset is excluded from the verdict for the whole window; unknown viewing dates are never declared clean afterwards.
5. **Protocol first.** Before any read, a written protocol fixes:
   - the outcome contract, from HYP-017 for C1;
   - entry, exit, costs and actual funding for C2;
   - net as traded as the primary metric;
   - the variant count, which carries the 54 lab configurations and HYP-024 as
     predecessors;
   - the list of excluded assets (viewed tokens and closed windows);
   - the radar and detector versions used;
   - how repeated signals of one asset are merged;
   - how missing data is handled (unresolved, never a loss, win or zero).

   The colleague reviews it before the read.

6. **Exposure.** The owner's viewing of these charts is recorded in the
   [seen-windows registry](seen-windows-registry.md). It changes no v2 decision: v2's
   rules are frozen and run automatically.

## Addendum 2026-11-15: three more viewed cases

AKE, FRONG and EVAA were named as recent radar cases in the owner and colleague's
direction discussion on 2026-11-15. Who viewed them, on which venue and on which dates
is not recorded. They are diagnostic only under item 4, reported apart and excluded from
any verdict for the whole window until their viewing dates are pinned down. The research
process read no price, signal or score about them; this addendum adds only their names.
The exposure row is in the [seen-windows registry](seen-windows-registry.md).

## Addendum 2026-11-17: BLESS and RARE, and a proposed refinement of C2

**Two more viewed cases.** On or shortly before 2026-11-17 the owner opened roughly
one-year price charts of BLESS and RARE after both reached the radar. The owner's
impression: one token drifts down with periodic pumps; the other repeats pump and
drawdown. Venue, exact viewing dates and the radar signal that fired are not recorded
and are not filled in from data. The charts span the v2 blind window, so both tokens
are diagnostic only under item 4, reported apart and excluded from any verdict for the
whole window. The research process read no price, signal or score about them; this
addendum adds only their names and the owner's description. The exposure row is in
the [seen-windows registry](seen-windows-registry.md). This is a record of where an
idea came from; it does not exclude the two tokens from unrelated future studies.

**Proposed refinement of C2, not an approved test.** The owner asked whether a token's
own history could tell how its next pump will behave. The proposal for the protocol
author: test whether one point-in-time feature, the share of a token's earlier
completed pump episodes that retraced, improves the C2 post-pump short against the same
short without the feature. Whether C2 takes it, its parameters and its place in the
variant count are decided when the protocol is written.

What is already on record, and why the feature is at most a refinement:

- [HYP-013](discovery-ledger.md) (token-behavior) already used the outcome of the most
  recent spike: recovery is the first daily close within +-10% of the pre-spike close,
  with unrecovered episodes right-censored (`token_behavior_descriptors.py`). Its four
  filters ended `insufficient_triggers` on 47 instruments over two weeks; no test ran.
  The possible novelty is the **share over several past episodes** and its predictive
  value, not the use of a pump's outcome as such.
- [Serial pump regimes v1](serial-pump-regimes-v1.md) describes what followed each
  independent pump regime, without a verdict.

What the protocol would have to fix before any read, if it takes the feature:

1. what counts as a pump and as one episode, reusing HYP-013's spike definition unless
   a reason to change it is written down;
2. how far and within how long a retrace must happen;
3. the minimum number of completed earlier episodes; below it the token is `no_history`,
   never 0% or 100%;
4. an episode still inside its retrace horizon at the decision is not completed and is
   left out, never counted as "did not retrace";
5. the current signal's own episode never enters its history;
6. the share is computed on a market-adjusted price, while the primary metric stays net
   as traded; a positive market-adjusted figure alone is not account profit;
7. one action, justified by prior reads rather than by chart shape (C2 is a short), with
   "no trade" as the comparison;
8. a fixed positive net threshold: a filtered short that only loses less is a fail.

The window from 2026-09-29 stays historical data for this study after v2 is terminal
(item 1); a pass in it would still need a forward confirmation. A first test is pooled
across instruments; no rule is fitted to BLESS, RARE or any viewed token.
