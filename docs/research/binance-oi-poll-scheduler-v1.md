# Binance OI poll scheduler v1

## What this PR fixes

Root cause 2 of the incident documented in
[binance-watch-input-readiness-v1.md](binance-watch-input-readiness-v1.md):
`missing_fresh_oi` rejected roughly 94% of `momentum_flow_watch_binance`
evaluations, including many that would otherwise have passed once PR2
([momentum-trade-price-source-v1.md](momentum-trade-price-source-v1.md))
fixed `missing_price`. `momentum_flow_watch_evaluator._fresh_oi` requires
an OI reading whose own event time lands inside the exact 1-minute bucket
being evaluated -- a real-time freshness bar the previous poll design
could not reliably meet.

## The previous design's structural bug

`binance.PollOpenInterest` ran a single goroutine on a single
`time.Ticker`, firing one blocking HTTP request per tick, spaced by
`interval / len(symbols)` (~114ms at ~525 symbols and a 60s target
interval). `time.Ticker` **drops missed ticks rather than queueing them**:
any single request slower than ~114ms stalled the entire round-robin, not
just that one symbol's own turn -- every other symbol waited behind it.

Measured directly against real prod data (2026-08-17), not assumed:
per-symbol OI refresh gap over a 30-minute window was p50 127s, p95 255s,
p99 505s, max 1010s -- 2 to 17x slower than the 60s target this design was
configured for. Binance's own rate budget (`GET /fapi/v1/openInterest` is
weight 1, 2400 weight/min total) had ample headroom for 525
requests/minute; the bottleneck was the blocking-call-inside-ticker
structure, not the rate limit.

## The fix: bounded concurrent workers, paced by a real token bucket

`PollOpenInterest`'s signature changed from `(ctx, symbols, interval,
consume)` to `(ctx, symbols, OpenInterestSchedulerConfig, consume)`:

```go
type OpenInterestSchedulerConfig struct {
    Workers            int // concurrent in-flight requests
    RateLimitPerMinute int // total request weight/min across all workers
}
```

- **Workers** hide real HTTP round-trip latency: while one request is
  in flight, others proceed instead of waiting behind it. Worker count
  does not itself raise throughput past the rate limit -- it only lets
  latency be hidden rather than serialized.
- **RateLimitPerMinute** is enforced by a small, dependency-free token
  bucket (`internal/binance/ratelimit.go`): capacity `Workers` tokens
  available immediately (so every worker's first request fires without
  waiting on a refill tick -- the same cold-start reasoning the previous
  design documented), refilled one token per tick at `time.Minute /
RateLimitPerMinute`. All workers pull from one shared round-robin index
  (`atomic.Uint64`), so the token bucket -- not worker count, not a
  per-worker sub-interval -- is what actually paces total request rate.
- Default (`DefaultOpenInterestSchedulerConfig`): 8 workers, 1200
  requests/min. 1200 is half of Binance's real 2400/min budget,
  deliberately conservative rather than tuned from a measured per-request
  latency distribution -- this PR does not have that measurement yet (see
  "What this PR does not do" below). Both are overridable via
  `OI_POLL_WORKERS`/`OI_POLL_RATE_LIMIT_PER_MINUTE` (`infra/docker/
