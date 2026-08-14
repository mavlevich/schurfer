# Momentum source contract v1

Status: additive only. No behavior change to the running momentum-capture
binary; `cmd/momentumcapture/main.go` is untouched and still talks to
`bybit.Source` directly. This PR proves the narrow interfaces
`docs/research/momentum-venue-capability-matrix-v1.md`'s architectural
decision calls for actually fit Bybit's real code, by building and testing
a Bybit adapter against them -- it does not yet rewire the live binary onto
those interfaces.

## What this PR is and is not

**Is**: `apps/collector/internal/momentumsource` (the shared, venue-agnostic
interfaces and envelope types) plus `apps/collector/internal/bybit`'s new
`Adapter` (translates the existing, unmodified `bybit.Source` into those
interfaces). Both are new files; nothing in `bybit.go`, `trades.go`, or
`ws.go` changed. `cmd/momentumcapture/main.go` does not import either new
package.

**Is not**: rewiring the live collector binary to consume `Adapter` instead
of `bybit.Source` directly. That is a separate, deliberately deferred step.
The currently-running momentum-capture instance is the corrected canary
ROADMAP.md's item 6 is actively measuring toward its own 24/48/72-hour
checkpoint; changing its internal wiring now, even in a behavior-preserving
way, adds risk to that measurement window for no benefit this PR needs. Do
that rewiring as its own reviewed step once this contract has had a chance
to be read, not folded silently into this one.

## Interfaces

`UniverseSource`, `TradeSource`, `TickerSource`, `OpenInterestSource` --
matching the roadmap's own PR 3 scope. Each accepted event carries a shared
`Envelope` (exchange, market type, native market id, exchange event time,
local receive time, session id), per the capability matrix's own
"Architectural decision" section.

`UniverseSnapshot.Validate()` enforces the same fail-closed accounting
`bybit.validateCatalog` already checked privately (every catalog item
included or excluded under a named, counted reason, nothing left
unclassified) -- now as a reusable contract any future venue's
`UniverseSource` is checked against, not a Bybit-only habit. This is the
same bug class the "Bybit universe remediation" section of the capability
matrix records fixing once already; the contract makes a regression to an
opaque single exclusion count structurally harder to ship.

## Finding: OpenInterestSource cannot double-subscribe Bybit

The capability matrix's own architecture diagram shows a Bybit-adapter
edge going straight into `OpenInterestSource`, as if it were on equal
footing with `TradeSource`. Building the actual adapter surfaced why that
is not quite right: Bybit's
own OI reading arrives embedded in the same ticker push `TickerSource`
already streams (see `ws.go`'s `tickerState`). If `Adapter` also
implemented `OpenInterestSource` as a second call into `Source.Run`, a
consumer that used both interfaces (the eventual canonical momentum-capture
loop, once it exists) would open a REDUNDANT second WebSocket subscription
to the exact same ticker topics just to satisfy the interface -- doubling
Bybit-side connection count and subscription load for data already being
received once.

`Adapter` therefore does not implement `OpenInterestSource`. Instead,
`OpenInterestFromTicker(TickerEvent) (OpenInterestReading, bool)` derives
the same reading from an already-consumed `TickerUpdate`, with zero
additional network activity. A venue whose OI genuinely is a separate
transport implements `OpenInterestSource` for real -- per the capability
preflight, Binance's OI is a distinct REST poll (`GET /fapi/v1/openInterest`,
or the coarser `openInterestHist` for value), so
`feat/binance-momentum-source-v1` (PR 4) is expected to implement
`OpenInterestSource` properly, unlike Bybit.

This is a genuine, load-bearing finding from actually building the
contract, not a stylistic preference -- the capability matrix's own
diagram is updated below to stop implying otherwise.

## Finding: per-trade session id needs care, not a free field

`bybit.PublicTrade` (the type `RunTrades`/`RunTradesWithLifecycle` already
deliver) carries no per-trade session id -- only the separate
`TradeLifecycleEvent` callback does, at the shard level (a shard being a
fixed symbol subset on one physical connection; `RunTradesWithLifecycle`
runs one goroutine per shard, each with its own session id).
`Adapter.StreamTrades` tracks the latest session id per NATIVE MARKET ID (not a
single shared variable, since multiple shards run concurrently and the
lifecycle/trade callback pair is shared across all of them), updated
synchronously on each shard's own "connected" event before any trade for
that shard's own symbols can arrive. See `adapter.go`'s own comment for the
exact ordering argument this relies on.

## Finding: TickerSource cannot promise the same error semantics as TradeSource

Self-reviewed with `/code-review` before this PR (same as the preflight):
found that `ws.go`'s own `handleTicker` only logs a `consume` error
(`slog.Warn`) and keeps the connection running -- it does not return the
error, unlike `trades.go`'s `handleTradePayload`, which does propagate a
`consume` error and causes `tradeStreamLoop` to treat it as a stream
failure and reconnect. `Adapter.StreamTicker` wraps `Source.Run`
unchanged, so it inherits this asymmetry: a `momentumsource.TickerConsumer`
that returns a non-nil error is silently swallowed for Bybit, while the
equivalent `TradeConsumer` error is not. Documented on `TickerConsumer`,
`Adapter.StreamTicker`, and the `TickerUpdate.OpenInterest` field comment
(which separately overclaimed that a nil `OpenInterest` always means "this
venue's transport has no OI" -- for Bybit specifically it can also mean
"not yet observed this connection episode," a transient condition the
original wording did not distinguish). Covered by
`TestAdapterStreamTickerSwallowsConsumerErrorsWithoutReconnecting`, which
proves the swallowing against a real WebSocket test server rather than
asserting it from reading the code alone.

## Contract tests

`momentumsource_test.go` covers `UniverseSnapshot.Validate()`'s own
invariants directly (exact accounting, duplicate detection, required venue
identity). `bybit/adapter_test.go` covers the Bybit adapter's translation
functions against synthetic Bybit-shaped inputs (`translateTrade`,
`translateTicker`, `OpenInterestFromTicker`, `translateUniverse`), plus one
end-to-end test against a local WebSocket test server proving the
session-id threading finding above actually works, not just reads
plausibly. `TestAdapterFetchUniverseUsesTheSameStrictCryptoPerpetualCatalog`
reuses the existing `instrument()`/`writeInstrumentResponse()` REST test
fixtures `bybit_test.go` already has, rather than inventing a second set.

## Capability matrix diagram update

The architecture diagram in `momentum-venue-capability-matrix-v1.md` is
updated to route Bybit's OI through `TickerSource` (matching what actually
got built) rather than showing a separate `OpenInterestSource` edge for
Bybit specifically; Binance keeps its own direct `OpenInterestSource` edge,
since that one is real.

## What PR 4 (`feat/binance-momentum-source-v1`) inherits

- `UniverseSource`: implement against the preflight's own findings
  (`contractType=TRADIFI_PERPETUAL` and `underlyingType=INDEX` as named,
  counted exclusion reasons; periodic `exchangeInfo` re-polling for the
  universe churn the preflight measured).
- `TradeSource`: `aggTrade`, with the granularity-mismatch constraint
  already recorded in the capability matrix carried into the envelope
  (e.g. a note that `Size`/notional over a window remains comparable, a
  single `Trade` does not).
- `OpenInterestSource`: implemented for real (poll-based), with
  `AmountProvenance`/`ValueProvenance` distinguishing the near-real-time
  `openInterest` amount from the 5-minute-or-coarser `openInterestHist`
  value, per the preflight's own finding -- never both reported as
  equally fresh.
