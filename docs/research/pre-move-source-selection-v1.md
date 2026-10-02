# Pre-move data source selection v1: protocol

Status: **protocol, registered before any probe ran.** This is a data-feasibility study,
not a hypothesis registration, an economic result, or permission to start a collector or
trade. It extends the [MEXC pre-move data audit](mexc-pre-move-data-feasibility-v1.md).
The report section is added after the bounded probes below, under these rules only.

## Question

Can Schurfer collect data on which a pre-move precursor can be tested at all? A usable
source must provide inputs known at the decision time (trades, open interest or external
events, with native timestamps and units), cover quiet periods and a complete,
pre-selected instrument set (including instruments that never pump), map each
instrument to a Bybit contract by a rule rather than a ticker, and leave a potential
execution route. The study selects **one source and a minimal data set, or records a
reasoned no-go**.

Two questions are kept apart:

- **H, historical research:** can the question be tested now, on data before the blind
  boundaries? This needs a historical point-in-time instrument catalogue (listings and
  delistings) and history of the inputs.
- **P, prospective collection:** can a bounded, registered universe be collected
  completely from now on, quiet periods included? This needs a stream or poll with
  native timestamps and gap detection, and an inclusion, exclusion and identity log. A
  source without history can still pass P. Collection itself remains a later,
  separately authorized step (after the 2026-10-31 direction decision and PR 5's
  storage and recovery budget).

## Blind and holdout boundaries

- **No current market data.** No endpoint that returns current or recent trades, open
  interest, prices, funding, tickers or order books is called, and discarding values
  after receipt does not count as blind. The HYP-012 v2 blind window covers every venue
  from 2026-09-29 until v2 reaches a terminal state; the 2026-10-31 date does not lift
  it.
- **No data from the unread HYP-012b holdout.** ISO weeks 36-39 (2026-08-31 to
  2026-09-28) stay unread. Every value-bearing request below asks only for data
  timestamped before **2026-08-01T00:00:00Z**, which also keeps a whole-month archive
  file clear of 31 August.
- **Allowed metadata requests,** whose documented responses carry no price, volume, open
  interest, funding or trade field:
  - Bybit `GET /v5/market/instruments-info` (category `linear`, every status);
  - Binance `GET /fapi/v1/exchangeInfo`;
  - BloFin `GET /api/v1/market/instruments`;
  - MEXC `GET /api/v1/contract/detail`;
  - archive listings and `HEAD` requests (names, sizes, modification times, checksum
    files) on `data.binance.vision`, `download.gatedata.org` and `public.bybit.com`.
- **Forbidden here:** Gate `/futures/usdt/contracts` and `/contracts/{contract}` (they
  return `last_price`, `mark_price`, `funding_rate` and `position_size`), every ticker,
  current trades, deals, depth, open-interest or funding endpoint, and every WebSocket.
  Live semantics such as a stream's trade-side flag are taken from documentation and
  marked unverified until a registered canary.
- No return, price move or outcome is computed, and no instrument is selected by pump
  history. Scanner first-detection counts are not used to rank sources: first
  observation is not the start of the move, and that gap is unmeasured.

## Candidates

Gate and Binance are probed first: Gate has registered Gate-to-Bybit identity routes
(registry v4) and a documented historical archive; Binance has a documented public
trade archive. MEXC (the starting point) and BloFin are evaluated with documentation
and their price-free catalogues. **Bybit is a control row**, scored on the same criteria
for comparison but not selectable: it is the execution venue, and the closed minute
rules (HYP-024, abnormal-flow v1) bound only their own mechanisms.

## Fixed instrument selection

- **Universe U:** Bybit USDT linear perpetuals from the price-free catalogue with
  `launchTime < 2026-07-01` and no `deliveryTime` before 2026-08-01. Every source is
  compared on the same Bybit-executable universe.
- **Sample S:** the bases of U ordered by `sha256("pre-move-source-selection-v1:" +
baseCoin)`, the first 12, plus BTC and ETH as liquid anchors for pagination and size.
  S is fixed before any value-bearing request and is the same for every source.
- **Presence:** Binance, BloFin and MEXC by catalogue membership of `BASE` with USDT
  settlement; Gate by a `HEAD` of its July trade archive file
  (`futures_usdt/trades/202607/BASE_USDT-202607.csv.gz`). A ticker match is only a
  candidate route; identity is evaluated separately.
- **Probe day and hour:** 2026-07-15, and the hour 12:00-13:00 UTC for REST
  completeness checks.

## Bounded probes