docker-compose.prod.yml` passes them through from `.env.prod`), so a
  future retune needs no code change or rebuild.

At 525 symbols and the default 1200/min, one full round of every symbol
takes `525 / 1200 * 60s` ≈ 26s -- roughly 5x faster than the previous
design's own p50, and comfortably inside the 60s bucket freshness window
`_fresh_oi` requires.

### Real 429/418 handling, not a generic HTTP error

`fetchOpenInterest` now distinguishes a 429 (Too Many Requests) or 418
(Binance's "I'm a teapot" code for an IP auto-banned after repeated 429s)
from every other HTTP failure via `*RateLimitError{StatusCode,
RetryAfter}`, parsed from the response's own `Retry-After` header
(falls back to a conservative 60s if the header is missing or
unparseable). A rate-limit response means **this process's own IP** is
over budget or banned -- a condition every worker shares, not a
single-symbol problem. `PollOpenInterest` pauses the entire pool (a
shared `atomic.Int64` deadline every worker checks before its next
request, extended via compare-and-swap so a second worker's own hit never
shortens an already-longer pause) until `RetryAfter` elapses, instead of
letting the other workers keep hammering the same limit -- or, for a 418,
extending an active ban -- while one worker waits it out. A 418 logs at
Error severity; a plain 429 logs at Warn.

`fetchOpenInterest` also parses Binance's own `X-Mbx-Used-Weight-1m`
response header into `OpenInterestReading.UsedWeight1m` (0, not an error,
when the header is absent), and logs a Warn if it ever crosses 1920 (80%
of the real 2400/min budget) -- a signal that something else on this IP
is also spending weight, worth an operator's attention, since this
process's own configured `RateLimitPerMinute` stays well under 2400 by
design.

### The gap-detection threshold is now computed, not hardcoded

`checkOpenInterestGaps`'s own threshold used to be a package-level
constant (`3 * binance.DefaultOpenInterestPollInterval` = 180s), a single
number independent of how many symbols the process actually subscribed
to. It is now `application.openInterestGapThreshold`, computed once at
startup as `openInterestGapThresholdMultiple (3) *
openInterestExpectedCycleDuration(universe.Count(),
cfg.OpenInterestScheduler)` -- the scheduler's own real, budget-driven
full-cycle time for this run's actual universe size, not a guess. Floored
at `openInterestGapThresholdFloor` (30s): a tiny universe divides a
generous per-minute budget into a near-zero expected cycle, and a
threshold that small would false-positive on ordinary single-request
latency jitter rather than catch a genuine interruption. The floor does
not bind at production scale (525 symbols, default config: raw formula
already gives ~79s).

## What this PR does not do

- **Does not measure a real per-request latency distribution** to size
  `Workers` from evidence. The default (8 workers, 1200/min) is a
  deliberately conservative starting point; retuning from real measured
  p50/p95 request latency (now newly possible, since `UsedWeight1m` and
  the existing `openInterestReceiveToHandle`/`openInterestHandler`
  latency histograms are both already wired) belongs to a future
  operational pass, not this PR.
- **Does not add a circuit breaker or alerting on sustained 429/418.** The
  pool-wide pause handles a single rate-limit episode correctly; a feed
  that stays rate-limited or banned for an extended period still just
  keeps retrying (visibly, via the Warn/Error logs) rather than raising
  a distinct health signal. Revisit if this becomes a real operational
  pattern, not preemptively.
- **Does not touch Bybit.** `cmd/momentumcapture`'s own ticker/OI feed is
  a completely separate push-based path (`internal/collector`/NATS), with
  no round-robin poller of any kind -- nothing here applies to it.

## Testing

- `internal/binance/ratelimit_test.go` (new): the token bucket starts
  full, blocks once empty until a refill tick, does not accumulate
  unbounded credit from ticks received while already full, and respects
  context cancellation while waiting.
- `internal/binance/openinterest_test.go` (extended): the 6 pre-existing
  `PollOpenInterest` tests updated for the new `OpenInterestSchedulerConfig`
  signature (all pass with `-race`), plus new tests proving genuine
  concurrency (`TestPollOpenInterestUsesWorkersConcurrently`, using an
  HTTP handler that tracks max simultaneous in-flight requests), that the
  rate limiter actually bounds request volume over a window (not just
  eventually converges), that a 429 pauses the **entire** pool until
  `Retry-After` elapses (`TestPollOpenInterestPausesEntirePoolOn429`,
  proving a >=900ms gap after two workers hit 429 near-simultaneously),
  and `Retry-After`/`X-Mbx-Used-Weight-1m` header parsing (both present
  and absent/malformed).
- `cmd/momentumcapturebinance/main_test.go` (extended): the dynamic gap
  threshold's floor and production-scale formula both pinned by dedicated
  tests (`TestComputeOpenInterestGapThresholdAppliesTheFloorForASmallUniverse`
  / `...ScalesWithRealisticUniverseSize`), so a future change to the
  multiplier or formula cannot silently regress either case.

Full verification: `go build ./...`, `go vet ./...`, `golangci-lint run
./...`, and `go test ./... -race` all clean for this PR's own changes at
time of writing (two unrelated pre-existing lint findings remain in files
this PR does not touch: a `gosec` integer-conversion note in
`cmd/momentumcapture/main.go` and a `nilerr` note in
`internal/binance/trades.go`).

## What's next

- PR4 `analysis/binance-watch-input-coverage-v1`: 24-48h coverage
  measurement now that PR2 (price) and PR3 (this PR, OI cadence) are both
  live -- descriptive only, no threshold tuning, no outcomes. This is
  also where a real per-request latency distribution would come from, to
  retune `Workers`/`RateLimitPerMinute` from evidence instead of the
  conservative default.
- PR5 `feat/binance-momentum-watch-v2` (conditional on PR4's own
  findings). `momentum-watch-binance`/`momentum-paper-binance` stay
  stopped on prod until PR4's coverage read.
