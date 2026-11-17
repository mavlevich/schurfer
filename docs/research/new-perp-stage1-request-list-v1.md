# I25 stage 1: contract and request list (v1, DRAFT, revision 2, 2026-11-17)

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
  1d1b7349526d93fd0396e8b6d572ecf761f6a4216161edb564f3bd234388b4c5
go 1 approval hash (go_sha256["1"]) =
  854eed7e766153792ce4742296570d36314a0d3c5a9a35ac27f36e65adc763ba
go 2 approval hash (go_sha256["2"]) =
  780b7e36d280f21660380f60cf7e5cae2e71b0e4086b072c2c1bc0fedf8673ef
contract.json file SHA-256 =
  6f84cd11fbcb8b8e2815da525b6c3e55a121b09f706cb1775d411d02fba94e6e
```

Check without a network request:

```bash
uv run python -m schurfer_analytics.new_perp_stage1 plan
uv run python -m schurfer_analytics.new_perp_stage1 write-contract --check
```

| Go  | Reads                                                                                        | Classes            | Planned requests | Max requests | Attempt cap | Byte cap |
| --- | -------------------------------------------------------------------------------------------- | ------------------ | ---------------- | ------------ | ----------- | -------- |
| 1   | **metadata only**: catalog fields, archive names, per-symbol listings, HEAD sizes. No body   | C1, C3, D1, D2h    | 547              | 761          | 1,000       | 300 MiB  |
| 2   | first daily files (column 1), one entry bar, hourly series, funding. Needs go 1 and manifest | D2, D3, D4, D5, D6 | 1,135            | 2,400        | 1,700       | 6.5 GiB  |

Planned total 1,682 requests (the protocol's section 10 said about 1,000: that counted
listings and first files only). Attempt caps include retries. At 1 second spacing go 1
takes about 10 minutes and go 2 about 20 minutes plus the first-file transfer.

**The gate between the gos.** Go 1 ends by writing `manifest.json` and `go1_done.json` in
the run root: the launch count, the exact D2 request list (as a hash), the preflight (the
summed `Content-Length` of the first files against the 6 GiB class cap, files over the
64 MiB per-file cap counted apart), and an upper bound on the entry candidates. The go 1
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
| 1   | C1    | `catalog_linear`  | GET `api.bybit.com/v5/market/instruments-info`                      | 3        | 9     | 3 MiB        | 27 MiB      |
| 1   | C3    | `archive_root`    | GET `public.bybit.com/trading/`                                     | 1        | 2     | 4 MiB        | 8 MiB       |
| 1   | D1    | `archive_listing` | GET `public.bybit.com/trading/{SYMBOL}/`                            | 293      | 450   | 1 MiB        | 256 MiB     |
| 1   | D2h   | `first_file_head` | HEAD `public.bybit.com/trading/{SYMBOL}/{SYMBOL}{FIRST_DAY}.csv.gz` | 250      | 300   | 0            | 0           |
| 2   | D2    | `first_file`      | GET `public.bybit.com/trading/{SYMBOL}/{SYMBOL}{FIRST_DAY}.csv.gz`  | 250      | 300   | 64 MiB       | 6,144 MiB   |
| 2   | D3    | `entry_trade_bar` | GET `api.bybit.com/v5/market/kline`                                 | 205      | 300   | 64 KiB       | 24 MiB      |
| 2   | D4    | `mark_hourly`     | GET `api.bybit.com/v5/market/mark-price-kline`                      | 205      | 300   | 512 KiB      | 150 MiB     |
| 2   | D5    | `trade_hourly`    | GET `api.bybit.com/v5/market/kline`                                 | 205      | 300   | 512 KiB      | 150 MiB     |
| 2   | D6    | `funding_history` | GET `api.bybit.com/v5/market/funding/history`                       | 270      | 1,200 | 256 KiB      | 300 MiB     |

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
  `multiplier_ambiguous` or `ticker_reuse_suspected`. Planning 205.

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

The first response of each JSON class also leaves a **key-only schema probe** (key paths and
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
   per-file cap of **64 MiB** is not downloaded; its launch has an unreadable `T0`, counted
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
(the process dies) does resume in the same root.

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
  transport failure and a Bybit `retCode` 10006 are retried; the pause before the next send is
  the server's `Retry-After` if a whole number up to 300 s, else **10 s, then 30 s**.
- **HTTP 403 stops the whole run at once, no retry.** Any other 4xx, a redirect (not
  followed), a non-JSON or unexpected body, or a schema mismatch also stops it.
- A 404 on a listing or a file is a **result** (`absent`). After three failed sends a
  per-launch request is `unavailable`, which the verdict counts as missing evidence, never as
  covered. A phase 0 class that stays unavailable stops the run.
- **Bounded reads.** Requests ask for `Accept-Encoding: identity`, and the body is read from
  the transport in chunks of at most `cap + 1` bytes; only bytes that fit under the response
  cap are accepted, hashed and counted. A body over the cap ends as `too_large` with exactly
  the cap bytes counted. Revision 1 counted the whole block that crossed the cap.
- **No cross-revision reuse of pages** (decision Q15). `--prior-root` carries only attempts
  and bytes into the budget.

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
| M4    | **pagination finished and settlements proven** (below). Share uncovered at most 5%                                                                                                                                                                                                                    | `incomplete_coverage` |
| M5    | at least 95% of the episodes have the entry minute trade bar (else `entry_unavailable`) and **continuous** hourly trade and mark series over the V1 path (below)                                                                                                                                      | `incomplete_coverage` |
| M6    | **PARTIAL check.** Catalog fields only: `minNotionalValue` at most USD 50, `maxLeverage` at least 2, `qtyStep` and `minOrderQty` present; unknown counts against; at least 90% feasible. It can only fail an order                                                                                    | `capital_infeasible`  |
| floor | `n_primary` below 100 or fewer than 26 ISO weeks                                                                                                                                                                                                                                                      | `insufficient_events` |

**M4, funding coverage.** Finishing the pagination is not a proof. A short page covers from
the span start, a full page only from its oldest row (HYP-036), and four full pages without
reaching the start are `pagination_exhausted`. Coverage is **proven** only if the timestamps of
all pages form one chain with no step above 8 hours (the longest Bybit funding interval; a gap
inside a page or between two pages is `gap_inside` or `gap_between`), the oldest row is at
most one step after the span start (`starts_late`), and the newest row is at most one step
before the span end unless the catalog delivered the symbol by then (`ends_early`, else an end
event). A page that did not come back is `unavailable`. Reasons are reported by count.
**Limit:** a single missing settlement of a 1 h or 4 h interval symbol is smaller than the
8 h step and is not detected.

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
(minute-level path continuity and M6, below). After go 1 alone the report is the **go 1
report** (`go1_complete` or a provisional stop code): it shows M2 and M3 shares, M1 by file
name, M6 and the manifest, and lists what is not yet assessable.

**Scope limits, stated plainly:**

- **M2 (ii) is a lower bound.** Only directories absent from the catalog whose last file is
  inside the window are candidates. A symbol delisted after the cut and missing from the
  catalog cannot be told from a test-year launch, so it is not kept (section 1, item 3).
- **M6 does not show that an order opens.** No price, quantity rounding, risk limit or depth
  is checked. The report labels it `partial` and lists `not_checked`.
- **Minute-level continuity** of the whole path is proven at stage 4, when the bars are
  fetched (Amendment 1).

## 6. What changed since revision 1 (`e393458`)

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

## 7. Interpretations made, and limits stated plainly

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
5. Planning numbers (263 launches, 205 entry candidates, 250 first files) are guesses. The caps
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
2. **The 8 h funding step.** It cannot see one missing settlement of a 1 h or 4 h symbol. An
   alternative is a row count against the catalog `fundingInterval` (a current snapshot).
   _Recommend accept the limit at stage 1;_ stage 4 fetches the exact rows it prices.
3. **M2 (ii) lower bound.** _Recommend accept:_ the only alternative keeps names whose launch
   date is unknown, which the cut forbids.
4. **Who approves go 2.** The owner, on the go 1 report and the manifest hash; the colleague
   reviews the report. _Recommend this split._
5. **Phase 0 may repeat once** (C1 max 9, C3 max 2). A third interruption stops the run.
   _Recommend accept._
6. **Per-file cap of 64 MiB** and over-cap launches counted apart and toward the lifecycle
   gate. _Recommend keep;_ the owner sees the count in the manifest before go 2.
7. **Budget.** 1,682 planned requests, 2,700 attempt caps and 6.5 GiB for go 2, recomputed for
   the two gos. _Recommend accept._
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
