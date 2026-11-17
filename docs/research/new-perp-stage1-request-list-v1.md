# I25 stage 1: contract and request list (v1, DRAFT, 2026-11-17)

Status: **draft for the colleague's review, then the owner's go.** This document and its
code fetch nothing. It is the stage 1 gate of the
[I25 laboratory protocol](new-perp-short-lab-protocol-v1.md) (section 11: "owner's go
per fetch, registry row, request list"): everything the owner needs to give **one
informed go** for the data-recoverability check M1 to M6 on the discovery year.

- Frozen contract: [`contract.json`](evidence/new-perp-stage1-v1/contract.json).
- Code: `apps/analytics/schurfer_analytics/new_perp_stage1.py` (contract, reducers,
  planner, fetcher) and `new_perp_stage1_verdict.py` (M1 to M6, stop codes, from receipts).
- Registry: the section "I25 stage 1 registration" of the
  [seen-windows registry](seen-windows-registry.md).

## 0. What the owner is asked to approve

**Request list hash** (the hash bound into every run; it covers the whole contract with
this field removed, optional classes included):

```text
request_list_sha256 = 79aa32aa033c87618ff8c35d94166772245954d26946b37b854e961e646b2f1e
contract.json file SHA-256 = 92985aa2c1f8fa7d669fb6ddecdf2eb1fe8448872b10448622d57f945fc5b565
```

Check without a network request:

```bash
uv run python -m schurfer_analytics.new_perp_stage1 plan
uv run python -m schurfer_analytics.new_perp_stage1 write-contract --check
```

**One go covers these coverage reads** (protocol section 11, item 2 names funding,
mark-price and book reads as needing a separate go; this document asks for them in the
same go, and says below which one could be split):

| Group                     | Classes        | What is read                                                                              |
| ------------------------- | -------------- | ----------------------------------------------------------------------------------------- |
| Catalog and archive names | C1, C2, C3, D1 | instruments catalog fields, spot catalog, trade-archive directory and per-symbol listings |
| First daily trade files   | D2h, D2        | size of, then the whole first daily trade file of each launch, column 1 only              |
| Bar and mark existence    | D3, D4, D5     | one entry minute bar, hourly trade and hourly mark-price series, start times only         |
| Funding coverage          | D6             | funding history pages, page boundaries and timestamps only                                |
| Order-book existence      | F (optional)   | HEAD requests for the V1 entry-day and exit-day book files; no body, no volume            |

**Totals.** Planned **1,773 requests** for the core classes (C1 to D6) and **410** for
the optional book class F: **2,183** in all. Hard caps: **2,600 attempts** for the
core classes and **3,200** with F, retries included, and **6.5 GiB** of response bytes
in total (6,656 MiB; first daily files are 6,144 MiB of it). At the planned 1 second
spacing the requests alone take about 40 minutes, plus the first-file transfer time.

The protocol's section 10 planned "on the order of 1,000 requests and a few GiB". The
planning number counted listings and first files only. This list adds what M4 and M5
need (funding, entry bar, hourly existence) and the book existence, which is why the
total is about twice that; the byte planning of "a few GiB" is unchanged and is fixed by
the preflight below.

**Not covered by this go** (each stays a separate decision, protocol section 11): the
backward holdout and stage 1b; any assignment or count of the HYP-031 test year
(2025-10-01 to 2026-09-28); any recording inside the HYP-012 v2 blind window (from
2026-09-29); confirming the order-book archive as an official source and any book
volume cap (item 7); the owner's fee tier (item 8); the laboratory read of returns;
any paper, LIVE_PROBE or order path; and the merge of this PR.

## 1. Scope and the cut: how test-year and blind-window names never get in

Stage 1 is the **discovery year only**, 2024-10-01 to 2025-09-30 (**cut = 2025-10-01T00:00Z**).
The directory listings of the trade archive, and the catalog, hold names and dates from
the test year and the blind window. The rules, all enforced **inside the reducers**
(`new_perp_stage1.py`) and each pinned by a test that compares the output with and
without the excluded rows:

1. **Catalog rows.** The launch time is read first. A row launched at or after the cut
   is skipped before any other field of it is read. It is not stored and **not counted**.
2. **No total row counts.** No receipt holds the number of rows of a response, only the
   number of rows that were kept. So the number of test-year launches cannot be read
   from a receipt or a report (protocol section 11, item 5).
3. **Listings.** File names dated at or after the cut are dropped uncounted. A symbol
   whose files all start after the cut is recorded as having no file before the cut, with
   no date.
4. **Bars, marks, funding.** Every span ends at or before `cut - 1 ms`. A row dated at or
   after the cut that a server returns anyway is dropped uncounted.
5. **Eligibility is cut before entry.** A launch whose planned exit `X` is after the cut
   is `window_censored` and gets **no** bar, mark or funding request. The last V1
   eligible launch is the one with `T0 + 14 d <= cut`.
6. **Archive directory names.** The root listing is reduced in memory against the whole
   catalog, **including the post-cut symbols**, so a directory of a catalog symbol is
   never a candidate and no name of a test-year launch is requested or recorded. See
   open question 4 for the one remaining case.
7. **Prices, rates and the post-cut symbol cannot reach a file.** The tests give the
   fetcher a synthetic Bybit whose every price, rate and tick size is a sentinel and which
   holds a post-cut launch, then check that **no file of the run root contains any
   sentinel** and that no request touched the post-cut symbol or a date from the cut.

## 2. The request classes

Counts are requests (first attempts); the attempt cap is global. "Per response" is the
byte cap of one response, "class bytes" the cap of all responses of the class.

| Class | Name              | Method and endpoint                                                                  | Expected | Max   | Per response | Class bytes | In go         |
| ----- | ----------------- | ------------------------------------------------------------------------------------ | -------- | ----- | ------------ | ----------- | ------------- |
| C1    | `catalog_linear`  | GET `api.bybit.com/v5/market/instruments-info`                                       | 3        | 9     | 3 MiB        | 27 MiB      | yes           |
| C2    | `catalog_spot`    | GET `api.bybit.com/v5/market/instruments-info`                                       | 1        | 4     | 3 MiB        | 12 MiB      | yes, optional |
| C3    | `archive_root`    | GET `public.bybit.com/trading/`                                                      | 1        | 1     | 4 MiB        | 4 MiB       | yes           |
| D1    | `archive_listing` | GET `public.bybit.com/trading/{SYMBOL}/`                                             | 383      | 750   | 1 MiB        | 256 MiB     | yes           |
| D2h   | `first_file_head` | HEAD `public.bybit.com/trading/{SYMBOL}/{SYMBOL}{FIRST_DAY}.csv.gz`                  | 250      | 300   | 0            | 0           | yes           |
| D2    | `first_file`      | GET `public.bybit.com/trading/{SYMBOL}/{SYMBOL}{FIRST_DAY}.csv.gz`                   | 250      | 300   | 64 MiB       | 6144 MiB    | yes           |
| D3    | `entry_trade_bar` | GET `api.bybit.com/v5/market/kline`                                                  | 205      | 300   | 64 KiB       | 24 MiB      | yes           |
| D4    | `mark_hourly`     | GET `api.bybit.com/v5/market/mark-price-kline`                                       | 205      | 300   | 512 KiB      | 150 MiB     | yes           |
| D5    | `trade_hourly`    | GET `api.bybit.com/v5/market/kline`                                                  | 205      | 300   | 512 KiB      | 150 MiB     | yes           |
| D6    | `funding_history` | GET `api.bybit.com/v5/market/funding/history`                                        | 270      | 1,200 | 256 KiB      | 300 MiB     | yes           |
| F     | `book_head`       | HEAD `quote-saver.bycsi.com/orderbook/linear/{SYMBOL}/{DAY}_{SYMBOL}_ob500.data.zip` | 410      | 1,200 | 0            | 0           | yes, optional |

Parameters:

- **C1:** `category=linear`, `status` in `Trading`, `Delivering`, `Closed`, `limit=1000`,
  `cursor` while a next page exists (at most 3 pages per status).
- **C2:** `category=spot`, `limit=1000`, `cursor` while a next page exists.
- **D1:** one request per in-window catalog launch (planning 263, hard cap 300) and one per
  **archive-only candidate** (a directory absent from the whole catalog with a modification
  date from 2024-10-01 or none; planning about 120, hard cap 450). Above either cap the plan
  stops with `plan_exceeds_cap` and nothing is truncated.
- **D2h and D2:** only for a launch whose first listed day is within one day of its
  `launchTime`. A first file two or more days away decides the M1 class by its name alone
  (`premarket_or_ambiguous`) and is never downloaded.
- **D3:** `category=linear`, `interval=1`, `start=end=E`, `limit=1`, where `E` is the V1
  entry minute (`T0 + 7 d` rounded up to the whole minute).
- **D4, D5:** `interval=60`, `start = T0 + 3 d`, `end = min(T0 + 21 d, cut - 1 ms)`,
  `limit=1000` (one page holds the 456 hourly bars).
- **D6:** `startTime = T0 + 3 d`, `endTime` first `min(T0 + 21 d, cut - 1 ms)` and then
  just before the oldest row of the previous full page, `limit=200`, at most 4 pages.
- **F:** `ob500` for the V1 entry day and for the day of the last minute of the hold; `ob200`
  only where `ob500` answers 404.
- D3 to D6 and F are asked **only for entry candidates**: launches with a readable,
  unambiguous `T0` (`aligned` or `delayed_open`), not `window_censored`, not
  `lookback_censored`, and not `relist`, `multiplier_ambiguous` or `ticker_reuse_suspected`.
  Planning 205 (protocol section 7.4).

**Derived requests and their hashes.** C1 to C3 are the static list in `contract.json`
(`static_requests`, with the first-page URLs and parameters). Every later class is a
deterministic function of earlier receipts (the rules above). Before each phase runs,
the run writes `plans/<phase>.json` with the number of requests and the SHA-256 of their
sorted keys, so the exact derived list is fixed and auditable **before** its first request.
A derived list above its class cap, or a class not in the go, stops the run.

## 3. What is parsed, what is discarded

Every body physically holds prices or rates. The reducers keep only this and **discard the
rest unparsed**. Receipts and reports hold timestamps, existence, sizes, counts and hashes
only.

| Class | Kept                                                                                                                                                                                                | Discarded unparsed                                                                                              |
| ----- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| C1    | `symbol`, `baseCoin`, `status`, `launchTime`, `deliveryTime` (if before the cut), `fundingInterval`, whether `isPreListing` is present, `minNotionalValue`, `qtyStep`, `minOrderQty`, `maxLeverage` | `priceFilter` (every field), `upperFundingRate`, `lowerFundingRate`, the contents of `preListingInfo`, all else |
| C2    | `symbol`, `baseCoin`, `quoteCoin`, `launchTime` if the field exists                                                                                                                                 | all else                                                                                                        |
| C3    | directory names ending `USDT`, and their modification date only compared with 2024-10-01                                                                                                            | the page                                                                                                        |
| D1    | file names `SYMBOLYYYY-MM-DD.csv.gz` dated before the cut: first day, days inside the window, gaps longer than 30 days                                                                              | names from the cut, sizes, all else                                                                             |
| D2h   | status and `Content-Length`                                                                                                                                                                         | nothing else exists (no body)                                                                                   |
| D2    | **column 1 only** (the trade timestamp): minimum, maximum, first, last and the row count                                                                                                            | columns 2 and later of every row (side, size, price, ids, notionals); the file is streamed, never stored        |
| D3    | the bar start time (column 1)                                                                                                                                                                       | open, high, low, close, volume, turnover                                                                        |
| D4    | start times: count, oldest, newest                                                                                                                                                                  | open, high, low, close                                                                                          |
| D5    | start times: count, oldest, newest                                                                                                                                                                  | open, high, low, close, volume, turnover                                                                        |
| D6    | `fundingRateTimestamp`: row count, oldest, newest per page (page boundaries)                                                                                                                        | `fundingRate` of every row                                                                                      |
| F     | status and `Content-Length`                                                                                                                                                                         | nothing else exists (no body)                                                                                   |

A catalog row launched **before** the window keeps only `symbol`, the base and whether it
was delivered before the window: **no date**. They serve `relist` and `multiplier_ambiguous`
only (question 5). The first response of each JSON class also leaves a **key-only schema
probe** (key paths and value types, never values). Receipts, plans, `phase0.json` and the
catalog snapshot live in the run root **outside Git**. Only the aggregate report is committed
afterwards, and it holds counts, shares and codes, no symbol and no price.

## 4. Phases, the preflight and the fail-closed rules

1. **Phase 0 (atomic):** C1, C2, C3. The root listing is reduced against the whole catalog in
   memory, so the facts of the phase live in one file, `phase0.json`, written once. An
   interrupted phase repeats from its first request as a new round (5 more requests, inside
   the class caps).
2. **Phase 1:** D1 for every in-window launch and every candidate.
3. **Phase 2:** D2h for every launch whose first file needs reading, then the **preflight**:
   the summed `Content-Length` of the files of D2 against the class cap of **6,144 MiB**.
   Above it the run **stops before any file is read** and reports the sum. A file above the
   per-file cap of **64 MiB** is never downloaded and its launch has an unreadable `T0`
   (question 11). Then D2.
4. **Phase 3:** D3, D4, D5, then D6 in rounds (one page per unfinished launch per round).
5. **Phase 4 (optional):** F, `ob500` for entry and exit day, then `ob200` where absent.

Fail closed, as in the catalyst study: the **first request of every class is a schema
probe** and **any** schema mismatch stops the whole run with a fixed reason code and the
key tree, never a value, so one wrong assumption about a page cannot silently produce a
verdict. The HTML and gzip classes (C3, D1, D2) have no key tree; their reducers fail closed
(no anchors, no directories, not gzip, a bad timestamp, a row from the cut). The page formats of `public.bybit.com` and `quote-saver.bycsi.com` are assumptions
until the first probe (the symbol listing pattern is the one an earlier study already used).

**Retries and pacing** (lessons of the catalyst fetch):

- One request at a time, at least **1 s** apart.
- At most **3 attempts** per request. HTTP 429, 500, 502, 503, 504, a transport failure and
  a Bybit `retCode` 10006 are retried. The pause before attempt 2 and 3 is the server's
  `Retry-After` if it is a whole number of seconds up to 300, else **10 s, then 30 s**.
- **After an HTTP 429 the spacing doubles** (1, 2, 4, 8 s, at most 8 s) for the rest of the
  run: the catalyst fetch failed on retries one second apart.
- **HTTP 403 stops the run at once, no retry** (a Bybit IP block must not be probed).
  Any other 4xx, a non-JSON or unexpected body, or a schema mismatch also stops it.
- Redirects are **not followed**: a 3xx stops the run at the first probe of its class.
- A 404 on a listing, a first file or a book file is a **result** (`absent`), not an error.
- After three failed attempts a per-launch request is `unavailable`, which the verdict counts
  as missing evidence (never as covered). A core class of phase 0 that stays unavailable stops
  the run. **C2 (spot) is the only optional series:** if it fails for any reason the spot
  diagnostic is `unavailable` and nothing else changes.
- **Attempt journal.** Every attempt is written to `attempts/` **before** the request, and its
  receipt to `receipts/` after. A crash between the two is charged the full response cap of
  its class. The sequence must be contiguous or the run refuses to start.
- **Resume** continues from the receipts in the same root. The run binding holds the
  `request_list_sha256`, so a changed contract starts a new root. `--prior-root` carries the
  attempts and bytes of earlier roots into the budget; **cross-revision reuse of pages is not
  implemented** (question 15).

## 5. How M1 to M6 and the stop codes are computed (`new_perp_stage1_verdict.py`)

The verdict module reads the receipts of a run and makes no request. Launch statuses are
assigned in one fixed order, each launch gets exactly one, and the counts add up to the
launches: `t0_unavailable`, `t0_ambiguous`, `identity_excluded`, `window_censored`,
`lookback_censored`, `funding_uncovered`, `entry_unavailable`, `primary`.
`n_primary` is the protocol's formula (section 2) with `t0_unavailable` and
`entry_unavailable` made explicit.

| Check | Rule at stage 1                                                                                                                                                                                                                                                                                                                   | Stop code             |
| ----- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------- |
| M1    | `d = first trade - launchTime`: `aligned` -5 min to +60 min, `delayed_open` up to +24 h, else `premarket_or_ambiguous`. Pass: ambiguous at most 10%, readable at least 90%. Lifecycle: launches with no archive or an unreadable first file at most 15%                                                                           | `not_reconstructible` |
| M2    | (i) catalog launches with no archive file within 1 day of `launchTime` at most 5%; (ii) archive-only symbols (first file inside the window, absent from the catalog) at most 5% of the union, **unlisted candidates counted in the worst case**; (iii) V1 launches missing more than 5% of path days at most 5%                   | `incomplete_coverage` |
| M3    | statuses `relist`, `multiplier_ambiguous` (catalog), `ticker_reuse_suspected` (file list), `identity_unverifiable`; contradicting at most 10%, unverifiable at most 20% of the launches that survive M1 and M2, at least 100 episodes remain                                                                                      | `identity_unresolved` |
| M4    | funding covered by the HYP-036 page rule for days 3 to 21 after `T0`: **a full page covers only from its oldest row**, a short page covers from the span start, four full pages without reaching the start are `pagination_exhausted`, no rows or a failed page are uncovered. Share uncovered at most 5%                         | `incomplete_coverage` |
| M5    | at least 95% of the episodes have the entry minute trade bar (else `entry_unavailable`) and hourly trade and mark series that start at or before the entry hour and reach the hour before `X`. **End-of-archive rule:** a series that ends early is a gap unless the catalog delivery time is at or before the first missing hour | `incomplete_coverage` |
| M6    | catalog fields only: `minNotionalValue` at most USD 50, `maxLeverage` at least 2, `qtyStep` and `minOrderQty` present. Unknown counts against. At least 90% feasible                                                                                                                                                              | `capital_infeasible`  |
| floor | `n_primary` below 100 or fewer than 26 ISO weeks                                                                                                                                                                                                                                                                                  | `insufficient_events` |

Codes are listed in the order of protocol section 8 (rows 1 to 5). A check whose requests
were not all run is **`not_assessed`**, never a failure: the outcome is the first stop code
of the assessed checks, else `stage1_incomplete` if something is `not_assessed`, else
`stage1_pass`. Reasons, not bare zeros: the report holds the M1 classes, the M2 shares and
counts, the M3 statuses, the funding gaps by launch month and by status (delisted or not),
the M5 states, the M6 counts and the book coverage by label. The book coverage and the spot
diagnostic **gate nothing**, and `book_source_confirmed` is `false` until the owner decides
item 7, so scenario L counts book coverage as zero (protocol section 4).

## 6. Which reads to split, and what I recommend

The go in section 0 is **one go for classes C1 to D6, with F optional**. Recommendation:

- **Keep D3 to D6 (entry bar, hourly existence, funding) inside this go.** They read
  timestamps and page boundaries only (everything else is discarded unparsed), they are the
  whole of M4 and M5, and without them stage 1 cannot conclude. Splitting them out would
  leave a stage that can only fail, never pass.
- **D2 (the first daily files) is the one step that moves gigabytes.** It is protected by the
  automatic preflight, so one go is safe: the run stops by itself if the listed sizes exceed
  the cap. If the owner prefers a manual gate, the contract can be struck to C1 to D2h
  first (D2 and later `in_go: false`, a new hash) and widened after the sizes are read.
- **Strike F if in doubt.** It is HEAD only, no body and no volume, and it gates nothing. It
  only fills the book coverage table that scenario L needs and it does not decide the
  official-source question of item 7.
- **Not in this go, deliberately:** minute-level existence of the whole path (about 14,700
  requests for the days 3 to 21 union, about 5,800 for the V1 path, and every page full of
  prices). The protocol's gap rule is page level ("a minute is missing only when its source,
  file, page or series, is absent"), which the entry minute bar plus the hourly series
  prove at the resolution that matters; stage 4 fetches these bars anyway and re-checks
  them there (question 2).

## 7. Interpretations made, and limits stated plainly

Choices the protocol leaves open, each listed so the colleague can overrule it:

1. **M2 (iii) uses the V1 path days** (the calendar days from `E` to the last minute of the
   hold), not the union of days 3 to 21. More than 5% of 7 or 8 days means one missing day.
2. **M2 (ii) union** is the catalog launches in the window plus the archive-only symbols with
   a first file in the window. Catalog symbols launched before the window with a first file
   inside it are not listed (they are not launches) and are not in the union.
3. **`ticker_reuse_suspected`** is a first file more than 60 days before the launch, or a gap of
   more than 30 days between consecutive files that starts before the launch day. The
   first-file case is in practice caught earlier by M1 as `premarket_or_ambiguous`.
4. **`identity_unverifiable`** at stage 1 means no readable listing for the symbol, or a
   catalog `baseCoin` that is missing or differs from the symbol's base. `change_known_at_entry`
   is **not observable** at stage 1: the catalog is a current snapshot and no machine-timed
   announcement feed is read (the catalyst study found none usable for product updates). This
   is the protocol's accepted limit.
5. **A trade-bar page or series with no row is a gap** (`empty`), never a zero-trade minute.
   Inside a returned page nothing is missing (protocol 5.3 item 3).
6. **M6 is price-free.** The price-dependent part (a quantity step or minimum quantity against
   USD 50 at the entry price) needs a price and moves to stages 3 and 4. The catalog is a
   current snapshot, not the contract of the launch day. The risk-limit endpoint is not
   requested (question 6).
7. **The first minute with volume is not read**, only the first public trade (the M1 classes
   are defined on it), which avoids reading volumes (question 7).
8. **No entry filter and no price in selection.** Every status above is decided from dates,
   catalog fields and the **existence** of data.
9. Planning numbers (263 launches, about 120 archive-only candidates, 205 entry candidates)
   are guesses. The caps are the contract; the counts only tell the owner what to expect.

## 8. Run procedure (after the go, not now)

```bash
uv run python -m schurfer_analytics.new_perp_stage1 fetch --root <absolute durable dir>
uv run python -m schurfer_analytics.new_perp_stage1_verdict --root <same dir> --out <report.json>
```

`fetch` refuses a dirty tree, a revision not published in `origin/main`, a contract file that
differs from the code, or a registry without this study's row. The registry row, this document,
the contract JSON and the code must be in `main` before the first request. The report is then
committed with the run's `request_list_sha256` and the code revision. No holdout, test-year or
blind-window input is requested anywhere.

## 9. Open questions (each with my recommendation)

1. **One go or two?** One go for C1 to D6, F optional, with the automatic preflight before D2.
   Alternative: stage 1-A (C1 to D2h and D2 after the sizes are read) and stage 1-B (D3 to D6).
   _Recommend one go._ Funding, mark-price and bar reads here keep to timestamps and page
   boundaries and are what M4 and M5 are.
2. **Minute-level path existence** (about 5,800 requests for V1, 14,700 for the union): not in
   the go. _Recommend not at stage 1._ The hourly series plus the entry bar prove existence at
   page resolution, the protocol's own gap rule, and stage 4 re-proves it on the bars it reads.
3. **Book class F in the go?** _Recommend yes, optional and strikeable:_ HEAD only, no volume,
   gates nothing. The official-source confirmation and any volume cap stay item 7.
4. **Archive-only candidate names.** The M2 second share needs a listing of every directory
   absent from the catalog with a modification date from 2024-10-01. Those names go to
   `phase0.json` (outside Git), and may include a symbol launched after the cut that was
   delisted and dropped from the catalog since (its first file date is dropped at the cut,
   nothing else is recorded). _Recommend accept:_ a name carries no date, and the root
   directory count was already read at coverage level (registry, 2026-10-30).
5. **Pre-window catalog rows** (symbol and base, no date, outside Git) are kept for `relist`
   and `multiplier_ambiguous`. Some are holdout-era launches. _Recommend accept:_ no date and
   no count is reported, and without them `relist` cannot be decided.
6. **M6 without a price, and no risk-limit class.** _Recommend accept._ A risk-limit class would
   add about 263 requests, return nothing for closed symbols, and is not expected to bind at
   USD 50 (an assumption the first probe cannot check). It can be added by a new contract.
7. **First bar with volume not read** (M1 method also lists it). _Recommend drop:_ the classes
   are defined on the first public trade, and it avoids reading volumes.
8. **M2 (iii) and the M5 gate use the V1 path**, while D4, D5 and D6 request days 3 to 21 as in
   M4 and M5 of the protocol. _Recommend accept:_ V1 is the only decider, and the extra days
   cost no request.
9. **Ticker-reuse thresholds** (60 days, 30 days) and the subsumption by M1. _Recommend accept._
10. **`identity_unverifiable` and `change_known_at_entry`** as in section 7 item 4. _Recommend
    accept_ as the protocol's stated limit.
11. **Per-file cap of 64 MiB for a first daily file.** A larger file is not downloaded and
    counts as an unreadable `T0`, which counts toward the 15% lifecycle gate. This can bias
    against hyped launches. _Recommend keep 64 MiB;_ the sizes are in `preflight.json` before
    D2, and the owner can raise the cap by a new contract if many files are above it.
12. **Budget:** 2,183 planned requests, 3,200 attempts and 6.5 GiB hard caps, against the
    protocol's planned 1,000. _Recommend accept;_ the largest levers are D1 (archive-only
    candidates) and F.
13. **Spot catalog (C2)** is one diagnostic request, and the spot `launchTime` may not exist.
    _Recommend keep._ Spot archive listings are not requested.
14. **Pacing:** 1 s spacing, doubled after a 429, with 403 as a stop. _Recommend accept._
15. **No cross-revision reuse of pages.** A rerun after a code fix repeats finished requests
    under the cumulative `--prior-root` budget, and the expensive D2 files are resumed only in
    the same root. _Recommend accept for now;_ add the catalyst-style reuse only if a stop
    happens.

## 10. Verification performed

- Unit and integration tests with a mock Bybit (no network): the cut in every reducer
  (with-and-without comparisons), the first-file reader on a gzip stream split anywhere with
  garbage in columns 2 and later, the funding page rule, the identity statuses, the planner
  caps, retry, backoff, 429 slowdown, 403 stop, schema stop, resume and interrupted phase 0,
  the preflight and per-file cap, a **full run into a temp root that is then scanned for
  sentinels**, and every M1 to M6 verdict and the stop-code order from receipts a run
  really wrote.
- The checked-in contract equals `build_contract()` and this document carries its hash.
- Ruff, mypy (strict) and the full `make verify` gate before the push. **Nothing was fetched,
  no network request was made, and no data was read.**
