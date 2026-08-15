# Binance momentum capture v1

Status: implemented and unit-tested. Compose profile disabled by default,
gated behind an explicit host-capacity check on the prod start make
target. PR 6 of the 30-PR roadmap, following
`feat/binance-momentum-source-v1` (PR 4) and
`refactor/momentum-canary-multivenue-v1` (PR 5).

## What this PR is and is not

**Is**: `apps/collector/cmd/momentumcapturebinance` -- a complete second
capture binary, structurally parallel to `cmd/momentumcapture`, wired into
a disabled-by-default Compose profile (`momentum-capture-binance`) in both
`infra/docker/docker-compose.dev.yml` and `docker-compose.prod.yml`, with
its own start/stop/health Makefile targets (dev and prod, the prod start
target gated by the same RAM/disk capacity check pattern
`prod-momentum-capture-start` already uses).

**Is not** activated. ROADMAP item 7 is explicit: "activate it only after
a corrected Bybit checkpoint and host-capacity gate pass." Nothing in this
PR starts the Binance process anywhere; the profile stays off until a
human runs the make target deliberately, after that gate passes.

## Why a separate binary, not one venue-parameterized binary

The roadmap's own "Bybit, Binance, and combined always stay separate
books" rule, applied to the process boundary too: a bug in one venue's
capture process must never be able to take the other down. Beyond that
rule, the two venues' actual data shapes differ enough that a shared
abstraction would cost real clarity for no present benefit -- see the next
section.

## The structural difference: no ticker feed at all

`cmd/momentumcapture`'s ticker/OI feed arrives over NATS from a SEPARATE
process (`cmd/collector`). `cmd/momentumcapturebinance` has no equivalent:
`binance.Adapter` deliberately does not implement
`momentumsource.TickerSource` (see docs/research/binance-momentum-source-
v1.md), so this process's only source of price-or-OI information is
`binance.Source.PollOpenInterest`, run in-process, with no price/bid/ask at
all.

`momentum.Engine` only ever sets `OpenPrice`/`HighPrice`/`LowPrice`/
`ClosePrice`/`LastBidPrice`/`LastAskPrice` from `AddTickerObservation`.
Every bar this process produces will have all six of those fields
permanently nil. This is a real, known limitation of what Binance momentum
bars can show in v1, not a bug: `TickerObservation`'s own contract already
supports "a delta can carry price with no OI, OI with no price change, or
neither", and this process's calls are always the OI-only case. Any
research reading Binance's own bars needs to know this going in --
`buy_total_notional_usd`/`sell_total_notional_usd`/histograms/OI are real,
OHLC/bid/ask are not there.

## Design decisions

**Open-interest gap detection is the ONLY discontinuity mechanism for this
feed, not a backup to reconnect-based detection.** `cmd/momentumcapture`
gets ticker discontinuity two ways: proactively (`checkTickerGaps`, a
silence threshold) and reactively (a `StreamSessionID` change on the next
message, revealing a reconnect already happened). A REST poll has no
"session" to change, so `checkOpenInterestGaps` -- the direct analog of
`checkTickerGaps` -- is this process's only signal.
`openInterestGapThreshold` (180s, 3x the default OI poll interval) is
picked with the same "conservative multiple, not a live-measured
distribution" honesty `cmd/momentumcapture` already applies to its own
threshold.

**Trade reconnect/read-timeout counters are tallied from the lifecycle
callback directly, not read from a `Source.StreamStats()` method.**
`bybit.Source` has one; `binance.Source` does not, and adding one purely
for this PR would mean touching already-merged, already-tested
`internal/binance` files for a single call site. `handleLifecycle` already
receives every disconnect as a `TradeLifecycleEvent` with its own
`ReadTimeout` flag -- tallying from that is equally accurate and keeps
this PR additive-only against `internal/binance`.

**`latency.go` (and `deriveHealthStatus`) are deliberate, documented
duplicates of `cmd/momentumcapture`'s own copies, not a forced choice.**
`internal/wsstream` (PR 4) already proves a shared package can be
extracted with the already-merged consumer's call sites left completely
unchanged (thin same-named wrapper functions); the same trick was
available here. It was not taken: unlike `wsstream`, this is pure
future-maintenance risk reduction (two copies could drift), not something
correctness needs today, and `cmd/momentumcapture/main.go` is the source
of the currently-running Bybit canary process -- touching it again this
session for a no-behavior-change refactor is a cost this PR chose not to
pay. Extracting both into a shared package is legitimate follow-up work,
not a closed question. Same spirit as `FetchSymbolCatalog`'s retry loop
and `validateCatalog`'s accounting idiom staying duplicated between
`internal/bybit` and `internal/binance`.

