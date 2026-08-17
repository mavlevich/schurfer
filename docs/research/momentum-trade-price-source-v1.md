# Momentum trade-derived price source v1

## What this PR fixes

Root cause 1 of the incident documented in
[binance-watch-input-readiness-v1.md](binance-watch-input-readiness-v1.md):
Binance capture bars had `close_price` (and `open_price`/`high_price`/
`low_price`) permanently NULL, because OHLC construction was exclusively an
`AddTickerObservation` concern in `momentum.Engine`, and Binance has no
ticker/price feed at all (`binance.Adapter` deliberately does not implement
`momentumsource.TickerSource`; open interest is REST-polled with no price
attached). Every `momentum_flow_watch_binance` evaluation was rejected with
`missing_price`.

Combined into this same PR (agreed explicitly, not scope creep): a second,
independently colleague-found bug in `binance.PollOpenInterest`'s own
`ObservedAt` timing -- `ObservedAt` was captured with `time.Now()`
_before_ `client.Do()` instead of after the response body was read and
decoded, so it measured "when we started asking" rather than "when we
actually observed this reading." Both changes touch the same capture path
and ship together as one migration/one deploy.

## The fix: a configurable PriceSource, not a Binance special case

`momentum.Engine` gains a `PriceSource` type, fixed per-`Engine` at
construction (`NewWithPriceSource`), never inferred per-call or switched
mid-stream:

- `PriceSourceTickerLast` -- Bybit, unchanged: OHLC comes from
  `AddTickerObservation`'s own `LastPrice`, arrival-order Open/Close (first
  observation this bucket is Open, most recent is Close). `momentum.New()`
  still defaults to this, so nothing not explicitly opted in changes.
- `PriceSourceAggregateTrade` -- Binance, new: OHLC comes from `AddTrade`'s
  own accepted trade prices.

`cmd/momentumcapture` (Bybit) explicitly constructs
`NewWithPriceSource(PriceSourceTickerLast)`; `cmd/momentumcapturebinance`
constructs `NewWithPriceSource(PriceSourceAggregateTrade)`. Bybit's own code
path (`AddTickerObservation`'s existing `Ticker*` fields, its own Open/High/
Low/Close derivation) is untouched -- proven by the full existing Bybit test
suite passing unmodified, plus a same-shape real-Postgres round-trip test
covering both venues (see "Testing" below).

### Why trade-derived Open/Close uses EventAt, not arrival order

Bybit's existing ticker-derived Open/Close trusts arrival order: the first
`AddTickerObservation` call received this bucket sets Open, the most recent
sets Close. That is safe for Bybit because its ticker feed is relayed
through this repo's own internal NATS bus with strong per-symbol ordering.

Binance's public aggTrade WebSocket stream carries much weaker ordering
guarantees -- out-of-order delivery under reconnect/resubscribe is a
realistic, not theoretical, case. Trusting arrival order there would let a
late-arriving early trade silently overwrite Close with a stale price, or a
reordered burst corrupt Open. `recordTradePriceObservation` instead uses
each trade's own `EventAt` (exchange-assigned event time, not local receipt
time): Open is the price of whichever accepted trade has the _earliest_
`EventAt` seen so far for this bucket (self-correcting if a still-earlier
trade arrives after a later one already set Open); Close is the _latest_
`EventAt` seen so far, by the same rule. High/Low remain plain running
min/max, which don't depend on ordering at all. Covered by a dedicated test
(`TestAddTradeOutOfOrderEventAtCorrectsOpenAndClose`) asserting a
late-arriving earlier-`EventAt` trade retroactively fixes Open, and a
late-arriving trade with an _earlier_ `EventAt` than the current Close does
**not** retroactively become Close.

## Canonical, venue-agnostic price provenance

Both `AddTickerObservation` (Bybit) and `AddTrade` in trade-price mode
(Binance) populate the same set of fields on `Bar`, so a downstream
consumer never has to branch on `PriceSource` to find the right timestamp:

- `PriceSource` -- which observation type this bar's own OHLC actually came
  from.
- `PriceObservedThisMinute`, `First/LastPriceEventAt`,
  `First/LastPriceReceivedAt` -- populated identically in spirit by both
  paths (Bybit mirrors its own existing `Ticker*` fields verbatim; Binance
  populates them from accepted aggTrade prices).

### Capability-specific completeness: additive, not a rename

`OpenInterestComplete`/`PriceComplete` are new fields alongside the
existing `TickerComplete`/`TradesComplete`, not a replacement.
`finalizeBar` sets `OpenInterestComplete = TickerComplete` and
`PriceComplete` from `TradesComplete` (trade-price mode) or `TickerComplete`
(ticker mode) -- the same underlying feed-health signal, under a name that
stays accurate for Binance specifically, whose only `AddTickerObservation`
caller is its OI poller (see `handleOpenInterest` in
`cmd/momentumcapturebinance/main.go`), never a real ticker. Renaming
`TickerComplete`/`TradesComplete` themselves would be a bigger, separate
change (an explicit colleague-review decision, not an oversight) --
`Bar.Complete = TickerComplete && TradesComplete` is unchanged, and remains
equivalent to `OpenInterestComplete && PriceComplete` by construction.

