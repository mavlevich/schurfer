# Binance momentum capability preflight v1

Status: research only. No adapter code, no capture. Extends
[momentum-venue-capability-matrix-v1.md](momentum-venue-capability-matrix-v1.md)'s
Binance entries with live-verified REST findings; does not change that
document's `implemented`/`officially_documented`/`probe_required` states on
its own -- those move only when an adapter and a bounded canary exist.

As-of: 2026-08-14 UTC. All REST findings below were fetched live against
`https://fapi.binance.com` during this investigation, not taken from memory
or docs alone; exact response payloads are quoted where it matters.

## What this preflight is and is not

Per the capability matrix's own "Gate to the next PR": Binance capture
(`feat/binance-momentum-capture-v1`, PR 6 in the current roadmap) waits on a
corrected Bybit checkpoint. Preparing -- reading docs, verifying live REST
behavior, sizing resources -- does not. This PR is exactly that preparation:
it resolves as many `probe_required`/thin `officially_documented` entries as
possible through cheap, safe REST calls (no auth, no order placement, no
sustained connection), and explicitly identifies what still needs a bounded
WebSocket probe once an adapter exists. It does not attempt that WebSocket
probe here.

## 1. Futures universe

Live `GET /fapi/v1/exchangeInfo` (weight 1, response 1.07 MB, 865 raw symbol
entries):

| Filter                                     | Count |
| ------------------------------------------ | ----- |
| `contractType=PERPETUAL`                   | 698   |
| + `status=TRADING`                         | 570   |
| + `quoteAsset=USDT` and `marginAsset=USDT` | 527   |
| of which `underlyingType=COIN` (crypto)    | 525   |
| of which `underlyingType=INDEX`            | 2     |

The 2 index entries are `BTCDOMUSDT` (BTC dominance index) and `ALLUSDT` (an
aggregate market index) -- neither is a single-asset crypto instrument and
both must be excluded the same way Bybit excludes non-standard instruments.

**Finding: Binance's universe filter is structurally cleaner than Bybit's.**
Binance tags tokenized-stock and commodity perpetuals with their own
`contractType=TRADIFI_PERPETUAL` (163 symbols observed: `XAUUSDT`,
`XAGUSDT`, `TSLAUSDT`, `XPTUSDT`, `XPDUSDT`, `INTCUSDT`, `HOODUSDT`,
`MSTRUSDT`, `AMZNUSDT`, `CRCLUSDT`, ...). A single `contractType !=
PERPETUAL` check excludes them entirely -- Bybit's own catalog mixes these
into the same symbol type and needs the separate `stock_perpetuals_excluded`
/ `commodity_perpetuals_excluded` counters already visible in
`market:momentumcapture:health:bybit` to filter them out after the fact. The
source-contract refactor (PR 3) should still keep an explicit,
independently-counted exclusion path for `INDEX`-type USDT perpetuals and
for `TRADIFI_PERPETUAL`, fail-closed, matching the project's existing
"unknown classification is excluded, not silently admitted" convention --
just note the exclusion signal itself is a native enum value here, not a
heuristic.

**Finding: universe churn is real and needs point-in-time handling.** 127 of
865 raw symbols were `SETTLING` at fetch time (formerly active perpetuals
being wound down) and 1 was `PENDING_TRADING` with an `onboardDate` already
in the past relative to `serverTime` (a real but minor inconsistency in the
observed data, not something to build logic around). The existing
`capabilities.go` constraint "adapter and point-in-time universe drift
handling not implemented" is confirmed as a real, non-trivial gap: the
Binance universe moves more than a static-catalog-at-startup model can
assume, more than what Bybit's "controlled restart" constraint implies is
tolerable there. A future adapter needs periodic `exchangeInfo` re-polling
(weight 1, cheap) and to treat a symbol leaving `TRADING` mid-session as a
lifecycle event, not silently drop it.

## 2. Open interest -- amount and value

Two separate live-verified endpoints, with materially different freshness:

**`GET /fapi/v1/openInterest?symbol=BTCUSDT`** (weight 1):

```json
{ "symbol": "BTCUSDT", "openInterest": "112104.426", "time": 1786737418938 }
```

`time` was 2.4 seconds behind `serverTime` at fetch -- genuinely near-real-time,
amount only.

**`GET /futures/data/openInterestHist?symbol=BTCUSDT&period=5m&limit=3`**
(documented weight 0, separate IP cap of 1000 req/5min):

