# Source-lead forward cohort v2 (HYP-012 v4, PR D)

Status: registered 2026-09-26, before any v2 episode exists.
Code: `source_lead_forward_cohort.py` (`CONTRACT_VERSION = source_lead_forward_cohort_v2`),
`source_lead_qualification.py` (qualification v4), migration 0051.

## What changes from v1

|                 | v1 (closed)                                         | v2                                                              |
| --------------- | --------------------------------------------------- | --------------------------------------------------------------- |
| Qualification   | `source_lead_qualified_capture_v3`                  | `source_lead_qualified_capture_v4`                              |
| Registry        | v3, 14 assets, Binance routes                       | v4, 85 assets (Bybit 44, Binance 55), fingerprint `7d5f635a...` |
| Venue selection | lowest round-trip impact across all approved venues | lowest round-trip impact among `TRADABLE_VENUES = ("bybit",)`   |
| Estimand        | `standalone_early_entry_net_return_v1`              | `standalone_early_entry_net_return_tradable_venue_v2`           |
| Start           | 2026-09-03T00:00Z                                   | 2026-09-29T00:00Z (`IDENTITY_REGISTRY_V4_START`)                |

v2 keeps every v1 evaluation rule unchanged:

- entry at the captured ask VWAP;
- 30-minute horizon;
- OHLCV close exit proxy with 15 bps exit slippage, and the 0 / 15 / 30 bps sensitivity;
- shared cost model (10 bps taker per side, funding prorated);
- the frozen cluster bootstrap;
- evidence floor: 100 resolved episodes, 7 clusters, 4 UTC weeks;
- concentration caps: 35% per asset, 45% per week;
- one evaluation at the earliest prefix that meets the floor.

The floor and caps are not loosened even though v4 has more assets.

## Venue rule

Binance futures are not available to the owner, so Binance observations are still
captured and recorded in qualification details (`tradable: false`, with their
round-trip impact) but are never selected.

A lead that is executable only on Binance is excluded as
`no_tradable_executable_target`. Its Binance data remains available as a descriptive
comparison and is not part of the estimand.

The reader refuses to run if any qualified episode is on a venue outside
`TRADABLE_VENUES`. Adding a venue means a new cohort version, or an extension registered
in advance from its connection date, never a change mid-cohort.

A target whose contract size was defaulted instead of read from the instrument is refused
as `target_contract_size_unknown`. This uses the `quote_timing.contract_size_source` field
added in #446.

## What a verdict allows

A `candidate` verdict is necessary, not sufficient. It allows:

- registering a broader confirmatory cohort;
- a separate live execution test on Bybit, approved by the owner and capped at USD 50
  notional, to measure real fills, fees and slippage against this cohort's cost
  assumptions.

It does not establish the edge, and larger capital needs the confirmatory cohort. A `fail`
or `insufficient_data` verdict allows neither.

## Book freshness

A target book older than 2000 ms, measured as receive time minus the venue's book
timestamp (`quote_timing.book_age_ms`), is refused as `target_book_stale`. A book more than
1000 ms ahead of the local clock is refused the same way, and a book with no venue
timestamp is refused as `target_book_timestamp_missing`. 2000 ms is the limit the Bybit
canary used, under which 98.2% of Bybit books were fresh.

## v1 is closed without a formal read

The v1 cohort reached 15 of its 100 required episodes. Its only venue, Binance, is not
tradable for the owner. It is closed at the v2 start and never formally read; its rows
stay in the database under qualification v3.

## Deploy

The capture worker must run this code before 2026-09-29T00:00Z. If the deploy lands later,
move `IDENTITY_REGISTRY_V4_START` forward to the deploy time; it never moves earlier.
Captures before the start are recorded as `before_identity_registry_v4_activation`, with no
target sampling. Migration 0051 pins registry v4 on every v4-tagged qualification row.
