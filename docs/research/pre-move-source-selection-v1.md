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

## Amendment A1 (after review of run 1, before run 2)

The review of the first run found three defects in the probe tool, all in its
enforcement rather than in the protocol's bounds:

1. **The window guard trusted a declared date.** It accepted a caller-supplied
   `data_end` instead of the request itself. No request in run 1 crossed the boundary
   (every logged URL and query was within July), but the guard could not prove it.
   From A1, the window is derived from the actual request before it is sent: the
   month or hour in a Gate file name (which must match its directory), the date in a
   Binance or Bybit file name, `from`/`to` for Gate trades, `from + interval x limit`
   for `contract_stats` (5 m only, no `to`), and `startTime`/`endTime` for Bybit open
   interest. A request without a derivable upper bound, or one ending after
   2026-08-01, is refused unsent, **HEAD requests included**.
2. **Inputs were not retained.** Run 1 kept the downloaded archives but not the
   catalogue responses, the historical REST responses or the request parameters, so
   its universe, sample, coverage and REST-to-archive comparison cannot be replayed.
   From A1, every response body is stored by SHA-256 under the gitignored runtime
   directory; the request log records method, URL, parameters, derived window end,
   status and hash; the artifact holds the full universe and its per-source presence;
   and `--replay` recomputes the artifact offline from those inputs, refusing any
   missing or altered response.
3. **Budgets were incomplete.** Bytes were counted only for completed responses and
   wall time only before a request. From A1, every received chunk counts, failed
   attempts included, the wall limit is checked during transfers and before any retry
   sleep, and a request timeout never exceeds the remaining time.

**A disclosure the review did not raise.** Run 1 listed Binance's archive per symbol
with an undated prefix. An S3 listing returns each key's size and modification time,
so run 1 received the sizes of daily files dated after the boundary (up to
2026-10-01), although the tool kept only key names and used none of those sizes.
Under the protocol, receipt itself is not blind. From A1, listings are requested per
year (2019 to 2025) and per month (January to July 2026), so no key or size after
July 2026 is returned, and an undated prefix is refused.

**Run 2** repeats the registered probes once with the corrected tool, within the
same bounds, sample rule and limits. Its universe and sample are recomputed from the
catalogue at run time and compared with run 1. Run 1's artifact is kept unchanged as
superseded evidence; its unretained inputs are not reconstructed.

## Report

**Run 2 is the evidence.** It ran on 2026-10-02 at 14:53 UTC from clean revision
`9ab5ac4` (protocol `47c5d17`, amendment A1 above). Artifact:
[`evidence/pre-move-source-selection-v1/run2-probe-result.json`](evidence/pre-move-source-selection-v1/run2-probe-result.json),
SHA-256 `6c9b15601fe7625944d742aaaa0cf164141b3c01f5b7e30484203cbf016fc62a`. It used 207
requests (Binance 169, most of them dated archive listings; Gate 25; Bybit 11; BloFin
1; MEXC 1), 15.3 MB and 89 s, with no rate limit, retry exhaustion or stopped source.
Every archive and historical request carries a window derived from the request
itself; the latest ends exactly at 2026-08-01T00:00Z. The only non-200 response is
the expected 404 of the first Gate order-book name pattern. No current trade, open
interest, price, funding or book endpoint was called, and the artifact holds no price.

**Reproducibility.** All 207 response bodies are stored by SHA-256 under the
gitignored `runtime/research/pre-move-source-probe-v1/run2/responses/` of the
workstation that ran the probes (not yet copied to the backed-up production research
directory). `python -m schurfer_analytics.pre_move_source_probe --replay <artifact>
--raw-dir <that directory>` recomputed the whole artifact offline and found it
identical. The full 592-base universe and each source's presence in it are in the
artifact.

