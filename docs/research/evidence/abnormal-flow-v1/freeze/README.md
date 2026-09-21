# Abnormal-flow v1 freeze inputs

This directory contains outcome-blind inputs for the separate freeze PR. No
forward returns, PnL, or verdict were read while producing these artifacts.

## Candidate selection

The pre-declared power-constrained tail rule selects the highest OI-growth
percentile whose calibration episode rate projects at least 150 episodes over
the scorable evaluation span. The result is **P97.5**:

- OI growth: at least `2.6350760613721547%` over 60 minutes;
- buy pressure: at least `0.5899762964448008` (fixed P90);
- maximum high/low deviation from the window open: at most
  `0.008436929648241207` (fixed P25);
- OI-notional floor: at least `$1,296,533.1054042869` (fixed P25);
- calibration: 1,528 fires -> 175 cooldown-deduplicated episodes;
- projected scorable evaluation episodes: 243.7.

P99 is too sparse: 37 calibration episodes and 51.5 projected evaluation
episodes. P95 and P90 clear the floor but are less selective, so the registered
rule does not choose them.

The selected calibration cell is venue-skewed: 151/175 episodes are Bybit and
24/175 are Binance. The formal report must preserve per-venue results and must
not describe a pooled PASS as independently replicated on both venues.

The 150-episode threshold is a power floor, not proof of sufficiency or edge.
Formal evidence still depends on the frozen missingness ceiling, control-match
coverage, clustered uncertainty, leave-one-asset-out stability, costs, funding,
and the one-shot verdict.

The raw projection leaves 38.4% headroom before 243.7 falls below 150. The
freeze PR must therefore set the combined outcome/control attrition ceiling at
or below 38.4% (a 20-25% ceiling is preferred). If it permits more attrition,
the candidate selection must be recomputed before any returns are read.

## Inputs

`candidate_table.json` pins the identity export, provisional contract, and the
ordered set of 14 verified cold-bar manifests. Funding calibration is stored in
the sibling `../funding/` directory. The economic evaluation window remains
unread until the freeze PR is reviewed and merged.