## Evaluator: stale_quote reads the canonical field, keeps its name

`momentum_flow_watch_evaluator.prepare_symbol_evaluation`'s `stale_quote`
check switched from reading `last_ticker_received_at` to
`last_price_received_at`. This is a real behavior fix for Binance, not a
cosmetic rename: `last_ticker_received_at` was, for Binance, secretly the
OI poller's own timestamp (its only `AddTickerObservation` caller), so the
freshness check was measuring "is the OI poller alive" rather than "is our
price fresh." `last_price_received_at` is the canonical field described
above; for Bybit it matches `last_ticker_received_at` whenever a ticker
observation carries a `LastPrice` (every call in normal operation), so this
is a no-op on that venue in practice.

The `stale_quote` reason-code string itself is deliberately **not**
renamed or versioned -- an explicit colleague-review instruction: "либо
оставить `stale_quote` для v1 и добавить детали `price_source`; либо
версионировать taxonomy/reason schema... Тихо менять reason code в
существующем контракте не стоит." Silently changing a reason code inside
the frozen v1 `QualityReason` contract is the thing to avoid; changing what
data feeds an unchanged reason code, to fix what it was silently measuring
wrong for one venue, is not the same thing.

## Schema

`0030_momentum_bars_trade_price_source_v1`: 8 new nullable columns on
`timeseries.bybit_momentum_bars_1m` (`price_source`,
`first_price_event_at`, `last_price_event_at`, `first_price_received_at`,
`last_price_received_at`, `price_observed_this_minute`,
`open_interest_complete`, `price_complete`). All nullable, no backfill:
every column describes something never computed for any bar written before
this migration, and NULL honestly means "not tracked at the time," not a
default implying a false negative.

`momentum_flow_watch_repository.py`'s `_bars` table definition and
`WatchBar` dataclass carry the same 8 fields through to the evaluator.

## Testing

- `momentum_price_source_test.go` (new, 8 tests): PriceSource
  default/explicit construction, trade-derived OHLC construction, the
  out-of-order `EventAt` correctness case above, late-trade
  non-retroactivity, duplicate-trade-ID safety, `AddTickerObservation`
  field mirroring, `OpenInterestComplete`/`PriceComplete` mirroring.
- `openinterest_test.go`: `RequestedAt` non-zero,
  `ObservedAt >= RequestedAt`, and a dedicated test using an
  `httptest.Server` with an artificial response delay proving `ObservedAt`
  is captured after the response is read, not before the request is sent.
- `writer_integration_test.go` (extended, real Postgres): the 8 new columns
  round-trip correctly, including the harmless-retry-vs-genuine-collision
  `payload_hash` distinction still holding with the wider row.
- `test_momentum_flow_watch_evaluator.py` /
  `test_momentum_flow_watch_worker.py`: fixture bars extended with the
  canonical price fields (mirroring Bybit's own real shape); `stale_quote`
  behavior proven unchanged for Bybit-shaped input.
- `test_momentum_flow_trade_price_source_integration.py` (new, real
  Postgres): the actual gap identified in
  binance-watch-input-readiness-v1.md's own "Process critique" --
  `momentum_flow_watch_evaluator`'s tests always supplied synthetic
  `close_price`, a shape the real Binance producer had never once
  produced. This test seeds real Postgres rows shaped exactly as the Go
  writer now persists them, for both `price_source` values, and proves
  each clears `prepare_symbol_evaluation`'s full quality gate (no
  `missing_price`, no `stale_quote`, `quality_ready = True`) through the
  same repository/evaluator code path production uses -- for both venues,
  with no venue-specific evaluator branch.

Full verification: `go build ./...`, `go vet ./...`, `go test ./...`
(collector module) and `uv run pytest` / `ruff check` / `mypy` (analytics
package) all clean at time of writing.

## What's next

- PR3 `fix/binance-oi-poll-scheduler-v1`: root cause 2 from
  binance-watch-input-readiness-v1.md (`missing_fresh_oi`, ~94% of
  evaluations) -- bounded concurrent OI polling with a token bucket sized
  against Binance's real `GET /fapi/v1/openInterest` cost (weight 1,
  2400 weight/min budget), targeting roughly 25-30s per-symbol cadence
  instead of the current 60s round-robin's measured p50 127s / p95 255s
  real refresh gap.
- PR4 `analysis/binance-watch-input-coverage-v1`: 24-48h coverage
  measurement once PR2+PR3 are both live -- descriptive only.
- PR5 `feat/binance-momentum-watch-v2` (conditional on PR4's findings).
  `momentum-watch-binance`/`momentum-paper-binance` stay stopped on prod
  until PR3 and a coverage read land.