**Run 1 versus run 2.** Run 1 (`a0465cb`, kept as
[`run1-probe-result.json`](evidence/pre-move-source-selection-v1/run1-probe-result.json),
SHA-256 `7a21a1d83944c957d1576ad8f7aefe011b2cd6998f2dde227bc3bf41fec167e8`, 81
requests) cannot be replayed: its catalogue and REST inputs were not retained. Both
runs produced the same catalogue counts, universe, sample, coverage and every Gate,
Binance and Bybit probe result. The one difference is B1: run 2's dated listings end
on 2026-07-31, where run 1's undated listings reached 2026-10-01. The conclusions
below rest on run 2.

### Universe and sample

The price-free Bybit catalogue listed 1,906 instruments; 592 USDT perpetual bases form
U. S is VELODROME, C98, FIGHT, NAORIS, MNT, PLUME, AKT, MU, BMT, BE, PNUT, SAGA, plus
BTC and ETH. Catalogue presence of U: MEXC 82%, Binance 80%, BloFin 69%. Gate has no
price-free catalogue; all 14 bases of S have a July trade archive file. Ticker presence
is only a candidate route.

### Capability matrix

`pass` is verified by a probe or a committed artifact; `doc` is documentation only and
must be verified by a registered canary; `fail` and `unknown` are as stated.

| Criterion                    | Gate                                                                                                                 | Binance                                                                                                                                                     | BloFin                                                  | MEXC                                                  | Bybit (control)                                            |
| ---------------------------- | -------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------- | ----------------------------------------------------- | ---------------------------------------------------------- |
| P1 trades, native time, side | pass: archive `timestamp` in seconds with microseconds, signed `size`; REST and WebSocket doc                        | pass: `aggTrades` with `transact_time` (ms) and `is_buyer_maker`; stream doc                                                                                | doc: WebSocket `side`, `ts`                             | doc: `push.deal` `T` (side meaning unverified)        | pass: daily dump `side`, `timestamp`                       |
| P2 bounded universe          | doc: contracts carry `status`, `create_time`, `delisting_time` (endpoint carries prices, not called)                 | pass: `exchangeInfo` with `status`, `onboardDate`                                                                                                           | pass: `instruments` with `listTime`, `offTime`, `state` | pass: `contract/detail` with `state`, no listing time | pass: `instruments-info` with `launchTime`, `deliveryTime` |
| P3 gaps                      | pass: `dealid` contiguous per contract, 0 gaps over 23,774 to 66,448 July rows                                       | pass: `agg_trade_id` contiguous; first/last trade ids                                                                                                       | doc: `tradeId` present, continuity unknown              | fail (doc): deal push has no trade id                 | pass: `trdMatchID`                                         |
| P4 identity beyond ticker    | pass: approved registry v4 by contract address, 44 Gate-to-Bybit routes, extension tooling exists                    | unknown: the implemented Binance/Bybit rule promotes established assets by ticker (an accepted simplification); no public contract-address source evaluated | fail: no identity source                                | fail: no approved registry                            | trivial (execution venue)                                  |
| H1 history with integrity    | pass: July archive; REST and archive agree exactly for the probe hour (84, 209, 13 trades)                           | pass: checksums verified; ids contiguous (1 underlying trade-id gap in C98)                                                                                 | fail: last 100 trades only                              | fail: no history                                      | pass                                                       |
| H2 delisted in history       | unknown: not probed                                                                                                  | unknown: not probed                                                                                                                                         | fail                                                    | fail                                                  | not probed                                                 |
| H3 usable at its timestamp   | pass for trades; unknown for `contract_stats` (whether `time` opens or closes the 5-minute bucket)                   | pass for trades; unknown for `metrics` `create_time`                                                                                                        | n/a                                                     | n/a                                                   | pass for trades; OI timestamp semantics unverified         |
| Open interest history        | pass: `contract_stats` 5 m for July with `open_interest` (contracts), `open_interest_usd`, taker sizes, liquidations | pass: `metrics` 5 m with `sum_open_interest` and `_value`                                                                                                   | fail                                                    | fail                                                  | pass: 5 min REST                                           |
| Depth history                | pass: hourly order-book logs (`BASE_USDT-YYYYMMDDHH.csv.gz`, documented full snapshot plus 100 ms updates)           | pass: `bookDepth` by percent band about every 30 s                                                                                                          | fail                                                    | fail                                                  | not probed                                                 |

