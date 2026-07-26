# Episode replay protocol v1

Status: pre-registered before the first virtual-strategy replay.

This protocol defines the confirmatory boundary for the pump-short replay. It is
deliberately separate from the implementation: changing code must not silently change
the experiment.

## Question and unit of observation

The primary question is whether the current pump-short strategy, or a pre-registered
challenger, has positive net expectancy after fees, funding, and liquidity-aware
slippage.

The unit of observation is one persisted `pump_event_id`, not a decision or trade.
Decisions inside an episode are replayed in timestamp and database-id order. A strategy
variant may open at most one virtual trade in an episode.

The correlation cluster is the reviewed canonical asset when one exists. Until
canonical links are available, the normalized base ticker is used as a conservative
fallback. Reports must show the number of unique clusters and the share contributed by
the most frequent clusters.

## Cohorts

- Decisions through `2026-07-25T23:59:59.999999Z` are discovery/development data. They
  may be used to validate the pipeline and discover hypotheses, but not to confirm a
  champion.
- The first confirmatory cohort starts at `2026-07-26T00:00:00Z`.
- A first directional reading is allowed at 50 eligible episodes. It is descriptive
  only and cannot promote a strategy.
- The first formal go/no-go analysis uses the first 100 eligible episodes in the
  confirmatory cohort and requires at least 30 distinct asset clusters.
- If 100 episodes contain fewer than 30 clusters, the result is
  `insufficient_diversity`; do not extend or change the threshold after inspecting
  outcomes. A replacement protocol must be registered first.
- A challenger selected from a family is only a shadow candidate. Promotion beyond
  shadow requires a new forward cohort that was not used to choose the challenger.

An eligible episode has direct episode attribution and a complete chronological
decision path. Its persisted episode must start inside the selected window and be
closed before the exclusive cutoff; open or boundary-crossing episodes are reported as
censored, not silently truncated. The close timestamp is used only to establish the
evaluation boundary and is never exposed as an entry feature. Every decision in an
eligible episode must have:

- a unique `decision_id`;
- a non-empty `strategy_version`;
- a positive decision price;
- the stored decision feature/config envelope;
- the required outcome horizons for the selected replay;
- exact anchor-venue outcome coverage unless a manifest explicitly enables fallback
  for a sensitivity analysis.

If one decision in an episode fails structural validation, the whole episode is
excluded from confirmatory replay. This prevents silently cherry-picking the usable
part of a path.

## Primary and secondary metrics

The primary metric is mean net expectancy per episode after fees, funding, and modeled
slippage.

The baseline has evidence of edge only when:

- eligible episode `N >= 100`;
- distinct asset clusters `>= 30`;
- the lower bound of the two-sided 95% cluster-bootstrap confidence interval for net
  expectancy is above zero.

A challenger can replace the baseline as shadow champion only when:

- its own lower 95% confidence bound for net expectancy is above zero;
- the lower 95% confidence bound for its paired per-episode expectancy difference
  versus the baseline is above zero after Holm correction across the registered
  challenger family;
- its point estimate remains positive when each of the five most frequent asset
  clusters is excluded in turn.

If the upper confidence bound for net expectancy is at or below zero, the result is
`no_go`. Every other formal result is `inconclusive`; win rate alone cannot change that
classification.

Secondary metrics are profit factor, maximum drawdown, average win, average loss,
MAE, MFE, captured move, initial-stop rate, missed-winner rate, trade frequency, and
exposure time. They explain the primary result but cannot override it.

## Multiple comparisons

Only challengers whose complete manifests were committed before the confirmatory query
belong to the confirmatory family. Apply Holm correction to their primary paired
comparisons. Parameter sweeps, unregistered variants, subgroup searches, and the
"best" result found after looking at the data are exploratory and require a new
forward cohort.

## Time and look-ahead rules

At decision time `t`, a variant may use only information with source timestamp
`<= t`. The eventual episode close, later `miss_count`, future candles, resolved
outcomes, and later decisions are evaluation data, never entry features.

Derived candle features use only fully closed candles. A candle that closes after the
decision is unavailable even if its open time precedes the decision.

For a bar in which both TP and SL could have fired:

- use complete one-minute candles to establish order when they are available;
- otherwise resolve against the short by assuming the stop fired first;
- record the resolution source (`one_minute` or `conservative_stop_first`) in the
  virtual trade.

Fallback venue outcomes and partial candles are never silently treated as exact anchor
data. Sensitivity runs must identify them in their manifest and report them separately.

## Reproducibility manifest

Every run records:

- protocol and replay-engine versions;
- Git revision and explicit working-tree dirty state;
- dataset start and exclusive cutoff;
- deterministic input fingerprint;
- strategy versions;
- resolver version and required horizons;
- accepted outcome statuses and fallback policy;
- query/schema version;
- strategy, fee, funding, slippage, and exit-model versions once those components
  exist.

Reports generated from a dirty working tree are development artifacts, not
confirmatory evidence. A historical report is reproducible only when its code revision,
manifest, and input fingerprint are retained together.

## Promotion boundary

Passing this protocol can promote a challenger to forward shadow collection. It does
not authorize `AUTO_TRADE=true`. Real-money promotion additionally requires a new
out-of-sample cohort, production/database hardening, exchange-key controls, and an
explicit risk review.
