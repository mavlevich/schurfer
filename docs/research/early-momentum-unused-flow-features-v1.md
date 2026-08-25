# Early-momentum unused flow features v1

**Branch/report:** `analysis/early-momentum-unused-flow-features-v1`
**CLI:** `make early-momentum-unused-flow-features-report ARGS="--format markdown|json"`

This is a retrospective, discovery-only read over the exact source bars of
already completed `early_momentum_v4` paper episodes. It does not alter the
running strategy, create a paper position, or authorize live capital.

## Question and prior boundary

The collector already persists taker flow and short-burst fields that v4 does
not use in its entry rule. This report asks whether any of those point-in-time
features describe a useful **filter-to-cash** challenger over the existing v4
opportunities.

The discovery window is fixed in code:

- start: `FORMAL_COHORT_START` from the v4 net-evidence contract;
- exclusive end: `2026-08-25T06:25:00.970709Z`, the database-clock start of
  `early_momentum_v4_prospective_v1`;
- outcomes are read only after the exclusive end plus the existing six-hour
  maturity buffer.

The end boundary was fixed before this report was implemented. No opportunity
armed at or after it can influence candidate selection. The current v4
prospective cohort remains an untouched baseline and is not evidence for the
new challenger.

## Point-in-time dataset contract

The cohort is restricted in SQL to registered strategy `early_momentum/4`, the
pinned v4 contract hash, paper LONG trades with closed and complete
`paper_conservative_costs_v1` accounting, and exact episode linkage.

For each trade, the report reconstructs exactly 121 one-minute bars ending at
the episode's persisted `features.bucket_start`. The join is fail-closed on all
of:

- `source_exchange` and `source_native_id`;
- `market_type`, `capture_version`, and `universe_version` persisted by the
  episode;
- first/last timestamp, 121 distinct buckets, 60-second maximum gap, and all
  price/trade/OI completeness flags;
- finite non-negative taker/burst notionals and positive latest OI value.

The entry features never use bars after the decision bucket. PnL is the label,
not an input to any feature.

## Features and coverage decision

The read reports four normalized diagnostics:

1. 15-minute taker imbalance;
2. acceleration versus the preceding 106 bars;
3. 15-minute 10-second-burst imbalance;
4. 15-minute turnover divided by latest OI value.

Block-trade fields are excluded because observed 24-hour coverage is zero on
both venues. RPI fields are excluded from the cross-venue candidate because
they are absent on Binance and sparse/venue-specific on Bybit. Missing data is
not interpreted as zero.

Correlations and quartiles are descriptive only. Several related features are
shown to expose non-linearity and data quality; they do not create several
independent hypotheses or multiple promotion chances.

## One frozen candidate

The only candidate produced by this viewed discovery is:

```
moderate_15m_taker_imbalance_filter_v1
0.20 <= taker_imbalance_15m < 0.50
otherwise: cash / no entry
```

The bounds were chosen after viewing the pre-boundary quartile shape: moderate
buy pressure looked better while the most aggressive buy-flow tail looked
worse. Therefore the same historical observations are **selection data, not
validation data**, regardless of their sample size or apparent PnL. Bounds may
not be tuned after the report is merged. Any different band is a new candidate
version with a new prospective boundary.

The report always returns `discovery_candidate_only` (or
`insufficient_data` when nothing is selectable), and always includes
`discovery_only_requires_new_prospective_registration`. It has no path to a
paper/live promotion verdict.

## Required follow-up before changing v4

A separate PR must register a new database-clock prospective cohort after this
candidate and its runtime policy are immutable. That cohort must:

- retain every baseline opportunity, its exact feature snapshot, and the
  select/reject-to-cash decision so rejection economics cannot disappear;
- keep baseline v4 running unchanged for a contemporaneous comparison;
- use one predeclared gate and no threshold retuning on prospective outcomes;
- fail closed on missing/incomplete flow features;
- remain PAPER or SHADOW until the evidence gate is met. It must not alter the
  current v4 contract in place.

Any claim about live profitability additionally remains subject to honest
entry/exit liquidity, complete accounting, strategy-level risk limits, order
idempotency, and reconciliation. This report answers only whether the new
feature deserves a prospective trial.

## Reproducibility

The result records code revision, dirty state, fixed bounds, dataset/schema
versions, exclusions, and a SHA-256 fingerprint of all raw rows in deterministic
trade-id order. This is intentionally a quick discovery read, not a formal
promotion artifact. If its exact historical rows need to become durable input
to another formal report, freeze them through `research_dataset_artifact.py`;
do not treat the displayed fingerprint as an off-host backup.
