# Source lead qualified capture v1

Status as of research/gate-source-lead-registry-activation-v3 (PR 3 of 3,
2026-08-29/30): the reviewed identity registry is no longer empty. It carries 14
canonical assets (28 gate/binance links), each backed by both on-chain asset-identity
evidence (research/gate-source-lead-registry-activation-v2) and independently fetched
derivative-market evidence (research/source-lead-derivative-market-evidence-v1),
cross-checked at load time. `ROUTE_EVIDENCE_INDEPENDENTLY_VERIFIED` is `True` --
`qualify_source_lead` can now actually return `status='qualified'`, for captures at or
after `IDENTITY_REGISTRY_V3_START`. No `gate_source_lead_4h_v1` cohort is registered
yet and no strategy settings may change from this contract alone -- registration still
follows the boundary below. The rest of this document describes the mechanism, which
is unchanged in shape across v1/v2/v3; version-specific numbers (registry version,
fingerprint, cutoff) live in `source_lead_qualification.py` and
`source_lead_contract.py`, not duplicated here.

## Purpose

`source_lead_prospective_capture_v1` preserves raw Gate, Binance, and Bybit
observations under provisional `base_symbol_v1` identity. This layer does not mutate
those rows. It appends one qualification result per capture and qualification
version, so later review cannot rewrite the evidence that was visible at source
time.

## Reviewed identity registry

The packaged registry (`source_lead_identity_registry_v3.json` as of PR 3; see
`DEFAULT_REGISTRY_RESOURCE` in `source_lead_qualification.py` for whichever version is
actually live) is fail-closed, versioned, and pinned to a canonical SHA-256 fingerprint
by the qualification contract and database constraint. Each approved link must
contain:

- one internal canonical asset id;
- the exact exchange and versioned instrument identity key;
- an HTTPS evidence URL from an authoritative venue or project source;
- a SHA-256 hash of the reviewed evidence.

The loader rejects malformed versions, duplicate instrument keys, invalid hashes,
and more than one live instrument version for the same canonical asset and exchange.
Equal base tickers, display names, or market ids never create an approval. Changing
any link requires a new registry and qualification version plus a new forward UTC
cutoff; existing qualification rows remain immutable.

The v1 registry intentionally shipped with no links, so deployment was testable
without silently approving any asset -- every capture recorded
`source_identity_unapproved` until reviewed links existed. The now-live v3 registry
carries 14 approved canonical assets; a capture whose source or target identity is
not one of those still records `source_identity_unapproved` exactly the same way,
fail-closed by construction, not by an empty table.

## Identity review queue

`source_lead_identity_review_v1` turns the raw prospective captures into a bounded,
reproducible manual-review queue. It groups the exact Gate identity that existed at
source time with the exact Binance/Bybit instrument identities and executable $50
quotes observed by the forward collector. The report includes the full capture
denominator, an input fingerprint, first/last observation times, instrument-version
conflicts, exact target metadata, and a deliberately non-loadable registry skeleton.

The skeleton contains `review_status=unapproved` and null evidence fields. It is a
work queue, not a registry update. Its proposed internal asset id is only a stable
label for review; equal base tickers never authorize a link. An independent reviewer
must verify authoritative project or venue evidence, archive the reviewed payload,
record its SHA-256, and approve every exact instrument key in a new versioned
registry. The Research page exposes only raw point-in-time Gate groups, raw persisted
source conflict flags, and executable-target coverage continuously, so missing
identities do not require manual SQL to discover. It deliberately does not reproduce
the report's cross-group classification logic; this Python report is the sole source
of `review_state` and `review_flags`.

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
impact when qualified; notional; and per-target diagnostics. The readiness endpoint's
source-lead query is not scoped to one qualification version (colleague review,
2026-08-29/30, PR 3 review round -- an earlier version was, which made every
pre-cutover capture's already-computed qualification disappear from every count the
moment the live qualification version bumped), so a window spanning a version
activation genuinely can carry more than one registry version or fingerprint; the
endpoint surfaces that as `identity_registry_mixed=true` rather than erroring.
`identity_verified=false` on the original target quote remains correct and unchanged.

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

1. run `source-lead-identity-report` and resolve every conflict in the intended scope;
2. archive and independently review authoritative evidence for each exact link;
3. add approved links and bump the registry and qualification versions;
4. deploy and verify qualification/alert health;
5. choose the next clean UTC boundary after deployment;
6. freeze the selector, horizon, exit, cost, cash, and missing-data semantics.

Pre-cutoff provisional and empty-registry rows remain useful for capacity, latency,
and exclusion analysis, but they cannot support a PnL verdict.