```json
[
  {
    "symbol": "BTCUSDT",
    "sumOpenInterest": "112061.139",
    "sumOpenInterestValue": "7054730562.9477",
    "timestamp": 1786736700000
  },
  {
    "symbol": "BTCUSDT",
    "sumOpenInterest": "112069.753",
    "sumOpenInterestValue": "7052179726.1051",
    "timestamp": 1786737000000
  },
  {
    "symbol": "BTCUSDT",
    "sumOpenInterest": "112080.414",
    "sumOpenInterestValue": "7054845619.023",
    "timestamp": 1786737300000
  }
]
```

The latest bucket's own timestamp was ~121 seconds behind `serverTime` at
fetch, on top of the 5-minute bucket width itself (the finest period this
endpoint supports).

**Finding: this refines, not confirms, the existing "no native OI-value
field" constraint.** `capabilities.go` and the capability matrix both state
Binance has no native current-OI-value field -- true for the endpoint they
reviewed (`openInterest`), but `openInterestHist` genuinely does carry
`sumOpenInterestValue`, sourced by Binance itself, not derived by us. The
correct, precise framing is: Binance HAS a native OI-value figure, but it is
only available at 5-minute-or-coarser granularity with a few minutes of
inherent staleness on top -- structurally different from Bybit's
ticker-pushed, effectively point-in-time `openInterestValue`. A momentum-flow
feature built on Binance's OI value would be comparing a ~5-10 minute-old
aggregate against Bybit's live push; that difference needs to be an explicit,
documented constraint on any future cross-venue OI-value comparison
(`analysis/momentum-venue-overlap-and-lead-v1`, PR 14), not silently treated
as equivalent. `openInterest` (amount, near-real-time) remains directly
comparable to Bybit's.

## 3. Trades

Not independently re-verified live in this preflight beyond what the
capability matrix already documents (`aggTrade`, 100ms same-price/same-side
grouping, buyer-maker flag for taker-side derivation). Re-stated here because
it directly bounds what the eventual `TradeSource` implementation can honor:
total taker notional over a window remains meaningful; per-trade or
top-K/large-trade-histogram comparisons against Bybit's individual-trade
stream are not, per the matrix's own existing constraint. No new finding.

## 4. Liquidations

Not independently re-verified live. Restating from documentation:
`!forceOrder@arr` (all-market) and `{symbol}@forceOrder` both push at most
the latest liquidation event per symbol per 1000ms -- a censored signal, not
a complete tape, exactly as the capability matrix already states. Relevant
to PR 26/27 (liquidation source contract / capture), not to the near-term
momentum work.

## 5. Rate limits (live-verified)

`exchangeInfo` response's own `rateLimits` field, confirmed against the
`x-mbx-used-weight-1m` response header during this investigation (reached 2
after ~6 lightweight calls):

| Type           | Interval | Limit |
| -------------- | -------- | ----- |
| REQUEST_WEIGHT | 1 minute | 2400  |
| ORDERS         | 1 minute | 1200  |
| ORDERS         | 10 sec   | 300   |

`ORDERS` limits are irrelevant to a market-data-only capture adapter (no
order placement). `REQUEST_WEIGHT` at 2400/min is generous for a periodic
`openInterest` poll loop across ~525 symbols (weight 1 each): even polling
every symbol once a minute costs only 525 weight/min, leaving ample headroom
for `exchangeInfo` re-polling and the separate `openInterestHist` cap.

## 6. Live WebSocket throughput -- not measured here

Message rate, timestamp-vs-receive lag, aggTrade id contiguity, and
reconnect/gap behavior were NOT empirically measured in this session: the
local investigation sandbox completed WS handshakes but did not deliver
subsequent data frames (a sandbox networking limitation, confirmed by REST
calls to the same host working normally), and installing ad-hoc packages on
the live production host for a one-off measurement was deliberately avoided.

