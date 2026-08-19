# Binance bookTicker capture v1

## What this is

First slice of the "capture non-recoverable data now, fit later" plan
agreed after the 2026-08-17 discovery-screen colleague review (see
[momentum-flow-bidirectional-burst-study-v1.md](momentum-flow-bidirectional-burst-study-v1.md)'s
own history): a real best-bid/ask feed for Binance, closing a gap that
existed since `cmd/momentumcapturebinance` first shipped.

Before this: `binance.Adapter` never implemented `momentumsource.
TickerSource`, so `LastBidPrice`/`LastAskPrice` on Binance bars were
permanently nil -- the only two columns in the shared bars table
(`timeseries.bybit_momentum_bars_1m`) Binance never populated (OHLC was
already fixed by `feat/momentum-trade-price-source-v1`). This mattered
concretely: the 2026-08-18 forensic read of the 8 real `momentum_flow_
paper_v1` stop_loss trades found exit spread consistently 2-6x wider than
entry spread right at the stop -- visible only as two snapshot points
(entry/exit quote), with nothing in between. A continuous per-minute bid/
ask now exists for Binance the same way it already did for Bybit.

## What was built

`internal/binance/bookticker.go` -- a new `RunBookTicker`/
`RunBookTickerWithLifecycle` WS stream (`internal/binance/trades.go`'s
own shard/reconnect pattern, copied deliberately, not abstracted: same
"documented duplication over a premature shared abstraction" convention
this package already uses). Subscribes to Binance's `<symbol>@bookTicker`
combined stream, best bid/ask only (not depth/quantity).

**Real bug found and fixed while writing this, caught by its own test**:
Binance's bookTicker payload carries both `"b"`/`"a"` (best bid/ask PRICE)
and `"B"`/`"A"` (their QUANTITY) in the same frame. Go's `encoding/json`
falls back to a case-INSENSITIVE match when a JSON key has no exact-tag
counterpart in the destination struct -- so a struct declaring only
lowercase-tagged `BidPrice string \`json:"b"\``silently received`"B"`'s
quantity value too (whichever key the object listed second overwrote the
first), clobbering the real price with the quantity. Fixed by declaring
explicit (otherwise-unused) `BidQty`/`AskQty`fields tagged`"B"`/`"A"`so
each JSON key gets its own exact-case match. Caught immediately by`TestHandleBookTickerPayloadNormalizesValidRows` before this ever touched
a real connection.

**Endpoint choice**: bookTicker stays on Binance's OLD unrouted `/stream`
path (`wsPublicBaseURL`), not the newer routed `/market/stream`
(`wsMarketBaseURL`, aggTrade/markPrice/kline/liquidations) --
`trades.go`'s own doc comment documents the 2026-08-15 incident this
split traces to (a `/stream` URL against a `market`-category stream name
completes the WS handshake successfully but never pushes a single
application frame). Using the wrong base URL here would silently
reproduce that exact failure mode for bookTicker specifically.

Wired into `cmd/momentumcapturebinance/main.go` via a new
`handleBookTicker` -- this process's second `AddTickerObservation`
producer alongside the existing OI-poll one (`handleOpenInterest`).
`BidPrice`/`AskPrice` only; `LastPrice`/`OpenInterest` stay nil on every
call, matching the engine's own documented "a delta can carry price with
no OI, OI with no price change, or neither" contract -- no migration, no
writer change: the `last_bid_price`/`last_ask_price` columns and their
write path already existed (Bybit has always populated them).

## What this PR does not do

- No lifecycle/reconnect counters wired for this feed (unlike the trade
  path's `tradeReconnectTotal`/`tradeReadTimeoutTotal`) -- `RunBookTicker`
  (not the `WithLifecycle` variant) is used, and `bookTickerStreamLoop`
  logs its own reconnects unconditionally. A follow-up if this feed's own
  reconnect rate turns out to matter operationally.
- No gap/discontinuity detector the way OI has `checkOpenInterestGaps` --
  a missing bid/ask for a few minutes degrades bar richness, not
  liveness the way a missing OI check protects against; not treated as
  an alarm-worthy condition in this first cut.
- Binance only. Bybit's own ticker stream already carries bid1Price/
  ask1Price per minute; no Bybit-side change needed or made here.
- Still per-minute-bar granularity (last observed value within the
  minute), not a continuous tick-by-tick capture -- matches the existing
  bars-based capture architecture's own resolution, not a new one.

## Running it

No new Makefile target: this is capture-path code, live in
`momentum-capture-binance` the same way trades/OI already are. `make
momentum-capture-binance-start` / `make prod-momentum-capture-binance-start`
picks it up automatically once deployed.
