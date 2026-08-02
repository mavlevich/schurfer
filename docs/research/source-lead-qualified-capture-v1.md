# Source lead qualified capture v1

Status: qualification foundation only. The bundled reviewed identity registry is
empty, no `gate_source_lead_4h_v1` cohort is registered, and no strategy settings
may change from this contract.

## Purpose

`source_lead_prospective_capture_v1` preserves raw Gate, Binance, and Bybit
observations under provisional `base_symbol_v1` identity. This layer does not mutate
those rows. It appends one qualification result per capture and qualification
version, so later review cannot rewrite the evidence that was visible at source
time.

## Reviewed identity registry

The packaged `source_lead_identity_registry_v1.json` is fail-closed, versioned,
and pinned to a canonical SHA-256 fingerprint by the qualification contract and
database constraint.
Each approved link must contain:

- one internal canonical asset id;
- the exact exchange and versioned instrument identity key;
- an HTTPS evidence URL from an authoritative venue or project source;
- a SHA-256 hash of the reviewed evidence.

The loader rejects malformed versions, duplicate instrument keys, invalid hashes,
and more than one live instrument version for the same canonical asset and exchange.
Equal base tickers, display names, or market ids never create an approval. Changing
any link requires a new registry and qualification version plus a new forward UTC
cutoff; existing qualification rows remain immutable.

The initial registry intentionally has no links. This makes deployment testable
without silently approving any asset. Captures will record
`source_identity_unapproved` until reviewed links are added.

## Deterministic venue selector

`lowest_round_trip_impact_v1` considers only target observations that:

- were sampled successfully;
- have an exact reviewed target identity linked to the same canonical asset as the
  reviewed Gate instrument;
- contain complete executable bid and ask depth for the configured $50 notional;
- have finite non-negative bid and ask impact.

The selector minimizes `bid_impact_bps + ask_impact_bps`. Exact ties are resolved by
the normalized exchange name, making the result independent of iteration order.
Fees are not added because this version ranks observed market quality rather than
claiming account-tier execution economics. The two target snapshots are captured
sequentially by the existing bounded worker, so reports must retain both timestamps
and cannot call them simultaneous.

Each result stores the qualification, registry, and selector versions plus the exact
registry fingerprint; status and reason; canonical asset id; selected exchange and
impact when qualified; notional; and per-target diagnostics. The readiness endpoint
fails closed if it ever observes more than one registry version or fingerprint under
the frozen qualification version. `identity_verified=false` on the original target
quote remains correct and unchanged.

## Operational alert

The notifier checks source-lead health on every normal notifier tick. A capture that
remains `collecting` for more than ten minutes produces one edge-triggered alert and
one recovery message. Critical abandonments (`capture_queue_full`, worker failure,
or shutdown timeout) produce one incident message per capture id. Routine lifecycle
closures such as `collector_process_restarted` and `capture_worker_cancelled` remain
visible in readiness metrics but never hold Telegram health red for 24 hours. Redis
keeps both de-duplication contracts across notifier restarts. Database read errors
are logged and never converted into a false health alert.

## Registration boundary

Before registering `gate_source_lead_4h_v1`:

1. add and independently review authoritative links for the intended asset scope;
2. bump the registry and qualification versions;
3. deploy and verify qualification/alert health;
4. choose the next clean UTC boundary after deployment;
5. freeze the selector, horizon, exit, cost, cash, and missing-data semantics.

Pre-cutoff provisional and empty-registry rows remain useful for capacity, latency,
and exclusion analysis, but they cannot support a PnL verdict.
