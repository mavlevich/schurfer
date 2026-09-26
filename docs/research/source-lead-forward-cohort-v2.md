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

## One formal read, claimed before any return

`source-lead-forward-cohort-report` computes returns, so it is the formal read itself. The
claim lives in `app.formal_read_claims` (migration 0054), unique per (study, contract
version, cohort start). A run goes through these steps:

1. **Timing pre-check.** With fewer than 100 matured episodes, or fewer than 4 UTC weeks
   among them, it refuses before fetching anything.
2. **Find the checkpoint without returns.** It fetches the exit bars and finds the
   registered checkpoint from resolution status alone: the first 100 resolved episodes over
   4 weeks. `episode_resolution_status` checks bar presence, the boundary and gap, and
   finite positive prices, and computes no return. The full resolution runs the same
   checks first, and a test forbids `calculate_performance` on this path. If the checkpoint
   is not reached, it refuses and claims nothing, so a later run can try again.

   **Registered decision:** fetching the OHLCV candles, which contain prices, before the
   claim is allowed. A "read" means computing or showing returns. The candles stay in
   memory and are used only for resolution status, and the same fetched bars feed the
   verdict after the claim.

3. **Claim the prefix, then compute.** It commits a claim storing the exact ordered capture
   ids of that checkpoint prefix, and only then computes the verdict on exactly those ids.
   Clusters and concentration are judged at that single checkpoint: a shortfall is a
   permanent `insufficient_data` and never a reason to wait.
4. **Complete.** Once the checkpoint artifact is written, the claim is marked `completed`
   with its fingerprint. Every later run refuses.

**Only one run computes at a time.** A claim carries a lease owner and a 60-minute expiry,
longer than the 20-minute exchange-fetch budget. A run that fails after claiming (an
exit-bar timeout, a cache error, an artifact write) can be resumed on the same stored ids,
but only after the lease expires, by atomically taking the lease over. It never searches for
a new prefix. Only the current lease owner can mark the claim `completed`.

## Exit-book diagnostic (not part of the verdict)

`source-lead-exit-capture`, a separate service writing to `app.source_lead_exit_observations`
(migration 0052), samples the raw Bybit book (50 levels) of every qualified v2 episode's
entered instrument.

- **When.** At the end of the v2 exit bar, `ceil_minute(entry + 30m) + 60s`. That is the
  instant the verdict's OHLCV close refers to, so a later calibration compares like with
  like.
- **Claim first.** Each episode is claimed before the request. A crash between request and
  write becomes `crashed_after_claim` and is never re-requested.
- **Quantity.** A hypothetical quantity, `notional / entry ask VWAP`, rounded down to
  `qtyStep`, with the raw value also stored.
- **Timeliness vs outcome.** Timeliness is `on_time` up to 30 s, `late` up to 120 s, and
  `missed` after that. It is recorded separately from the fetch outcome.
- **Book freshness.** Same limits as qualification.
- **What is stored.** The book snapshot with `ts`, `cts`, `seq`, `u` and a SHA-256.

The v2 verdict never reads this table. Until a registered diagnostic read, only coverage,
statuses and delays may be shown. It feeds cost calibration for the next contract version.

## Shadow execution (not part of the verdict)

The execution service's `source_lead` strategy (`source_lead_shadow.py`, `SOURCE_LEAD_MODE`)
measures the real path to an order without placing one. Unset means DISABLED; only
`shadow` or `disabled` are accepted.

For each qualified v2 episode it:

- **Claims first.** It claims a row in `app.source_lead_shadow_attempts` (migration 0055)
  before any quote.
- **Resolves the instrument from the registered native id.** It must be exactly one active
  USDT linear swap.
- **Takes a fresh raw Bybit book at the intended send time.** The book carries its native
  `ts`, with the same freshness limits as qualification.
- **Records every skip with its own outcome.**
- **Records a valid intent** through `ShadowBroker` into `trade_decisions`.

**Timing chain:**

- capture to first seen (`late` over 30 s, never dropped);
- `qualified_at` to first seen;
- first seen to quote request;
- quote request to response.

`quote_change_bps` is the change of the executable $50 ask VWAP over that delay, on the
same instrument and notional. No order was sent, so it is not slippage.

A live broker (`LIVE_PROBE`) is a separate change with its own order-lifecycle review.

## v1 is closed without a formal read

The v1 cohort reached 15 of its 100 required episodes. Its only venue, Binance, is not
tradable for the owner. It is closed at the v2 start and never formally read; its rows
stay in the database under qualification v3.

## Deploy

The capture worker must run this code before 2026-09-29T00:00Z. If the deploy lands later,
move `IDENTITY_REGISTRY_V4_START` forward to the deploy time; it never moves earlier.
Captures before the start are recorded as `before_identity_registry_v4_activation`, with no
target sampling. Migration 0051 pins registry v4 on every v4-tagged qualification row.
