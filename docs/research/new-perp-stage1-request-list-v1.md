# I25 stage 1: contract and request list (v1, DRAFT, revision 6, 2026-11-18)

Status: **draft for the colleague's review, then the owner's go.** This document and its
code fetch nothing. It is the stage 1 gate of the
[I25 laboratory protocol](new-perp-short-lab-protocol-v1.md) (section 11: "owner's go
per fetch, registry row, request list"). Stage 1 is the data-recoverability check M1 to M6
on the discovery year. Revision 2 applies the review of revision 1 (`e393458`): six
defects, and the owner's decisions on the open questions. Section 6 lists each.

- Frozen contract: [`contract.json`](evidence/new-perp-stage1-v1/contract.json).
- Code: `apps/analytics/schurfer_analytics/new_perp_stage1.py` (contract, reducers,
  planner, fetcher) and `new_perp_stage1_verdict.py` (M1 to M6, stop codes, from receipts).
- Registry: the section "I25 stage 1 registration" of the
  [seen-windows registry](seen-windows-registry.md).
- Protocol change: [Amendment 1](new-perp-short-lab-protocol-v1.md) in section 4.

## 0. What the owner is asked to approve: two gos

**One contract file, two gos that cannot run out of order.** Why one file and not two:
the rules (windows, cut, thresholds, retry and parse rules) must be identical in both gos,
and one hash over everything binds a run to one reviewed text. What keeps the gos in order
is not the file but the code: go 2 refuses to start unless go 1 finished **in the same
root**, and unless it is given the **SHA-256 of the manifest** that go 1 wrote and the
owner reviewed. Each go is approved by its own hash, which covers the shared rules plus
that go's classes and caps only, so a later change to go 2 cannot change what was approved
for go 1.

```text
request_list_sha256 (whole contract, bound into every run) =
  8a4758760af4889078f2034eba77c78a8f1885b8269fce6b2cde863259cf9c17
go 1 approval hash (go_sha256["1"]) =
  02b5ac65932302e377563cc9d8ce1a1448cd3c94b022cb797a214a4a77f359fe
go 2 approval hash (go_sha256["2"]) =
  5324b08114db77c5cc999aa0f7f79f994ff101968b2a9d04ebb67ff4291713bb
contract.json file SHA-256 =
  1f59d45a759d8ee13866611abba366807b87f33ba482d1592cde30a3b0475edc
```

Check without a network request:

```bash
uv run python -m schurfer_analytics.new_perp_stage1 plan
uv run python -m schurfer_analytics.new_perp_stage1 write-contract --check
```

| Go  | Reads                                                                                        | Classes            | Planned requests | Max requests | Attempt cap | Byte cap |
| --- | -------------------------------------------------------------------------------------------- | ------------------ | ---------------- | ------------ | ----------- | -------- |
| 1   | **metadata only**: catalog fields, archive names, per-symbol listings, HEAD sizes. No body   | C1, C3, D1, D2h    | 526              | 761          | 1,000       | 300 MiB  |
| 2   | first daily files (column 1), one entry bar, hourly series, funding. Needs go 1 and manifest | D2, D3, D4, D5, D6 | 1,181            | 2,400        | 2,400       | 8.5 GiB  |

**Combined byte ceiling of both gos: 300 MiB + 8,704 MiB (8.5 GiB) = 9,004 MiB, about 8.8 GiB. These are hard ceilings: each includes a 64 KiB reserve for the last block (section 4).** Planned total 1,707 requests (go 1 526, measured; go 2 1,181) (the protocol's section 10 said about 1,000: that counted
listings and first files only). Attempt caps include retries. **Worst case** (4 funding pages for
every one of 258 candidates): go 2 sends 258 + 3 x 258 + 4 x 258 = **2,064 first attempts**, and the
cap of 2,400 leaves 16% for retries. At one request per second go 1 takes about 10 minutes and a
full go 2 about **40 to 50 minutes** (2,064 requests is about 34 minutes, plus retries and the
first-file transfer). **Go 1 spent 526 attempts on 2026-11-17.** The Go 1 rerun in a new root is
**up to 1,000 new attempts** on top of them (526 expected), and **needs a new owner go under the
unchanged go 1 hash**. Worst-case attempts for the whole study: 526 spent + 1,000 + 2,400 = 3,926;
expected about 526 + 526 + 1,430 = 2,480.

**The gate between the gos.** Go 1 ends by writing `manifest.json` and `go1_done.json` in
the run root: the launch count, the exact D2 request list (as a hash), the preflight (the
summed `Content-Length` of the first files against the 7,936 MiB (7.75 GiB) class cap, files over the
512 MiB per-file cap counted apart), and an upper bound on the entry candidates. The go 1
**report** (`new_perp_stage1_verdict`, aggregate counts only, no symbol) shows the owner
what is in it. The owner approves go 2 by giving the manifest's SHA-256.

**The go covers these coverage reads.** Protocol section 11, item 2 names funding and
mark-price reads as needing a separate go. Go 2 contains them, so the owner's go 2 is that
separate go: D3 to D6 read start times, hourly mark-price and trade series and funding
timestamps only, and discard prices and rates unparsed.

**Out of this contract, deliberately** (each stays a separate decision, protocol section 11):
the order-book archive (class F and its HEAD existence requests are removed: item 7), the
spot catalog (class C2 is removed: it does not reliably establish a spot listing date), the
backward holdout and stage 1b, any assignment or count of the HYP-031 test year
(2025-10-01 to 2026-09-28), any recording inside the HYP-012 v2 blind window (from
2026-09-29), the owner's fee tier (item 8), the laboratory read of returns, any paper,
LIVE_PROBE or order path, and the merge of this PR.

## 1. Scope and the cut: how test-year and blind-window names never get in

Stage 1 is the **discovery year only**, 2024-10-01 to 2025-09-30 (**cut = 2025-10-01T00:00Z**).
Catalog responses, the archive root and the listings hold names and dates from the test year
and the blind window. The rules, all enforced **inside the reducers** and each pinned by a
test that compares the output with and without the excluded rows:

1. **Catalog rows.** The launch time is read first. A row launched at or after the cut is
   skipped before any other field of it is read. It is not stored and **not counted**.
2. **No total row counts.** No receipt holds the number of rows of a response, only the number
   kept. The number of test-year launches cannot be read from a receipt or a report (protocol
   section 11, item 5).
3. **Archive root: an unknown date is not allowed.** A directory is kept only if it is absent
   from the whole catalog (post-cut symbols included, in memory) **and** its modification date
   is inside the window: its last file is before the cut, so it cannot be a test-year launch.
   A directory with no date, a date from the cut, or a catalog symbol leaves **no trace** and
   is not counted: the reducer keeps no directory count at all, and adding a post-cut
   directory to the page leaves its output and `phase0.json` unchanged. If a page carries no
   dates at all the output says so (a yes or no) and the M2 archive-only share is
   `not_assessed`. (Revision 1 kept unknown-date names and counted them; that is removed.)
4. **Listings.** File names dated at or after the cut are dropped uncounted.
5. **Bars, marks, funding.** Every span ends at or before `cut - 1 ms`. A row dated at or
   after the cut that a server returns anyway is dropped uncounted.
6. **Eligibility is cut before entry.** A launch whose planned exit `X` is after the cut is
   `window_censored` and gets no bar, mark or funding request.
7. **Pre-window catalog rows** keep only `symbol`, the base, and (new) a **delivery date only
   if it falls inside the window**, so that a symbol closed inside the window before a launch
   is a `relist` and one still live at it is `multiplier_ambiguous`. A date before the window
   or from the cut is never kept. These rows are **closed service material outside Git**; no
   date and no count of them is published.
8. **Prices, rates and the post-cut symbol cannot reach a file.** The tests give the fetcher a
   synthetic Bybit whose every price, rate and tick size is a sentinel and which holds a
   post-cut launch and post-cut directories, then check that **no file of the run root holds a
   sentinel** and that no request touched the post-cut symbol or a date from the cut.

## 2. The request classes

Counts are requests (first attempts). "Per response" is the byte cap of one response, "class
bytes" the cap of all responses of the class.

| Go  | Class | Name              | Method and endpoint                                                 | Expected | Max   | Per response | Class bytes |
| --- | ----- | ----------------- | ------------------------------------------------------------------- | -------- | ----- | ------------ | ----------- |
| 1   | C1    | `catalog_linear`  | GET `api.bybit.com/v5/market/instruments-info`                      | 4        | 9     | 3 MiB        | 27 MiB      |
| 1   | C3    | `archive_root`    | GET `public.bybit.com/trading/`                                     | 1        | 2     | 4 MiB        | 9 MiB       |
| 1   | D1    | `archive_listing` | GET `public.bybit.com/trading/{SYMBOL}/`                            | 263      | 450   | 1 MiB        | 256 MiB     |
| 1   | D2h   | `first_file_head` | HEAD `public.bybit.com/trading/{SYMBOL}/{SYMBOL}{FIRST_DAY}.csv.gz` | 258      | 300   | 0            | 0           |
| 2   | D2    | `first_file`      | GET `public.bybit.com/trading/{SYMBOL}/{SYMBOL}{FIRST_DAY}.csv.gz`  | 258      | 300   | 512 MiB      | 7,936 MiB   |
| 2   | D3    | `entry_trade_bar` | GET `api.bybit.com/v5/market/kline`                                 | 228      | 300   | 64 KiB       | 24 MiB      |
| 2   | D4    | `mark_hourly`     | GET `api.bybit.com/v5/market/mark-price-kline`                      | 228      | 300   | 512 KiB      | 150 MiB     |
| 2   | D5    | `trade_hourly`    | GET `api.bybit.com/v5/market/kline`                                 | 228      | 300   | 512 KiB      | 150 MiB     |
| 2   | D6    | `funding_history` | GET `api.bybit.com/v5/market/funding/history`                       | 239      | 1,200 | 256 KiB      | 300 MiB     |

Parameters:

- **C1:** `category=linear`, `status` in `Trading`, `Delivering`, `Closed`, `limit=1000`,
  `cursor` while a next page exists (at most 3 pages per status).
- **D1:** one request per in-window catalog launch (planning 263, hard cap 300) and one per
  **archive-only candidate** (planning a few dozen, hard cap 150). Above either cap the plan
  stops with `plan_exceeds_cap` and nothing is truncated.
- **D2h and D2:** only for a launch whose first listed day is within one day of its
  `launchTime`. A first file two or more days away decides the M1 class by its name alone
  and is never downloaded.
- **D3:** `interval=1`, `start=end=E`, `limit=1`, `E` = `T0 + 7 d` rounded up to the minute.
- **D4, D5:** `interval=60`, `start = T0 + 3 d`, `end = min(T0 + 21 d, cut - 1 ms)`,
  `limit=1000` (one page holds the 456 hourly bars).
- **D6:** `startTime = T0 + 3 d`, `endTime` first `min(T0 + 21 d, cut - 1 ms)`, then just
  before the oldest row of the previous full page, `limit=200`, at most 4 pages.
- D3 to D6 are asked **only for entry candidates**: a readable, unambiguous `T0` (`aligned` or
  `delayed_open`), not `window_censored`, not `lookback_censored`, and not `relist`,
  `multiplier_ambiguous` or `ticker_reuse_suspected`. Measured 228 by the Go 2 run (the 263 launches less the lookback and window censored and the ambiguous; at most 258 by the Go 1 manifest).

**Derived requests and their hashes.** C1 and C3 are the static list in `contract.json`
(`static_requests`). Every later class is a deterministic function of earlier receipts. Before
each phase runs, the run writes `plans/<phase>.json` with the request count and the SHA-256 of
the sorted request keys. A derived list above its class cap, or a class outside the running
go, stops the run.

## 3. What is parsed, what is discarded

Every body physically holds prices or rates. The reducers keep only this and **discard the
rest unparsed**. Receipts and reports hold timestamps, existence, sizes, counts and hashes.

| Class | Kept                                                                                                                                                                                             | Discarded unparsed                                                                                              |
| ----- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------- |
| C1    | `symbol`, `baseCoin`, `status`, `launchTime`, `deliveryTime` (before the cut), `fundingInterval`, whether `isPreListing` is present, `minNotionalValue`, `qtyStep`, `minOrderQty`, `maxLeverage` | `priceFilter` (every field), `upperFundingRate`, `lowerFundingRate`, the contents of `preListingInfo`, all else |
| C3    | directory names ending `USDT`, kept only for the candidates of section 1 item 3                                                                                                                  | the page, every other directory, any count                                                                      |
| D1    | file names `SYMBOLYYYY-MM-DD.csv.gz` before the cut: first day, days inside the window, gaps longer than 30 days                                                                                 | names from the cut, sizes, all else                                                                             |
| D2h   | status and `Content-Length`                                                                                                                                                                      | nothing else exists (no body)                                                                                   |
| D2    | **column 1 only** (the trade timestamp): minimum, maximum, first, last and the row count                                                                                                         | columns 2 and later of every row (side, size, price, ids, notionals); streamed in memory, never stored          |
| D3    | the bar start time (column 1)                                                                                                                                                                    | open, high, low, close, volume, turnover                                                                        |
| D4    | start times: count, oldest, newest, and the **runs of missing hours** (at most 20, else `runs_truncated`)                                                                                        | open, high, low, close                                                                                          |
| D5    | the same for the trade series                                                                                                                                                                    | open, high, low, close, volume, turnover                                                                        |
| D6    | `fundingRateTimestamp`: per page the row count, oldest, newest and the **largest gap between consecutive timestamps**                                                                            | `fundingRate` of every row                                                                                      |

An **error envelope** (retCode other than 0) additionally keeps its `retCode` and short `retMsg`
(section 4); it holds no market value. The first response of each JSON class also leaves a **key-only schema probe** (key paths and
value types, never values). Receipts, plans, `phase0.json`, `manifest.json` and the catalog
snapshot live in the run root **outside Git**. Only the aggregate report is committed
afterwards: counts, shares and codes, no symbol and no price.

## 4. How a run behaves

1. **Phase 0 (go 1, atomic):** C1 and C3. The root listing is reduced against the whole catalog
   in memory, so the facts of the phase live in one file, `phase0.json`, written once. An
   interrupted phase repeats once from its first request as a new round (C1 and C3 caps allow
   it).
2. **Phase 1 (go 1):** D1 for every in-window launch and every candidate.
3. **Phase 2 (go 1):** D2h, then the **preflight** and the **manifest**. A file above the
   per-file cap of **512 MiB** is not downloaded; its launch has an unreadable `T0`, counted
   apart as `t0_over_cap` and reported (it still counts toward the lifecycle gate, so the bias
   against big launches is visible, not hidden).
4. **Go 2:** checks `go1_done.json`, the manifest hash and the manifest's own preflight
   (`within_cap`), re-derives the D2 list and compares its hash with the manifest, then D2,
   D3, D4, D5 and D6 in rounds (one funding page per unfinished launch per round).

**Fail closed.** The first response of every JSON class is a schema probe and **any** schema
mismatch stops the run with a fixed reason and the key tree, never a value. The HTML and gzip
classes (C3, D1, D2) have no key tree; their reducers fail closed (no anchors, no
directories, not gzip, a bad timestamp, a row from the cut). The page formats of
`public.bybit.com` are assumptions until the first probe (the symbol listing pattern is the
one an earlier study already used). A compressed transfer encoding is refused.

**A stop is a persisted run state.** An HTTP 403, a schema mismatch, a cap, a failed
preflight or a changed D2 list writes `stop.json` in the run root and ends that root:
a new fetcher on it refuses to start, and the report of a stopped run is `stage1_stopped`,
never a pass. There is no longer an optional class that could swallow a 403; a global 403
outranks every class rule. (A wrong manifest hash given for go 2 only refuses to start; nothing
is sent and the root is not stopped.) After a stop the only way on is a new root (with `--prior-root` to
carry the budget), by a corrected revision and the owner's decision. A crash without a stop
(the process dies) does resume in the same root. **The stop survives a crash between its two
writes:** the fatal receipt (HTTP 403 or a schema mismatch) is written first and `stop.json`
second, so on start a fetcher that finds a fatal receipt with no stop file writes the missing
stop and refuses to continue; nothing is resent. Every other stop kind (cap, preflight,
changed D2 list, a phase 0 class that failed) writes `stop.json` in one step.

**Binding.** `run.json` holds the full binding: the `request_list_sha256`, the budget carried
from earlier roots and the **code binding** (clean revision, the hashes of every published file
including the parsers, the contract and this document). A resume under any different binding is
refused, so a parser fix cannot continue an old run: a corrected revision starts a new root.

**Retries and pacing:**

- One request at a time, at least **1 s** apart. **After an HTTP 429 the spacing doubles**
  (1, 2, 4, 8 s, at most 8 s) and the slowdown **persists across a resume** (it is rebuilt
  from the receipts).
- At most **3 sends** per request, **counted from the attempt journal**: every attempt is
  written to `attempts/` before the request, so a send whose process died before its receipt
  was written still counts and a fourth send never happens. HTTP 429, 500, 502, 503, 504, a
  transport failure and a Bybit rate-limit or transient `retCode` in a 200 envelope (below) are retried; the pause before the next send is
  the server's `Retry-After` if a whole number up to 300 s, else **10 s, then 30 s**.
  **The wait survives a restart:** each receipt carries its wall-clock time and the
  `Retry-After` it received. A recovered request waits the rest of its `Retry-After` (the time
  already elapsed is credited), and a recovered run holds a persisted **not-before** time for
  every request, together with the doubled spacing.
- **A Bybit error envelope (HTTP 200 with `retCode` other than 0)** records **only its `retCode`
  and its short `retMsg`** (at most 200 characters) in the receipt: an error envelope carries no
  market data, and a success envelope still records nothing of the kind. **Retried** under the
  pacing rules above: the rate-limit codes 10006 and 10018 (each also doubles the spacing, like an
  HTTP 429) and the transient codes 10000 and 10016 (timeout, server error).
  Code 10018 is deprecated in the source table and retained only as a bounded
  compatibility case for its former IP-rate-limit meaning. Code 10019 belongs
  to the WebSocket trade service; this REST fetcher treats it as fatal.
  **Any other code is fatal and the stop names it** (`D5: api error retCode 10001`);
  an unknown code is never guessed. A body whose shape is wrong (no integer `retCode`, no string
  `retMsg`, not an object) is still a **schema mismatch** and fatal. After three sends a retried
  request is `unavailable`, as for any transient failure. Sources of the table: the repo's own
  live note (retCode 10016, "svc error: Get kline failed", about 1% of public kline requests on
  2026-10-26, in `bybit_post_funding_screen.py`) and the Bybit V5 error-code page
  (<https://bybit-exchange.github.io/docs/v5/error>), independently read on **2026-11-18**.
  `retCode` must be an actual integer before success or retry classification;
  booleans and floats are invalid, including `false` and `0.0`. A successful
  response needs integer zero and a dictionary `result`; an error envelope
  additionally needs a string `retMsg`. The tables and type rule are part of the
  contract. A non-JSON body or unexpected HTTP status records a fixed schema
  reason before stopping; neither path may escape as an unrecorded `KeyError`.
- **HTTP 403 stops the whole run at once, no retry.** Any other 4xx, a redirect (not
  followed), a non-JSON or unexpected body, or a schema mismatch also stops it.
- A 404 on a listing or a file is a **result** (`absent`). After three failed sends a
  per-launch request is `unavailable`, which the verdict counts as missing evidence, never as
  covered. A phase 0 class that stays unavailable stops the run.
- **Bounded reads, and a hard byte ceiling.** Requests ask for `Accept-Encoding: identity`. The
  transport (httpcore) reads at most **64 KiB** per call, so no chunk of a response is larger
  than that; the contract fixes this as `read_reserve_bytes` = **65,536**. The rule is
  `used + response cap + read reserve <= budget`, checked before every request against **both**
  the step budget (300 MiB for go 1, 8.5 GiB for go 2) and the class budget. So the last block
  is a numeric reserve **inside** every budget, and the bytes received can never exceed the
  stated ceilings: the 300 MiB and 8.5 GiB are hard ceilings, not targets. A declared
  `Content-Length` over the response cap reads nothing. Otherwise the raw chunks are read and
  **every received byte is accounted** (counted, hashed, journaled); the read ends at the first
  chunk over the response cap, which is at most the reserve beyond it. A chunk larger than the
  reserve (the transport breaking its 64 KiB read) is counted in full, stops the run and is
  never hidden. A send that crashed before its receipt is charged its cap plus the reserve.
- **No cross-revision reuse of pages** (decision Q15). `--prior-root` carries attempts
  and bytes into the budget **per go**: usage of go 2 classes counts against go 2 only and usage
  of go 1 classes against go 1 only, so old first files cannot block a new go 1.

## 5. How M1 to M6 and the stop codes are computed

The verdict module reads the receipts of a run and makes no request. Launch statuses are
assigned in one fixed order, each launch gets exactly one, and the counts add up:
`t0_unavailable`, `t0_ambiguous`, `identity_excluded`, `window_censored`, `lookback_censored`,
`funding_uncovered`, `entry_unavailable`, `primary`. `n_primary` is the protocol's formula
(section 2) with `t0_unavailable` and `entry_unavailable` made explicit.

| Check | Rule at stage 1                                                                                                                                                                                                                                                                                       | Stop code             |
| ----- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------- |
| M1    | `d = first public trade - launchTime`: `aligned` -5 min to +60 min, `delayed_open` up to +24 h, else `premarket_or_ambiguous`. Pass: ambiguous at most 10%, readable at least 90%. Lifecycle: no archive, an unreadable or over-cap first file at most 15%. The first public trade is enough for `T0` | `not_reconstructible` |
| M2    | (i) catalog launches with no archive file within 1 day of `launchTime` at most 5%; (ii) archive-only symbols at most 5% of the union (see the scope limit below; unlisted candidates count in the worst case); (iii) V1 launches missing more than 5% of path days at most 5%                         | `incomplete_coverage` |
| M3    | `relist`, `multiplier_ambiguous` (catalog, with the delivery date inside the window), `ticker_reuse_suspected` (file list), `identity_unverifiable`; contradicting at most 10%, unverifiable at most 20% of the launches that survive M1 and M2, at least 100 episodes remain                         | `identity_unresolved` |
| M4    | **pagination finished and no gap longer than 8 h**: a coarse check, labelled partial (below). Share uncovered at most 5%                                                                                                                                                                              | `incomplete_coverage` |
| M5    | at least 95% of the episodes have the entry minute trade bar (else `entry_unavailable`) and **continuous** hourly trade and mark series over the V1 path (below)                                                                                                                                      | `incomplete_coverage` |
| M6    | **PARTIAL check.** Catalog fields only: `minNotionalValue` at most USD 50, `maxLeverage` at least 2, `qtyStep` and `minOrderQty` present; unknown counts against; at least 90% feasible. It can only fail an order                                                                                    | `capital_infeasible`  |
| floor | `n_primary` below 100 or fewer than 26 ISO weeks                                                                                                                                                                                                                                                      | `insufficient_events` |

**M4, funding coverage (a coarse check, not proof).** Finishing the pagination is not enough. A
short page covers from the span start, a full page only from its oldest row (HYP-036), and four
full pages without reaching the start are `pagination_exhausted`. The coarse check passes only if
the timestamps of all pages form one chain with **no step above 8 hours** (a gap inside a page or
between two pages is `gap_inside` or `gap_between`), the oldest row is at most one step after the
span start (`starts_late`), and the newest row is at most one step before the span end unless the
catalog delivered the symbol by then (`ends_early`, else an end event). A page that did not come
back is `unavailable`. The reason of a pass is **`no_gap_over_8h`**, never "proven". **It does not
show that every settlement is present:** for a symbol whose real interval is shorter than 8 h,
missing settlements below the step go unseen, and the real historical interval is unknown (the
catalog `fundingInterval` is a **current snapshot**). The report therefore counts the covered
launches whose snapshot interval is exactly 8 h apart from those whose snapshot is shorter or
absent, and the M4 status is `partial_pass`, never a pass. A confirmed historical schedule cannot
be had without reading rates, so none is used.

**M5, hourly continuity.** Each series is checked for a hole: the reducer records the runs of
missing hours, and any run that overlaps the V1 path (the hour of `E` to the hour before `X`)
is an `interior_gap`. A series that starts after the entry hour is `starts_late`; one that
ends before `X` is `ends_early` unless the catalog delivery time is at or before the first
missing hour (`confirmed_event`, the end-of-archive rule). A page that may be cut by its row
limit or whose gap list was truncated is **`unproven`**: never green, and it makes M5
`not_assessed`. A missing trade hour counts as a gap, conservatively (a zero-trade hour is
treated as missing at this resolution; the stage 4 rule for zero-trade minutes is unchanged).

**Outcomes.** The outcome is the first stop code of the assessed checks, else `stage1_stopped`
if the run has a `stop.json`, else `stage1_incomplete` if anything is `not_assessed`, else
**`stage1_pass_partial`**. It is never a plain "pass": the report carries `partial_checks`
(all partial checks, listed next). After go 1 alone the report is the **go 1
report** (`go1_complete` or a provisional stop code): it shows M2 and M3 shares, M1 by file
name, M6 and the manifest, and lists what is not yet assessable.

**`partial_checks` in the report lists every one of these**, so a `stage1_pass_partial` is never
read as full proof: (1) **M6** static catalog fields only; (2) **M2 (ii)** the archive-only share
is a lower bound; (3) **M4** the funding step is coarse (no gap longer than 8 h) and the interval
is a snapshot; (4) **M5** minute-level continuity is deferred to stage 4; (5) any hourly series
that is **unproven** (a truncated page), counted.

**Scope limits, stated plainly:**

- **M2 (ii) is a lower bound and is never proven.** Only directories absent from the catalog
  whose last file is inside the window are candidates. A symbol delisted after the cut and
  missing from the catalog cannot be told from a test-year launch, so it is not kept (section 1,
  item 3). A value at or below 5% does not show the true share is; the status is
  `lower_bound_only_unproven`. A lower bound **above** 5% is a sure fail
  (`fail_lower_bound_above_limit`). The M2 gate may pass on (i) and (iii) while (ii) stays
  unproven, and the outcome says so.
- **M6 does not show that an order opens.** No price, quantity rounding, risk limit or depth
  is checked. The report labels it `partial` and lists `not_checked`.
- **Minute-level continuity** of the whole path is proven at stage 4, when the bars are
  fetched (Amendment 1).

## 6. What changed since revision 1 (`e393458`), and in round 2 (`45104e7`)

| Review item                                                                           | Change                                                                                                                                                                          | Remaining limit                                                            |
| ------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------- |
| P1-1 cut broken: spot kept undated symbols; root listing counted excluded directories | spot class removed; the root reducer keeps nothing not proven inside the window and no counts; unknown date is not allowed; Q4 dropped                                          | M2 (ii) is a lower bound                                                   |
| P1-2 pass without path coverage: only the ends of a series, a short funding page      | hourly continuity from recorded runs of missing hours; funding "finished" separated from "proven" (8 h chain, ends); unproven is never green                                    | one missing settlement of a short interval; zero-trade hours count as gaps |
| P1-3 resume with different code                                                       | the full binding (revision and code hashes) is compared; a difference needs a new root                                                                                          | none                                                                       |
| P1-4 stop was not a run state; a 403 on the optional class was swallowed              | `stop.json`; no continuation, no success verdict; no optional class remains                                                                                                     | none                                                                       |
| P2-5 fourth attempt after a crashed send                                              | sends counted from the attempt journal                                                                                                                                          | none                                                                       |
| P2-6 size cap checked after reading extra bytes                                       | bounded transport read with an identity encoding; only bytes under the cap are counted                                                                                          | none                                                                       |
| Reviewer A P2: a closed old contract judged as `multiplier_ambiguous`                 | a prior symbol's delivery date inside the window is kept (closed material) and compared with the launch                                                                         | none                                                                       |
| Q1 two gos; Q3 book removed; Q13 spot removed                                         | section 0; classes F and C2 deleted from the code and from the hash                                                                                                             | book and spot dates stay later decisions                                   |
| Q2, Q8 minute path deferred                                                           | Amendment 1; hourly continuity is checked now                                                                                                                                   | protocol M5 text deviated from, by amendment                               |
| Q5, Q6, Q7, Q9, Q10, Q11, Q14, Q15                                                    | closed material; M6 labelled partial; first trade is enough; thresholds frozen; unknown stays unknown; over-cap files counted apart; slowdown persists; no cross-revision reuse | as stated in sections 4 and 5                                              |

Round 2, after the review of `45104e7`:

| Review item                                                     | Change                                                                                                                  | Remaining limit                                  |
| --------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------ |
| P1 stop lost on a crash between the 403 receipt and `stop.json` | a fatal receipt alone stops a recovering fetcher, which writes the missing `stop.json`                                  | none                                             |
| P2 byte cap not closed: extra bytes received but not journaled  | declared length over the cap reads nothing; otherwise raw chunks, all received bytes accounted, read ends over the cap  | overshoot of at most one transport chunk         |
| P2 `Retry-After` lost on a restart                              | receipts persist the time and the `Retry-After`; the rest of the wait and a not-before time are honoured after recovery | wall clock must be sane                          |
| P2 costs of the two gos mixed through `--prior-root`            | prior usage kept per go                                                                                                 | none                                             |
| Status: 8 h funding step called `proven`                        | reason `no_gap_over_8h`, status `partial_pass`, interval snapshot strength counted                                      | unseen missing settlements of 1 h or 4 h symbols |
| Status: M2 (ii) lower bound reported as assessed and passing    | `archive_only_proven` false, status `lower_bound_only_unproven`; it can fail but never prove                            | true archive-only share unknown                  |
| Status: list all partial checks                                 | `partial_checks` lists M6, M2 (ii), M4, minute continuity and unproven series                                           | none                                             |

Round 3, after the review of `68c296a`:

| Review item                                                                     | Change                                                                                                                                 | Remaining limit                                       |
| ------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------- |
| P2 the step byte ceiling was not hard: the block crossing it was still received | a 64 KiB read reserve inside every step and class budget; a request starts only if cap plus reserve fits; chunks over the reserve stop | relies on the transport's 64 KiB read, and is checked |

## 6b. Amendment after the Go 1 run (revision 3)

**Finding.** Go 1 ran on `f6eb5c3` (526 attempts, 11.4 MB, no stop, outcome `go1_complete`). Its
HEAD sizes of the first daily files show that **22 of the 258 files are over the 64 MiB
per-file cap**, so those launches would have had no `T0`. Measured from the Go 1 receipts
(`facts.content_length` of D2h): **258 files, 6,425.47 MiB** in total; the 22 over-cap files
total 2,908.84 MiB and the largest is 408.70 MiB (428,549,908 bytes); 236 files (3,516.63 MiB)
are at most 64 MiB. All 258 are under 512 MiB. Go 2 was **not** run at 64 MiB.

**Change.** One per-file cap of **512 MiB** for D2 (`PER_FILE_CAP_BYTES`, also the D2 response
cap). Recomputed budgets (revision 4 raised the D2 class cap, the Go 2 step cap and the Go 2
attempt cap after the review of revision 3; the first two rows of the old column are revision 3):

| Quantity         | Before        | After                        | Why                                                                                                                                                                                      |
| ---------------- | ------------- | ---------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| D2 response cap  | 64 MiB        | 512 MiB                      | keeps all 258 files, with 103 MiB of margin over the largest                                                                                                                             |
| D2 class cap     | 6,144 MiB     | **7,936 MiB** (7.75 GiB)     | covers the retry scenarios below in the real queue order, with the 512 MiB + 64 KiB reserve needed before every request; 42 MiB of slack over the three-retry case                       |
| Go 2 step cap    | 6,656 MiB     | **8,704 MiB** (8.5 GiB)      | D2 class cap 7,936 + D3 to D6 at their own class caps (24 + 150 + 150 + 300 = 624) = 8,560, plus 144 MiB of slack: the step budget can never starve a class that is within its class cap |
| Go 2 attempt cap | 1,700         | **2,400**                    | worst case 2,064 first attempts plus 16% retry headroom (below)                                                                                                                          |
| Go 1 step cap    | 300 MiB       | 300 MiB                      | Go 1 used 11.4 MB                                                                                                                                                                        |
| Combined ceiling | about 6.8 GiB | 9,004 MiB, **about 8.8 GiB** | 300 + 8,704 MiB                                                                                                                                                                          |
| Read reserve     | 64 KiB        | 64 KiB                       | per request, set by the transport's read size, not by the file size; a D2 request needs `512 MiB + 64 KiB` of headroom in both budgets before it starts                                  |

**What the D2 class cap covers.** The check before every D2 request is `bytes so far + 512 MiB +
64 KiB <= class cap`, so the cap must cover the bytes already downloaded plus that reserve at
the last request, not only the total. Replayed on the real queue (the 258 measured sizes in the
order the queue requests them, sorted by symbol), the cap needed is: no retry 6,897.4 MiB; one
full retry of the largest file (right after its first attempt) **7,306.2 MiB**; one full retry of
each of the two largest 7,693.3 MiB; two full retries of the largest 7,714.9 MiB; one full
retry of each of the **three largest 7,894.1 MiB**; the four largest 8,051.7 MiB; three retries
of the largest 8,123.6 MiB. So 7,936 MiB **covers**: no retry, one full retry of the largest
file, one full retry of each of the three largest (the three are 408.7, 387.1 and 200.8 MiB), or
two retries of the largest. It **does not cover** a full retry of each of the four largest, or
three retries of the largest; in those cases the run stops cleanly by the class cap and never
overshoots. (Revision 3 had 7,168 MiB, which a single full retry of the largest file stopped
before the last files, at 250 of 258.)

**Is the Go 2 budget enough?** Bytes: yes, as above; at the step cap of 8,704 MiB every class can
run to its own class cap at once. Attempts: the **worst case is not an estimate from the
current catalog**: the catalog `fundingInterval` is a snapshot, so the historical page count is
not proven. The contract allows 4 funding pages per symbol, so the worst case for 258
candidates is 258 (D2) + 3 x 258 (D3 to D5) + 4 x 258 (D6) = **2,064 first attempts**. The cap of
**2,400** leaves 336 attempts, 16%, for retries (three sends per request at most, counted from
the journal). D6 can need up to 1,032 requests of its class maximum of 1,200, and the classes'
maximum requests sum to 2,400, so no class cap binds before the attempt cap. With the current
catalog the expected need is far lower (258 + 3 x 228 + 239 = 1,181 requests, about 1,430 with 10%
retries); a run that exhausts 2,400 attempts would stop cleanly and not overshoot.

**How the verified Go 1 metadata is carried: option (b), rerun Go 1 in a new root.**

- **Why not reuse (a).** The per-file cap is in the shared rules, which are part of **both** go
  hashes, so the Go 1 hash changes (the old Go 1 was approved on `5f4d84d3...`; the new one is
  `0937f3b3...`). A reuse would need a prior-root mechanism bound by hash that proves identical
  Go 1 class definitions and parse code by comparing normalized definitions, as the catalyst
  study did. That mechanism is not implemented (decision Q15, no cross-revision reuse), would be
  new unreviewed code on the critical path, and the existing `--prior-root` budget carry would
  count the old 526 attempts against the new Go 1 cap of 1,000 and block a rerun.
- **Why (b).** Go 1 costs 526 requests, about 11 MB and about 10 minutes, on public metadata. It
  produces a fresh catalog, listings, HEAD sizes and manifest under the new hashes, with no trust
  in reuse, and the old root stays a record. Go 2 is then approved on the new manifest hash.
- **Trade-off.** The rerun is **up to 1,000 new attempts** (526 expected) on top of the 526
  already spent (1,052 expected across the two Go 1 reads) against not trusting reuse, and it needs
  a **new owner go** under the unchanged go 1 hash `0937f3b3...`. Both Go 1 reads are coverage level on the same year. Do **not** pass the old root as
  `--prior-root` for the new Go 1.

## 6c. Amendment after the Go 2 partial run (revision 5)

**What happened.** Go 2 ran on `e70a1b4` in a new root after a clean Go 1 rerun (manifest SHA-256
`759305ceaf5730ff0b7f23963a2c17292590ecb590d0336376cc139a0b5ca76d`, all 258 first files, none
over the cap) and **stopped fail-closed** with `D5: schema mismatch (envelope)` after 589 Go 2
receipts in 13.5 minutes: D2 258 ok (6,425.47 MiB, 7.9 minutes), D3 110 ok, D4 110 ok, D5 109 ok,
D6 not started. 110 of the 228 entry candidates were reached.

**What the two odd D5 receipts are, from the receipts and the code** (bodies are not kept, by
design: only a key tree, a size and a hash).

|                   | seq 856, CHILLGUYUSDT                                                                                                                                                          | seq 1115, TUTUSDT                                                                                                                                                                                                          |
| ----------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Receipt           | HTTP 200, 123 bytes, outcome `error`; seq 857, the same request 10.6 s later, ok, 34,045 bytes                                                                                 | HTTP 200, 106 bytes, outcome `schema_mismatch`, reason `envelope`, key tree `result:dict, retCode:int, retExtInfo:dict, retMsg:str, time:int`                                                                              |
| Branch that fired | `classify_status` parsed the body as JSON and found `retCode == 10006` (the **only** way to get `error` with HTTP 200 in the old code), so it was retried with the 10 s ladder | `classify_status` passed it (valid JSON, retCode not 10006); the kline reducer called `_envelope`, which raises `envelope` when `retCode != 0`, and stopped the whole run                                                  |
| Known             | the body was a JSON object with `retCode` exactly 10006 (rate limit)                                                                                                           | the body was a well-formed envelope with an **integer retCode that is neither 0 nor 10006**: with retCode 0 and a dict result the reducer would have failed later with another reason (`symbol` or `list`), not `envelope` |
| Not known         | the message text (see the inference)                                                                                                                                           | **the retCode and the message**                                                                                                                                                                                            |

Why one was retried and the other fatal: the old code recognised exactly one code, 10006, as
retryable, and sent every other non-zero code to the reducer, where it is a fatal `envelope`
mismatch. **Inference, not proof:** an envelope without data is 78 bytes plus the message
(`{"retCode":10006,"retMsg":"","result":{},"retExtInfo":{},"time":<13 digits>}`), and the
10006 message "Too many visits. Exceeded the API Rate Limit." is 45 characters, so 78 + 45 = 123
matches seq 856 exactly. For seq 1115, 106 - 78 = 28 characters of message. The repo's own note
records that Bybit answers public kline requests with retCode 10016, "svc error: Get kline
failed" (27 characters), on about 1% of requests; TUTU's mark-price request one second earlier
succeeded with the same parameters. A transient server error fits, one byte off a 5-digit code
and that message. It is a hypothesis; the receipt cannot confirm it.

**Fix.** Section 4: an error envelope records only `retCode` and `retMsg`; rate-limit and
transient codes are retried; any other code is fatal and named; a wrong shape stays a schema
mismatch. The tables are in the contract, so **all three hashes change** (the shared rules carry
them, and the expected counts are updated to the measured 228 entry candidates and 239 funding
pages, 1,181 requests for Go 2). The old CHILLGUY retry did not double the spacing (only HTTP 429
did); it now does, and the slowdown persists across a restart.

**Rerun: option (a), a plain rerun in a fresh root, not a hash-bound reuse.** Q15 and the full
binding forbid resuming a root under different code, and the parse code changed (the envelope
check is in every JSON reducer, Go 1's catalog included), so both gos run again in a new root.
Reuse of the D2 (and Go 1) receipts whose class code is unchanged would need new trust-bearing
code: a normalized-definition comparison across revisions, a hash-bound import of receipts into
a root with a different run binding, and per-class carry of the budget. D2 saved 7.9 minutes and
6.3 GiB of public archive transfer; that does not pay for a new mechanism on the critical path.
**Recommend (a).** Cost of (a):

|                                                      | Attempts                        | Bytes                                        |
| ---------------------------------------------------- | ------------------------------- | -------------------------------------------- |
| Spent so far (v1 Go 1 526; v2 Go 1 526 and Go 2 589) | 1,641                           | about 6.3 GiB (11.4 MB + 11.4 MB + 6.28 GiB) |
| The rerun (fresh root), expected                     | 526 + about 1,181 = about 1,707 | about 6.4 GiB (D2 6.28 GiB again)            |
| The rerun, hard caps                                 | 1,000 + 2,400 = 3,400           | 300 MiB + 8,704 MiB                          |
| **Study total after the rerun, expected**            | **about 3,350**                 | **about 12.7 GiB**                           |
| **Study total, worst case within caps**              | **1,641 + 3,400 = 5,041**       | **about 6.3 + 8.8 = 15.1 GiB**               |

A fresh root has fresh caps; the totals above are the study's, not any root's. The rerun needs a
new owner go for Go 1 under the new Go 1 hash and a second for Go 2 on the new manifest hash.

**Diagnostic (run on the owner's go, one request): retCode 0, retMsg "OK".** The fix records the
retCode on the next run whatever it is, so the diagnostic was not needed for correctness; it was
worth one request because an unknown or deterministic code for TUTU would have stopped the rerun
again at the same candidate, after 7.9 minutes of D2 and 6.3 GiB. The request was rebuilt from the
saved receipts (its key equals seq 1115's), sent once to `api.bybit.com` with no retry, and only
`retCode` and `retMsg` were read; the response was piped and never stored:

```bash
curl -sS 'https://api.bybit.com/v5/market/kline?category=linear&symbol=TUTUSDT&interval=60&start=1742638383361&end=1744193583361&limit=1000' | jq '{retCode, retMsg}'
```

**Result: `retCode` 0, `retMsg` "OK".** The error that stopped Go 2 at seq 1115 **did not
reproduce**: the same request, with the same parameters, now succeeds. So the parameters are
accepted at the time of the diagnostic. This is consistent with a transient failure,
but does not establish its original cause or guarantee a later rerun will succeed.
**Not established:** the retCode of the original answer. The hypothesis of 10016
("svc error: Get kline failed") stays unconfirmed; nothing in a success answer can
confirm it, and the success body was not
looked at beyond those two fields. The request is **one extra request outside any run budget**
(coverage level, discovery year, window 2025-03-22T10:13:03.361Z to 2025-04-09T10:13:03.361Z).

## 7. Interpretations made, and limits stated plainly

**Revision 6, review of PR 618 at `eab8713`.** Synthetic requests reproduced three
defects: `false` and `0.0` were accepted as successful zero codes; WS-only 10019
was retried by the REST fetcher; malformed JSON and unexpected HTTP statuses
could raise `KeyError` while formatting a missing schema reason. Typed code
validation now precedes API classification and also protects direct reducers;
10019 stops on its first response; all schema-stop paths persist a fixed reason.
Regression cases exercise receipt recovery without `stop.json`. The error-code
page was read independently, and the diagnostic wording above no longer claims
that one later success proves the earlier error transient. No new market-data
request was made. Shared rules and all approval hashes change; caps and windows
do not. Existing roots remain sealed under their original bindings.

1. **M2 (iii) uses the V1 path days** (calendar days from `E` to the last minute of the hold).
   More than 5% of 7 or 8 days means one missing day.
2. **`ticker_reuse_suspected`** is a first file more than 60 days before the launch, or a gap of
   more than 30 days between consecutive files that starts before the launch day. The
   first-file case is in practice caught earlier by M1 as `premarket_or_ambiguous`.
3. **`identity_unverifiable`** is no readable listing, or a catalog `baseCoin` that is missing
   or differs from the symbol's base. `change_known_at_entry` is **not observable** at stage 1:
   the catalog is a current snapshot and no machine-timed announcement feed is read.
4. **No entry filter and no price in selection.** Every status above is decided from dates,
   catalog fields and the **existence** of data.
5. Planning numbers (263 launches and 258 first files are measured by Go 1; 228 entry candidates and 239 funding pages are measured from the Go 2 receipts and the catalog) are not guarantees. The caps
   are the contract; the counts only tell the owner what to expect.

## 8. Run procedure (after the go, not now)

```bash
uv run python -m schurfer_analytics.new_perp_stage1 fetch --go 1 --approved-sha256 <go 1 hash> --root <absolute durable dir>
uv run python -m schurfer_analytics.new_perp_stage1_verdict --root <same dir> --out <go1-report.json>
# review the go 1 report and manifest.json; then, on the owner's second go:
uv run python -m schurfer_analytics.new_perp_stage1 fetch --go 2 --approved-sha256 <go 2 hash> --manifest-sha256 <manifest hash> --root <same dir>
uv run python -m schurfer_analytics.new_perp_stage1_verdict --root <same dir> --out <report.json>
```

`fetch` refuses an approved hash that is not this contract's hash for that go, a dirty tree, a
revision not published in `origin/main`, a contract file that differs from the code, and a
registry without this study's row. The registry row, this document, the contract JSON and the
code must be in `main` before the first request. No holdout, test-year or blind-window input is
requested anywhere.

## 9. Open questions (each with my recommendation)

1. **Zero-trade hours as gaps.** At hourly resolution a missing trade bar is a gap, which may
   fail an illiquid launch that had an hour without trades. _Recommend accept:_ it is
   conservative, and the stage 4 zero-trade minute rule is unchanged.
2. **The 8 h funding step.** It cannot see one missing settlement of a 1 h or 4 h symbol; it is
   reported as a coarse, partial check. An alternative is a row count against the catalog
   `fundingInterval` (a current snapshot, so also unproven). _Recommend accept the coarse label at
   stage 1;_ stage 4 fetches the exact rows it prices.
3. **M2 (ii) lower bound.** Reported as unproven. _Recommend accept:_ the only alternative keeps
   names whose launch date is unknown, which the cut forbids.
4. **Who approves go 2.** The owner, on the go 1 report and the manifest hash; the colleague
   reviews the report. _Recommend this split._
5. **Phase 0 may repeat once** (C1 max 9, C3 max 2). A third interruption stops the run.
   _Recommend accept._
6. **Per-file cap of 512 MiB** (raised from 64 MiB after Go 1, see the amendment below): all 258
   measured files fit; over-cap launches are still counted apart and toward the lifecycle gate.
   _Recommend keep._
7. **Budget.** 1,707 planned requests; attempt caps 1,000 (go 1) and 2,400 (go 2, covering a
   2,064-attempt worst case); 8.5 GiB for go 2 and about 8.8 GiB combined, recomputed from the
   measured file sizes by replaying the real queue. _Recommend accept._
8. **Outcome name.** `stage1_pass_partial`, never `stage1_pass`. _Recommend accept._

## 10. Verification performed

- Tests with a mock Bybit (no network), one regression test per review item that fails on
  `e393458` (the defects were reproduced on that revision first): the cut in every reducer, the
  funding proof, hourly continuity, the full binding, a persisted stop, the attempt journal and
  bounded reads, plus the two gos in and out of order, the manifest gate, a **full run into a
  temp root that is then scanned for sentinels**, and every M1 to M6 verdict and the stop-code
  order from receipts a run really wrote.
- The checked-in contract equals `build_contract()` and this document carries its hashes.
- Ruff, mypy (strict) and the full `make verify` gate before the push. **Nothing was fetched,
  no network request was made, and no data was read.**
