# Multivenue canary telemetry v1

Status: merged, not deployed. PR 5 of the 30-PR roadmap, following
`feat/binance-momentum-source-v1` (PR 4).

## What this PR is and is not

**Is**: a scoping fix to `momentumcapture.Health`/`RedisStore` so that a
second venue's momentum-capture process can publish its own health
snapshot without colliding with the first venue's. Nothing here changes
what any process actually captures or measures.

**Is not**: the Binance capture process itself. No `cmd/` binary, no
Compose profile changes, no new data path. That is
`feat/binance-momentum-capture-v1` (PR 6)'s own scope.

## The problem

`momentumcapture.RedisStore.HealthKey` was a single constant,
`market:momentumcapture:health`, with no venue in it at all. Every field
this session's earlier work added to `Health` (universe counts, queue
depths, persist errors, latency histograms) was written to that one key,
because only one venue's process (Bybit) has ever existed.

Once `feat/binance-momentum-capture-v1` starts a second momentum-capture
process against the same Redis instance, it would have published its own
`Health` snapshot to the exact same key. Depending on which process
happened to publish last on any given 5-second tick, `redis-cli HGETALL
market:momentumcapture:health` would show ONE venue's numbers with no
indication the other venue's process even exists -- not a crash, not an
error, just one venue's health silently standing in for both. That is
the same masking-counter failure class as the earlier Bybit universe
remediation incident, but for observability instead of accounting: a
sick venue's counters invisible behind a healthy one's.

## The fix

- `Health` gained an `Exchange` field.
- `HealthKey` became a function of the exchange
  (`market:momentumcapture:health:<exchange>`) instead of a bare
  constant.
- `RedisStore.StoreHealth` fails closed if `Exchange` is unset, rather
  than defaulting to some venue: a forgotten `Exchange` assignment must
  surface as a loud error from the process that forgot it, not a quiet
  write to a malformed key.
- `cmd/momentumcapture/main.go` (the Bybit process) now stamps
  `Exchange: "bybit"` on every `Health` it builds.
- `infra/scripts/momentum_canary_checkpoints.py`'s `HEALTH_KEY` constant,
  the two Makefile health targets (`momentum-capture-health`,
  `prod-momentum-capture-health`), and every doc/help-text reference to
  the old key were updated to `market:momentumcapture:health:bybit`.
  This script stays deliberately Bybit-only for now (ROADMAP item 6's own
  framing); a Binance canary-checkpoints runner is its own future script
  once PR 6 exists, not a parameter bolted on ahead of that.

## What this does not attempt

- No change to `momentumcapture.Writer`'s database rows: those already
  carry their own `Exchange` column (`writer.go`'s `Row.Exchange`,
  populated from `Writer`'s own `exchange` field at construction) and
  were never at risk of cross-venue collision -- this PR closes the one
  remaining unscoped shared key, in Redis health telemetry specifically.
- No Binance-side wiring. `binance.Adapter` (PR 4) has no health
  publisher yet; that arrives with the actual capture process in PR 6.
- No change to what any dashboard or alert threshold considers healthy --
  `deriveHealthStatus`'s own logic is untouched.

## Operational note

This PR modifies `cmd/momentumcapture/main.go`, the source of the
currently-running Bybit momentum-capture process (the fixed-window canary
ROADMAP item 6 is measuring toward its 24/48/72-hour checkpoints).
Merging to `main` does not deploy or restart anything by itself, but
deploying this change WILL restart that process (a new `StreamSessionID`,
a fresh `Universe` fetch, momentarily zeroed in-process counters) since
the binary itself changes. Per the standing deploy discipline for this
codebase, deploy is a separate, explicitly confirmed step -- and it
should wait until this section's own item 6 checkpoint decision is made,
not land in the middle of the window it is trying to measure cleanly.
