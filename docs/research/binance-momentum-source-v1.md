# Binance momentum source v1

Status: implemented and unit-tested, not wired into any running binary or
Compose profile. PR 4 of the 30-PR roadmap, following
`refactor/momentum-source-contract-v1` (PR 3).

## What this PR is and is not

**Is**: `apps/collector/internal/binance` -- a `Source` (universe fetch,
`aggTrade` trade streaming, `openInterest` polling) plus an `Adapter`
satisfying `momentumsource.UniverseSource`, `TradeSource`, and
`OpenInterestSource`. Every classification and endpoint choice traces to
`docs/research/binance-momentum-capability-preflight-v1.md`'s own
live-verified findings.

**Is not**: a running capture pipeline. No `cmd/` binary, no Compose
profile, no database writer. That is `feat/binance-momentum-capture-v1`
(PR 6)'s own explicit scope, per ROADMAP.md's item 7: "implement and
unit-test the Binance adapter, add a disabled Compose profile" are two
separate steps, and this PR is only the first. Nothing here has ever
opened a sustained connection to Binance's production servers as part of
this repository's own test suite; the unit tests below dial only local
`httptest`/fake-WebSocket servers.

## What this PR does not do

- **No `TickerSource` implementation.** Binance's own price-carrying
  streams are semantically different from what `TickerUpdate`'s fields
  promise: `markPrice` is a computed/smoothed value, not a last-trade
  price -- populating `TickerUpdate.LastPrice` from it would silently
  conflate two different kinds of price, the same class of mistake the
  capability preflight's own OI-value finding already warned against.
  `bookTicker` (best bid/ask) is a closer match for `Bid`/`Ask`
  specifically, but still leaves no source for `LastPrice`/24h fields
  without a THIRD stream (`!ticker@arr`). A correct, non-misleading
  `TickerSource` for Binance needs its own deliberate design pass, not a
  rushed reuse of Bybit's field shape.
- **No `openInterestHist` polling.** Per the capability preflight,
  `GET /futures/data/openInterestHist` does carry a native
  `sumOpenInterestValue`, but only at 5-minute-or-coarser granularity with
  several minutes of additional publication lag. `OpenInterestReading`
  from this package's `PollOpenInterest` is amount-only; `Value` and
  `ValueProvenance` stay the zero value on every reading (see
  `provenanceIfPresent`-equivalent handling in `adapter.go`'s
  `StreamOpenInterest`). Whether to capture the coarse value at all, as a
  clearly-labeled, separately-timestamped, lower-freshness field, is a
  design choice left to a later PR.
- **No live-verified WebSocket throughput, timestamp lag, or reconnect
  behavior against real Binance servers.** Exactly the gap the capability
  preflight already flagged as unmeasured (its own sandboxed environment
  could not sustain a live WS connection). This PR's shard size
  (`tradeStreamsPerConnection = 200`) matches Bybit's own conservative
  choice rather than assuming Binance's documented combined-stream limits
  are safe to use at full width without a bounded live probe -- that probe
  is still open work for `feat/binance-momentum-capture-v1`.

## Design decisions

**`Adapter` does not implement `OpenInterestSource` for Bybit but does for
Binance.** This is the correct, structural difference the capability
preflight predicted: Bybit's OI arrives embedded in its ticker push
(`bybit.Adapter` derives it via `OpenInterestFromTicker` instead of a
redundant second subscription -- see momentum-source-contract-v1.md),
while Binance's OI genuinely is a separate REST poll.
Implementing `OpenInterestSource` for real here, unlike Bybit, is not an
inconsistency -- it reflects what each venue actually offers.

**A `PollOpenInterest` consumer error is logged and does not stop the
poll loop.** Amended after a code-review finding, before any real wiring:
the first version propagated a `consume` error out of `PollOpenInterest`
entirely, meaning a single transient downstream failure (e.g. a NATS
publish hiccup) would silently end open-interest collection for every
symbol on the whole venue until the process restarted. Unlike a WebSocket
source, where a fatal read error genuinely means the connection is gone
and reconnecting is the natural response, a poll loop has no connection to
reconnect -- logging one bad reading and trying again next tick is
strictly better than stopping. `TestPollOpenInterestDoesNotStopOnAConsumerFailure`
covers this directly.

**The first poll fires immediately, not after waiting one full
`perSymbolDelay`.** Bybit's ticker push delivers OI with its very first
message; a poll-based source that waited a full interval before its first
reading would leave every symbol with no OI data at all for up to that
long after starting -- a real cold-start gap, not just a testing
convenience.

**Symbol case normalization applies to outbound requests, not only
inbound responses.** `fetchOpenInterest` now normalizes `symbol` before
building the request URL (a code-review finding): Binance's REST API
matches the `symbol` query parameter case-exactly, so a caller passing a
lower-case symbol would otherwise send a request Binance does not
recognize, not just receive a differently-cased response.

**`DefaultOpenInterestPollInterval` (60s) is sized against today's ~525
symbol universe, with no runtime guard tying it to the live catalog size
or the 2400/min weight budget.** `PollOpenInterest`'s own per-symbol
spacing (`interval / len(symbols)`) keeps per-symbol staleness pinned to
`interval` regardless of how many symbols are passed -- that part scales
correctly. What does NOT scale automatically is total request weight: it
grows in lockstep with symbol count, protected only by the comment's own
arithmetic, not a check against the live catalog or the budget itself. Not
a bug today (nothing calls this outside tests), but PR 6's own wiring
should either compute the interval from the live universe size or assert
a floor, not inherit the fixed constant unexamined.

## Shared adapter helpers

`apps/collector/internal/wsstream` was extracted during this PR (a
code-review finding): read-liveness deadline management, read-timeout
classification, session-id generation, slice chunking, and finite-
positive-number validation were copy-pasted verbatim from
`apps/collector/internal/bybit` into this package's first draft. A second
code-review pass on the same PR found `normalizeSymbol` had ALSO been
copy-pasted between the two packages (missed by the first extraction,
since it isn't a WebSocket-specific helper) and moved it into the same
package as `NormalizeSymbol`. A third pass found the `httpClient`/`restURL`
nil-fallback idiom duplicated between this package's own `binance.go` and
`openinterest.go`; that one is package-local (each venue's `Source` type
differs, so it cannot live in `wsstream`) and became two small methods on
`binance.Source` instead: `httpClientOrDefault`/`restURLOrDefault`.

Both `bybit` and `binance` now depend on the one shared `wsstream`
implementation. `bybit`'s own `ws.go`/`trades.go` keep thin same-named
wrapper functions delegating to it, so existing Bybit call sites and tests
needed no changes beyond the function bodies; `binance` calls `wsstream`
directly with no such legacy call sites to preserve. New adapters should
follow `binance`'s pattern (direct calls), not `bybit`'s (wrappers exist
only for its own pre-existing history) -- see `wsstream`'s own package
doc comment for this convention stated explicitly.

## What PR 6 (`feat/binance-momentum-capture-v1`) inherits

- A bounded live probe against real Binance servers (message rates,
  timestamp lag, `aggTrade` id contiguity, reconnect behavior) before
  choosing a final shard size or trusting this package's error-recovery
  paths under real network conditions.
- The actual capture pipeline: a `cmd/` binary, a disabled-by-default
  Compose profile, a database writer, and per-venue health telemetry
  (ROADMAP.md's own item 7 activation sequence).
- A decision on `TickerSource` and `openInterestHist` (see "What this PR
  does not do" above).