This is real remaining work, not a documentation gap that can be closed by
reading further. Do it as a small, reviewed, bounded script (mirroring
`momentum_flow_episode_study_report.py`'s own "read-only, bounded, no
mutation" discipline) run from a normal dev machine or CI with working
outbound WebSocket connectivity -- or as part of `feat/binance-momentum-
source-v1`'s (PR 4) own smoke test, per that PR's stated scope ("Проверить
direction и timestamps live smoke-тестом"). Specifically still needed:
aggTrade message rate for a busy vs. a thin symbol, `E`/`T` field lag against
local receive time, `a` (agg trade id) contiguity within one connection,
`markPrice@1s` vs `@3s` actual delivered cadence, and reconnect behavior
after a forced disconnect.

## 7. Resource estimate

Grounded in the running Bybit adapter's own live numbers (`docker stats`,
this session), not a fresh guess:

| Metric                                      | Bybit (516 symbols, live)                               |
| ------------------------------------------- | ------------------------------------------------------- |
| RSS                                         | 62 MiB (12% of 512 MiB limit)                           |
| CPU                                         | ~14%                                                    |
| Net I/O (received, ~8h)                     | 23.3 GB (~2.9 GB/hour)                                  |
| Storage growth (measured, corrected canary) | 1,128 MiB/day hot, ~222 MiB/day compressed steady-state |

Binance's comparable USDT-crypto-perpetual universe (525 symbols) is almost
identical in size to Bybit's (516), so a first-order estimate for a Binance
adapter is the same order of magnitude on all four axes -- tens of MiB RAM,
a similar CPU fraction, single-digit GB/hour of WS traffic, roughly
comparable storage growth for the shared 1-minute bar schema (row count is
driven by symbol count x time granularity, not by underlying message
granularity). Two structural differences to size separately once real
numbers exist:

- **Lower per-symbol WS message count, not necessarily lower bytes**:
  100ms-grouped `aggTrade` produces fewer discrete trade messages than
  Bybit's individual-trade stream for a busy symbol, but each message can
  represent multiple fills.
- **An added REST poll loop for OI** (Bybit gets OI pushed inside its ticker
  stream; Binance needs a separate periodic `openInterest` call per symbol),
  a new, bounded, well-understood cost given the rate-limit headroom in
  section 5.

Host capacity: confirmed live via SSH this session, the production host now
reports 7.6 GiB total RAM / 4 CPUs (`free -h`, `nproc`), 3.6 GiB currently
available -- up from the original 4 GB host ROADMAP.md's capacity gate was
written against. Note this is 7.6 GiB, not a full 8 GiB: typical for a
nominally-marketed "8 GB" cloud plan once `free -h`'s binary GiB and any
hypervisor-reserved memory are accounted for, but the actual Hetzner plan
spec was not independently confirmed here, so whether this literally clears
the "at least 8 GB" wording is a judgment call for whoever owns that gate,
not asserted by this preflight. Either way, clearing it would not by itself
authorize PR 6 (Binance capture) to start -- that still waits on the
corrected Bybit checkpoint's own quality-gate result, per the capability
matrix's existing sequencing rule, which is unrelated and still open.

## Recommended `capabilities.go` update

Narrow, evidence-backed change only (full diff in this PR): update
Binance's `OIValue` capability entry to cite `openInterestHist` as
evidence and state the 5-minute-granularity/staleness constraint precisely,
instead of the current "no native current OI-value field" phrasing. Status
stays `probe_required` -- a documented, live-REST-verified field is still
not an adapter, still not live-probed via WebSocket-equivalent freshness
expectations, and still needs its own fixtures and bounded probe before
`implemented`.

## Open questions carried into PR 3/4

- Confirm live WS message rates, timestamp lag, and reconnect semantics
  (section 6) as part of the source-contract refactor's own Binance-side
  design, before `feat/binance-momentum-source-v1` locks an implementation.
- Decide whether `openInterestHist`'s coarse-but-native OI value is worth
  capturing at all for v1 (as a clearly-labeled, separately-timestamped,
  lower-freshness field) or deliberately left `unsupported` until a
  finer-grained source appears -- a real design choice, not a preflight
  answer.
- `INDEX`-type USDT perpetuals (`BTCDOMUSDT`, `ALLUSDT`) and
  `TRADIFI_PERPETUAL` need their own named, counted exclusion classes in
  the future `UniverseSource` contract, mirroring Bybit's existing
  `stock_perpetuals_excluded`/`commodity_perpetuals_excluded` pattern.

## Evidence

- [USD-M Futures WebSocket market streams](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/ws-streams/market)
- [USD-M Futures REST market data](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data)
- Live `GET https://fapi.binance.com/fapi/v1/exchangeInfo`, `GET .../fapi/v1/openInterest`, `GET .../futures/data/openInterestHist` responses, fetched during this investigation (2026-08-14 UTC), not archived verbatim -- re-fetch to reproduce, all are public, unauthenticated, low-weight endpoints.
