# ADR-0009: Incubate a separate public market-events project

Date: 2026-07-23
Status: Proposed

## Context

Schurfer is a private trading product. ADR-0002 deliberately keeps its dashboard,
execution, signals, infrastructure, and production data private.

The measurement work is also producing a strategy-neutral capability that can be
useful independently: normalized exchange-instrument identities and sourced listing,
delisting, relisting, suspension, and resumption events. A public implementation and
read-only event explorer could:

- improve the quality of the underlying dataset through external review;
- provide a reusable research input instead of another trading bot;
- demonstrate data engineering, exchange integration, and reproducible quantitative
  research in a public portfolio;
- create a future data/API product without exposing Schurfer's private edge.

CCXT historical endpoints can enrich funding, open interest, mark/index basis,
long/short ratios, and liquidations around an event. They cannot reconstruct an exact
decision-time order book, signal lag, unsupported venue history, or an instrument that
has disappeared from the venue. Durable live measurement and official event archives
therefore remain necessary.

## Decision

Incubate the event schema and collectors inside Schurfer until they have survived
production data and at least one reproducible event study. Then extract the generic
parts into a **separate public repository and deployment** with no runtime dependency
on Schurfer.

The public project may contain:

- canonical, versioned exchange-instrument records;
- listing, delisting, relisting, suspension, and resumption events;
- official source URLs, content hashes, announcement/effective/observed timestamps,
  and parser provenance;
- normalized quote-currency and timestamped FX context;
- public market outcomes and coverage diagnostics;
- a CLI/library for validation, export, and reproducible event studies;
- a delayed read-only website and, later, a rate-limited public API.

The intended public core is a point-in-time market-events SDK, not another order
execution engine. Its candidate surface includes:

- `Instrument`, `VenueMarket`, and versioned symbol/contract identity records;
- normalized `MarketObservation` and `MarketEvent` records;
- bitemporal `source_time`, `effective_at`, `observed_at`, and `fetched_at`
  timestamps;
- raw-payload hashes, parser/schema versions, source references, coverage, freshness,
  and conflict flags;
- listing, delisting, relisting, suspension, contract migration, and market-quality
  events;
- adapter conformance checks for stale timestamps, missing candles, duplicate rows,
  invalid volume, contract-size errors, capability mismatches, and cross-source
  conflicts;
- research-safe Parquet/DuckDB exports with deterministic manifests and synthetic
  public fixtures.

The SDK may use upstream connectors, but its value is identity, lifecycle, provenance,
and point-in-time correctness. It must not duplicate unified order submission,
portfolio accounting, or matching engines.

The private/public boundary is one-way and explicit:

1. Public collectors produce a versioned schema, release artifact, or public API.
2. Schurfer may consume that versioned output.
3. The public service never connects to the Schurfer production database.
4. Private decisions, strategy configuration, thresholds, execution logic, account
   data, credentials, and non-public production observations are never exported.
5. Data licensing and exchange terms are reviewed before publishing raw responses or
   redistributing historical datasets. When redistribution is unclear, publish
   source references, normalized metadata, code, and reproducible fetch instructions
   rather than the restricted payload.

ADR-0002 remains in force: the public project is not a public component of Schurfer
and never trades or holds third-party funds.

## Ecosystem boundary

The following projects solve adjacent problems. Evaluate them behind explicit
adapters or process boundaries rather than making the future public schema depend on
one engine:

| Project                                                             | Intended role                                                      | Current decision                                                                                                                                                                                                                                                   |
| ------------------------------------------------------------------- | ------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| [CCXT](https://github.com/ccxt/ccxt)                                | Unified crypto venue REST and WebSocket adapters                   | Keep as Schurfer's primary CEX integration. Contribute narrow, reproduced exchange-parser fixes upstream.                                                                                                                                                          |
| [OpenBB](https://github.com/OpenBB-finance/OpenBB)                  | Standard provider models for public and licensed financial data    | Evaluate first in the separate public research workflow, especially for equities, macro, and provider extensions. Its AGPLv3 boundary requires explicit review before it becomes a dependency of the private product.                                              |
| [NautilusTrader](https://github.com/nautechsystems/nautilus_trader) | Multi-asset event-driven backtest and live-execution engine        | Do not replace Schurfer execution now. Re-evaluate when real multi-venue execution, order-state recovery, or research/live parity exceeds the value of the current engine. It is LGPLv3 and upstream contributions require the project's issue-first/CLA workflow. |
| [LEAN](https://github.com/QuantConnect/Lean)                        | Apache-2.0 cross-asset research, backtesting, and brokerage engine | Use only as a standalone benchmark or future traditional-asset research runner. Do not embed its C# runtime into the current Python/Go services without a measured requirement.                                                                                    |

Contribution policy is evidence-driven:

1. Use a project on a real bounded workflow first.
2. Reproduce a generic defect against the current upstream revision.
3. Confirm the change belongs in the upstream abstraction rather than Schurfer
   policy.
4. Follow that repository's current contribution guide, tests, license, CLA, target
   branch, and naming rules.
5. Keep one defect or provider capability per pull request.
6. Remove a local workaround only after an upstream release is adopted and verified.

Open-source contribution is a useful outcome, not a reason to add a dependency. We do
not migrate engines or add providers only to manufacture public activity.

## Collaboration boundary

The engineering and research work can share the public schema without sharing private
strategy data:

- engineering owns adapters, identity, ingestion, storage contracts, quality checks,
  provenance, CI, and release compatibility;
- data science owns pre-registered event studies, feature definitions, statistical
  diagnostics, visualizations, and documented interpretation;
- both review the event schema, data dictionary, synthetic fixtures, and reproducible
  reports;
- `financial-markets-research` may consume released SDK artifacts and permitted public
  datasets, but never the Schurfer production database;
- private thresholds, decisions, credentials, execution results, and alpha labels are
  excluded by an allowlist test before any export.

## Delivery stages

1. **Private incubation**
   - stabilize instrument identity and event schemas;
   - collect live market-state changes and official archive events;
   - record provenance, conflicts, and parser versions;
   - validate at least CHECK/CHECKMATE, relisting, and Korean listing cases.
2. **Reproducible research package**
   - bounded CCXT historical enrichment around events;
   - event-time outcomes at 1h, 4h, 24h, 7d, 30d, and 90d;
   - BTC/market-adjusted comparisons and episode-clustered confidence intervals;
   - deterministic fixtures and a documented data dictionary.
3. **Repository extraction**
   - create a neutral public repository with independent CI and release versioning;
   - move only generic collectors, schemas, validation, and research utilities;
   - import it back into Schurfer only after the public API is stable.
4. **Public explorer**
   - searchable event and instrument pages;
   - data-source and freshness status;
   - delayed aggregate outcomes and downloadable permitted samples;
   - no trading recommendations or performance promises.

## Consequences

- Pro: produces a credible public portfolio project from work the private product
  genuinely needs.
- Pro: external review can improve exchange parsers and canonical identity quality.
- Pro: creates an optional data/API monetization path without revealing execution
  logic.
- Con: extraction too early would duplicate schemas and slow measurement work.
- Con: exchange terms and dataset redistribution rights require source-by-source
  review.
- Con: a public deployment needs separate secrets, database, backups, rate limits,
  abuse controls, and operational ownership.

## Revisit trigger

Approve repository extraction only when all of the following are true:

- the internal event schema has run in production for at least two weeks;
- identity conflicts and relistings have real fixtures and tests;
- one end-to-end event study is reproducible from documented inputs;
- the public/private field allowlist has a test;
- licensing for the initial published sources has been reviewed.