| Id  | Source  | Request (all data before 2026-08-01)                                                                                                                                                                                          | Purpose                                                                                                             |
| --- | ------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------- |
| C1  | all     | the allowed catalogue requests above                                                                                                                                                                                          | U, S, presence, listing fields                                                                                      |
| G1  | Gate    | `HEAD` July trade archive for each base in S                                                                                                                                                                                  | existence and size                                                                                                  |
| G2  | Gate    | download July trade files for the first 3 present non-anchor bases (hash order) under the size cap                                                                                                                            | columns, units, side sign, id continuity, hour row count                                                            |
| G3  | Gate    | REST `/futures/usdt/trades` for the G2 bases over the probe hour, paged                                                                                                                                                       | REST history depth and truncation against the archive                                                               |
| G4  | Gate    | REST `/futures/usdt/contract_stats` with `interval=5m` (undocumented values; a rejection is recorded) for 2 G2 bases from the probe hour, one page                                                                            | historical OI, units and depth                                                                                      |
| G5  | Gate    | `HEAD` of the order-book log for one G2 base under exactly two name patterns (the documented monthly `orderbooks/202607/BASE_USDT-202607.csv.gz`, then hourly `BASE_USDT-2026071512.csv.gz`), then one download under the cap | historical depth availability and format (columns documented as `timestamp, action, price, size, begin-id, merged`) |
| B1  | Binance | archive listing of daily `aggTrades` for each base in S                                                                                                                                                                       | first and last available day, delisted coverage                                                                     |
| B2  | Binance | daily `aggTrades` plus `.CHECKSUM` for 3 present non-anchor bases on the probe day                                                                                                                                            | checksum, columns, side flag, id continuity, units                                                                  |
| B3  | Binance | daily `metrics` plus `.CHECKSUM` for the same 3 bases                                                                                                                                                                         | open interest interval, units, timestamp semantics                                                                  |
| B4  | Binance | daily `bookDepth` plus `.CHECKSUM` for 1 of them                                                                                                                                                                              | historical depth format                                                                                             |
| Y1  | Bybit   | `HEAD` daily trade dumps for 3 present non-anchor bases, then one download under the cap                                                                                                                                      | control: columns and side                                                                                           |
| Y2  | Bybit   | REST `/v5/market/open-interest` (5min) for 2 of them over the probe day, one page                                                                                                                                             | control: historical OI                                                                                              |

MEXC and BloFin get no value-bearing request: neither documents historical trades or
open interest, and their current endpoints are forbidden here.

**Hard limits,** enforced by the probe tool and recorded in its artifact: at most 600
requests in total and 200 per source; 50 pages per REST window; 2 concurrent requests
per host; 30 s timeout per request; 2 retries with 1 s and 4 s backoff; a 429 or 418
stops that source and is recorded; at most 50 MB per downloaded file (a larger file is
recorded from its `HEAD` size and not downloaded) and 300 MB in total; 30 minutes of
wall time. The probes run once from a workstation, not from production. Raw files go
to `runtime/research/pre-move-source-probe-v1/` (gitignored) with SHA-256; the committed
artifact holds parsed structure, counts, sizes and hashes only.

## Criteria and selection rule

Each criterion is scored `pass`, `pass_documented` (documentation only, to be verified
by a canary), `fail` or `unknown`.

**Prospective gates (all required to be selectable):**

- **P1 trades:** individual trades with a native event timestamp and a documented
  taker-side field, from a stream or a poll that can be complete.
- **P2 bounded universe:** a price-free catalogue with state, or listing and delisting
  times, so a registered universe can carry an inclusion, exclusion and identity log.
- **P3 gaps:** trade ids, sequence numbers or equivalent so missing data is detectable
  rather than silently thin.
- **P4 identity:** a rule beyond ticker matching exists or can be built to map an
  instrument to its Bybit contract (an approved registry, or documented asset and
  contract metadata), and Bybit lists a perpetual for the base.

**Historical gates (preferred, not required):**

- **H1:** historical trades with side and native timestamps before the boundary, with
  verified integrity (checksum, id continuity, or agreement between archive and REST).
- **H2:** a historical universe that includes instruments delisted before the
  boundary.
- **H3:** each historical input is usable at its own timestamp (no later revision or
  publication-time leakage) or its publication lag is known.

**Selection:**

1. Only candidates passing P1-P4 (with `pass_documented` allowed) are selectable.
2. Prefer a candidate that also passes H1-H3: it can be tested before waiting for new
   data.
3. Then more Bybit-routable bases in S with an identity rule.
4. Then more decision-time inputs among open interest with known units and order-book
   depth.
5. Then the lower estimated storage and request cost for the full universe.

If no candidate passes P1-P4, the result is a no-go for this precursor line, and the
report lists the missing measurement for each source. A selected source yields a
minimal data set and requirements for collection; it is not evidence that any
precursor predicts anything.

## Report contents

A capability matrix (one row per source, Bybit as control) with the evidence class of
every cell; the probe artifact's request, byte and error counts; universe coverage and
identity per source; storage and request estimates for the full universe; the selected
source and minimal data set or the no-go; and requirements for a future collection
(native payload, event and receive time, units, side semantics, gap log, inclusion log,
identity log, bounded universe and hard budgets).
