# Exchange instrument identity cases

This note records production cases that define the requirements for canonical asset
and exchange-instrument identity. They are evidence and test fixtures, not permission
to merge assets automatically.

## Identity layers

Schurfer must keep three concepts separate:

1. **Display ticker** — a venue-facing label that can be renamed or reused.
2. **Exchange instrument** — a versioned venue market identified by
   `exchange + market_type + market_id + onboarded_at`.
3. **Canonical asset** — a reviewed cross-venue asset identity, preferably anchored
   by `chain + contract_address` for spot assets.

Derivative instruments may not expose a contract address. They remain unlinked until
their underlying asset can be verified through an official listing notice, spot
market metadata, or another authoritative source.

## Case 1: CHECK / CHECKMATE

Observed on 2026-07-23:

- Bithumb used the ticker `CHECK` for a newly listed KRW spot market.
- LBank spot exposed `CHECK/USDT`.
- LBank perpetual used:
  - market id `CHECKMATEUSDT`;
  - unified symbol `CHECKMATE/USDT:USDT`;
  - display/source naming `CHECK (CHECKMATE)`.
- LBank had previously announced that perpetual, then announced a delisting. The
  market later appeared active again.

Requirements demonstrated:

- ticker equality is not required for two instruments to refer to one asset;
- ticker similarity is not enough to prove that relationship;
- relisting the same market id must be distinguishable when onboard time is known;
- listing, delisting, and relisting are separate events;
- a Bithumb listing can cause a venue-local pump while an existing LBank perpetual
  remains below the pump threshold.

Until canonical verification is implemented, `CHECK` and `CHECKMATE` must remain
separate identities with enough metadata retained to resolve them later.

## Case 2: GMEROBINHOOD / GME

Observed on BingX on 2026-07-23:

- market id: `GMEROBINHOOD-USDT`;
- unified symbol: `GMEROBINHOOD/USDT:USDT`;
- display name: `GME-USDT`;
- launch time was present in the exchange market metadata;
- the first ticker showed an extreme percentage and unavailable volume;
- minutes later, the same instrument reported a normal percentage and non-zero
  quote volume.

Requirements demonstrated:

- preserve market id, unified symbol, display name, and launch time independently;
- tag new-listing observations instead of treating their first 24h percentage as
  directly comparable with mature markets;
- do not equate the display label `GME` with another asset solely by ticker text.

## Current foundation

Pump source rows retain:

- a versioned `identity_key`;
- exchange market id;
- unified and display symbols;
- market type;
- base, quote, and settle assets;
- contract size;
- onboard/listing timestamp when the venue exposes a supported field;
- first and last ticker timestamps;
- an `identity_conflict` flag if one pump episode observes different versioned
  instruments for the same venue.

Legacy rows remain nullable and therefore honest about missing metadata.

## Follow-up: listing and delisting event study

The next event collector should store official announcements and observed market
state changes separately:

- `announced_at`, `effective_at`, and `observed_at`;
- exchange and exchange-instrument identity;
- event type: listing, delisting, relisting, trading suspension, or resumption;
- source URL and a content hash;
- quote currency and normalized FX rate;
- whether deposits and withdrawals were available.

Historical analysis must use event-time information only and avoid survivorship
bias. Suggested outcomes are 1h, 4h, 24h, 7d, 30d, and 90d returns, MAE/MFE, maximum
drawdown, liquidity, and performance relative to BTC and a matched market-cap cohort.

The investment hypothesis is deliberately unproven: a token that pumps and retraces
after a Korean listing may or may not have positive long-term returns. Listing events
can be a discovery signal, but portfolio inclusion requires independent evidence
about liquidity, valuation, unlocks, fundamentals, and risk.