**`momentum.Trade.Seq` stays 0; `IsBlockTrade`/`IsRPI` stay false,
always.** Binance's `aggTrade` id is documented monotonically increasing
per symbol but NOT gap-free (100ms aggregation can skip ids) -- using it
for `Seq`'s own "bounded regression counter" role would need a
live-verified understanding of what a regression means in that id space,
exactly the "aggTrade id contiguity" question left open for this PR's own
live probe (see docs/research/binance-momentum-source-v1.md's "What PR 6
inherits"), not guessed at here. `IsBlockTrade`/`IsRPI` have no Binance
equivalent at all in the combined-stream `aggTrade` payload -- every
Binance bar will always show `BlockTradeCount`/`RPITradeCount` = 0, read as
"not applicable to this venue", never "no such trades occurred."

**`momentumcapture.Health` gained a generic `ExclusionCounts` field
(`map[string]int`), additive alongside its existing Bybit-named catalog
fields.**
Binance's own catalog taxonomy (`non_perpetual_contract`,
`underlying_index`, `unknown_underlying_type`) does not map onto Bybit's
finer-grained named fields (`StandardCryptoIncluded`,
`StockPerpetualsExcluded`, ...) without force-fitting a wrong label onto a
real number. `CatalogItemsTotal`/`CryptoPerpetualsIncluded`/
`NonUSDTExcluded`/`NonTradingExcluded`/`InvalidInstrumentExcluded` ARE
genuinely shared vocabulary (both venues' `SymbolCatalogCounts` carry the
same concept under the same name) and populate the existing named fields
directly; everything else goes into `ExclusionCounts`, keyed exactly the
way `binance.translateUniverse` already keys them.
`RedisStore.StoreHealth` serializes it as one JSON hash field
(`exclusion_counts_json`), since an arbitrary venue-defined key set does
not fit the fixed-field `HSet` schema every other counter uses.
`cmd/momentumcapture` (Bybit) leaves this field nil/`{}` -- zero behavior
change for the live process.

**`TickerGapTotal`/`TickerHandler*` double as this process's own OI-gap
counter and OI-handler latency.** Rather than adding a parallel set of
`OpenInterestGapTotal`/`OpenInterestHandler*` fields to the shared `Health`
struct for a concept `Health` already has a slot for (a per-symbol
feed-silence counter, a per-observation handler latency), this process
writes its OI-side numbers into the same fields Bybit's ticker side uses.
`deriveHealthStatus`'s `NATSDisconnectTotal || ... || TickerGapTotal > 0`
branch (copied verbatim, own thresholds independently editable per venue
per the `latency.go` precedent) fires correctly on this process's own OI
gaps as a result, with no Health schema change beyond ExclusionCounts.

**Row identity: same table, same schema, no new migration -- but NOT
`binance.MarketType` verbatim.** The hypertable's primary key already
includes `exchange`, and its compression segment-by already includes
`exchange` too (see the 0024 migration's own design notes) -- this table
was already built to hold more than one venue's rows.
`momentumcapture.Writer` already takes `exchange` as a constructor
argument, so this process calls `NewWriter` with it directly. Its
`marketType` argument is a new local constant, `"linear"` -- NOT
`binance.MarketType`, which is `"linear_usdt_perpetual"`. A code-review
finding caught that the table's own `market_type` column is `VARCHAR(16)`;
`binance.MarketType` is 21 bytes and would have failed every single
insert. `binance.MarketType` is the momentumsource/venue-capability-matrix
domain's own identity label, a different concept from this column, which
describes the PRODUCT TYPE (linear vs inverse vs spot) -- genuinely the
same for both venues here, so reusing `cmd/momentumcapture`'s own
`"linear"` literal exactly also makes the two venues' rows comparable on
that column, not just accidentally non-colliding.

## What this still cannot capture

- No `TickerSource`, so no OHLC/bid/ask, as covered above. A future PR
  designing a correct Binance `TickerSource` (see docs/research/binance-
  momentum-source-v1.md's own "What this PR does not do") would let a
  later capture-binary revision start populating these.
- No `openInterestHist` coarse-but-native value field -- `Amount`-only,
  same as the adapter itself (PR 4).
- No bounded live probe against real Binance servers has run yet (message
  rates, timestamp lag, `aggTrade` id contiguity, reconnect behavior under
  real network conditions) -- this PR's shard size and thresholds are
  carried over from the adapter's own conservative, unverified choices,
  not newly validated here. That probe is the natural first step once this
  process is actually activated under item 7's own gate, not before.

## Operational note

The Compose profile is off by default in both dev and prod. The prod
start target (`make prod-momentum-capture-binance-start`) refuses to run
below `PROD_MOMENTUM_CAPTURE_BINANCE_MIN_AVAILABLE_MB` (1024 MiB) available
RAM or `PROD_MOMENTUM_CAPTURE_BINANCE_MIN_DISK_MB` (10240 MiB) available
disk, mirroring `prod-momentum-capture-start`'s own gate. This is a floor,
not a substitute for ROADMAP item 7's own resize decision ("resize the
always-on host to at least 8 GB... before enabling Binance") -- that
decision, and the "only after the corrected Bybit checkpoint passes" gate,
remain separate, human calls this Makefile target does not make for you.