### Selection

1. **Selectable (P1-P4):** only **Gate**. Binance fails the rule on P4 (`unknown`), BloFin
   and MEXC fail P4, and MEXC also fails P3. Bybit is the control row.
2. Gate also passes H1, so the line can be tested on history before new collection;
   H2 and the open-interest part of H3 remain open.

**Selected source: Gate USDT perpetuals, executed on Bybit.** This is a capability
result only. It does not show that any precursor predicts anything, and it is not
ranked by how often Gate is the first source of a pump.

Two facts limit the choice and must shape what follows:

- **Identity coverage is narrow today.** The approved Gate-to-Bybit registry covers 44
  assets chosen from lead captures; only PLUME of S is in it. A universe that includes
  quiet assets needs the existing identity tooling run over the whole Gate-and-Bybit
  overlap, before any collection or test, with its approvals logged.
- **Gate is thinner than Binance on the same assets.** C98 had 33,262 Gate trades in all
  of July against 8,121 Binance aggregate trades on 15 July alone. A trade-flow feature
  on Gate has far fewer events per window. This is context for the registration, not
  part of the selection rule.

Binance is the runner-up. It becomes selectable if a public identity evidence source
beyond ticker matching (for example contract addresses) is established for its
Bybit routes; it would then compete on the same criteria with wider verified coverage.

### Minimal data set

1. **Trades:** Gate's monthly archive (`futures_usdt/trades/YYYYMM/BASE_USDT-YYYYMM.csv.gz`:
   `timestamp, dealid, price, size`), which already holds history and future months once
   published; REST or WebSocket only if the archive's publication lag is too long.
2. **Open interest and flow:** `contract_stats` at 5 minutes (`open_interest`,
   `open_interest_usd`, `long_taker_size`, `short_taker_size`, liquidations). It has no
   archive, so its retained depth and bucket semantics are open.
3. **Instrument and identity log:** contract `status`, `create_time` and delisting times
   with every inclusion, exclusion and identity decision, versioned with the registry.
4. **Depth:** not in the minimal set. One hourly log of a quiet small cap is about 40 KB
   compressed (about 29 MB a month); active contracts are larger, so full-universe depth
   does not fit current disk headroom. A narrow, registered subset is possible later.

### Storage and request estimate

July trade files of the 12 non-anchor sample bases range 0.26-32.3 MB compressed
(median 0.74, mean 3.64); BTC and ETH add 0.49 GB. For the 592-base universe this
is about **0.4-2.2 GB a month** compressed for trades, plus about 0.5 GB for the
anchors. `contract_stats` at 5 minutes is about 8,640 rows per contract per month.
Polling it for the universe is about 600 requests every 5 minutes; its rate limits and
history depth are unmeasured.

### What is still missing

- Gate archive publication lag and completeness for a high-volume month (REST and
  archive were compared only on three quiet hours below the 1,000-row page);
- delisted contracts in the Gate archive (H2);
- `contract_stats` bucket semantics, retained history depth and rate limits (H3);
- order-book log semantics beyond the first 50 lines (`set` snapshot only so far);
- identity approvals for quiet Gate-and-Bybit assets;
- the live meaning of the stream fields, to be checked by a registered canary only after
  the blind window ends.

### Requirements for the next steps

For PR 5 (storage, load and recovery) and any later collection: keep the native payload
and archive SHA-256; record event time and receive time separately; keep `size` sign as
the documented taker side with its unit (contracts) and the contract multiplier at the
time; log `dealid` gaps per contract; register a bounded universe with an inclusion,
exclusion and identity log; set hard byte, request and duration budgets. A collector or
historical study still needs its own registration, and nothing dated on or after
2026-09-29 is read before v2 reaches a terminal state.
