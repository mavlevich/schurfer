# Source lead prospective capture v1

Status: collection only. No cohort is registered yet, and this contract cannot
change production entry, exit, venue, score, size, leverage, or `DRY_RUN=true`.

## Purpose

Historical OBS-013 used later Binance/Bybit confirmation to construct its sample.
That was useful discovery but selected the winners retrospectively. This capture
records every newly observed Gate lead from a live scanner session, including
exclusions, missing target instruments, quote failures, and leads that never receive
a later confirmation.

`source_lead_prospective_capture_v1` is a measurement contract, not
`gate_source_lead_4h_v1`. A formal strategy contract may be registered only after a
healthy deployment establishes a new forward UTC cutoff. Pre-cutoff rows are smoke
data and never evidence for that strategy.

## Point-in-time denominator

- The scanner process start is a hard left-censoring fence. A Gate source first
  observed before that process start is never backfilled after a restart.
- Every new event that contains a Gate observation is classified once. Exact
  earliest-observation ties are stored and excluded rather than broken by ordering.
- A source is capture-eligible only when Gate is the unique earliest observed venue,
  its venue-local identity is complete and non-conflicting, and it is a linear USDT
  swap.
- Later source rows and cross-venue confirmation never alter the stored trigger.
  Confirmation is a future outcome for a later report.
- The current scanner measurement threshold still defines which Gate observations
  exist. This limitation must appear in every capacity denominator.

The historical counts have three different meanings and must not be conflated:

- all mature Gate-first observations;
- the subset with a target perp available at source time;
- the still narrower subset that confirms later.

Only the second can become an eligible prospective trading denominator. The third is
an outcome label, never a source-time selector.

## Target capture

For each source-eligible event, Binance and Bybit are processed sequentially. Each
attempt records:

- active exact-base linear USDT-perp metadata and onboarding time;
- exchange ticker timestamp and scanner observation timestamp;
- last price, rolling percentage change, and quote volume when available;
- best bid/ask, spread, and executable bid/ask VWAP impact for the configured $50
  notional from a bounded 50-level book;
- latency, failure status, and bounded error provenance.

The four-time envelope is explicit. Exchange ticker time is `occurred_at`, an
official publication time is unavailable for this market-data event,
`first_observed_at`/`observed_at` is Schurfer time, and row `created_at` is ingestion
time.

## Identity boundary

The initial route lookup is `base_symbol_v1`. It is stored with
`identity_verified=false`, even when both venue-local instruments are internally
consistent. Equal tickers do not prove equal underlying tokens. These captures are
valid market observations, but a registered trading contract must use a versioned
canonical identity approval established before its cohort starts.

No later identity approval may rewrite the captured source, target metadata, quote,
or timestamp. Reports must expose provisional, approved, rejected, and unknown
identity states separately.

The append-only qualification foundation is specified in
[`source-lead-qualified-capture-v1.md`](source-lead-qualified-capture-v1.md). It keeps
the original `identity_verified=false` observation intact and records reviewed
identity plus deterministic venue selection separately.

## Operational bounds and failure policy

- Every new source candidate in the scan is classified and durably inserted after
  the normal Redis publication; the denominator is never truncated by a network
  concurrency limit.
- Eligible claims are split into batches of at most eight by default. Network work
  runs in one background worker with at most 16 queued batches. The scanner,
  OI/funding snapshots, retrace housekeeping, and the next publication do not wait
  for Binance/Bybit.
- One target exchange client is live at a time; each market/quote operation has a
  five-second default timeout.
- One event/version row and one target/exchange row are enforced by unique indexes.
- Source exclusions and target `not_listed`, `inactive`, `listed_after_source`,
  `onboarding_unknown`, and `fetch_failed` results remain in durable coverage.
- Queue overflow, worker failure, cancellation, and shutdown timeout convert an
  already-durable claim to `abandoned` with an explicit bounded error. They never
  disappear from the denominator.
- Capture exceptions do not block the existing pump feed or paper strategy. Trading
  and future scanner cadence are therefore isolated from measurement availability.
- A hard process crash can temporarily leave a `collecting` row. The next scanner
  process marks claims owned by the previous process `abandoned` rather than
  recapturing them with a later quote; health checks expose any row that still
  remains collecting.

## Registration boundary

After deployment, migration, and a healthy smoke window, choose the next clean UTC
boundary as `cohort_start`. A future `gate_source_lead_4h_v1` contract must freeze:

- one canonical identity version;
- target eligibility and deterministic venue selection from source-time quotes;
- one 240-minute primary horizon and one fixed-risk exit policy;
- fees, impact, funding, missing-data, stop, and cash semantics;
- minimum 100 complete eligible events, 30 base clusters, and four UTC weeks;
- source leads/day, target-eligible leads/day, trades/day, confirmed winners/day,
  precision/recall, net/day, capacity, MAE/MFE, and cluster inference.

Thirty- and 480-minute rows may remain secondary diagnostics. MEXC may be retained as
a secondary measurement source, but it is not part of this Gate-primary capture.
