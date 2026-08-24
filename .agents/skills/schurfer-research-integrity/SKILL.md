---
name: schurfer-research-integrity
description: Design or review Schurfer datasets, event studies, backtests, optimizers, evidence reports, and strategy promotion gates. Use for claims about alpha, profitability, parameter selection, reproducibility, or market-data quality; not for execution-only refactors.
---

# Schurfer Research Integrity

Read `AI_RULES.md`. Start from a falsifiable hypothesis and an explicit target:
entry timing, return horizon, executable side, costs, capacity, and the decision
the report is allowed to support.

## Define the dataset contract first

- State venue, canonical instrument, market type, capture/data version,
  event-time semantics, bar interval, observation window, feature availability,
  deduplication key, missing-data policy, and purge/embargo rules.
- Partition windows by every identity dimension that prevents interleaving.
  Align event time separately from receive and persistence time. Enforce exact
  lookback/horizon continuity or classify gaps explicitly.
- Use only information available at the simulated decision timestamp. Current
  catalog membership, confirmed identity created later, final candle values,
  future OI/funding, and post-entry liquidity are not valid entry features.
- Form episodes before rows are split so overlapping triggers do not multiply
  one market move into many independent samples.

## Separate discovery from evidence

- Freeze discovery, validation, test, and prospective boundaries before
  examining later segments. Parameter selection happens in discovery;
  validation selects at most a preregistered candidate; test/prospective data
  only evaluates it.
- Do not promote from win rate alone. Report resolved/unresolved counts,
  distinct assets and UTC weeks, net EV, median, profit factor, uncertainty,
  drawdown, losing streak, concurrency/capital occupancy, asset/week
  concentration, and sensitivity controls.
- Include executable bid/ask assumptions, fees, funding, slippage, latency,
  partial depth, leverage, and capacity. Clearly distinguish descriptive
  extrapolation from evidence-supported expected returns.
- Negative mature out-of-sample EV is a failure even if diversity is also
  insufficient. Positive but underpowered evidence remains insufficient.

## Make reports reproducible

- Record exact cohort IDs/bounds, strategy/contract hash, code revision,
  dirty-tree state, query/data/capture versions, random seed, cost model, and
  fingerprints/checksums for external market paths.
- A formal result may not depend on a token remaining listed. Snapshot accepted
  external data into durable, integrity-checked storage. Fail closed on corrupt
  or unpersisted formal inputs; do not turn integrity errors into ordinary
  unresolved rows.
- First-writer-wins storage must make concurrent readers use the same winner.
  Never cache ambiguous empty, truncated, or internally gapped data as complete.

## Tests and performance

Test leakage boundaries, venue/symbol partitioning, off-by-one horizons,
missing/internal gaps, partial windows, duplicate episodes, purge rules,
negative-EV verdict precedence, deterministic seeds/fingerprints, cache races,
and repository SQL against real PostgreSQL. Establish correctness and a
repeatable baseline before optimizing; measure query plans, wall time, RSS,
rows scanned, and result equivalence for performance changes.
