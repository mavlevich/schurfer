# I25 Stage 1b: body-phase contract (v1, DRAFT)

**Not authorized to fetch.** This is the design, the frozen contract and the code for the body
phase of the I25 Stage 1b check on the sealed backward holdout `[2023-02-01, 2024-09-16)`.
Merging it grants no request. The owner decides each of the two gos below separately, by their
exact hashes. No request was made, no sealed file was read and no holdout data was seen while
writing it: the tests use a synthetic sealed root and a mock server only. The protocol is
[new-perp-short-lab-protocol-v1.md](new-perp-short-lab-protocol-v1.md) (sections 4, 5, 8 to 11 and
Proposed Amendment 6, added by this change).

- Frozen contract: [`contract.json`](evidence/new-perp-stage1b-body-v1/contract.json).
- Code: `apps/analytics/schurfer_analytics/new_perp_stage1b_body.py` (pins, planner, reducers,
  fetcher, runner) and `new_perp_stage1b_body_verdict.py` (M1 to M6, stop codes, from receipts).
  Discovery and metadata code are imported, never edited.
- Registry: section "I25 Stage 1b body registration" of the
  [seen-windows registry](seen-windows-registry.md).
- Depends on the Stage 1b metadata result record of 2026-11-20 (a separate documentation change)
  for the pins in section 1. **That record must be merged first.**

## 0. What the owner is asked to approve, later and separately

| Go  | Name | Reads                                                                    | Class | Requests (max) | Attempt cap | Byte cap                  |
| --- | ---- | ------------------------------------------------------------------------ | ----- | -------------- | ----------- | ------------------------- |
| 1   | A    | the first daily file of each launch with a sealed HEAD size, column 1    | D2    | 247            | 494         | 5,185,208,320 (4,945 MiB) |
| 2   | B    | entry bar, hourly mark and trade series, funding pages of the candidates | D3-D6 | 1,729          | 2,162       | 670,040,064 (639 MiB)     |

```text
request_list_sha256 (whole contract, bound into every run) =
  4fa65c35b75b587d55b520833c7c0e84c404c2eadcb56040c3d063f7ab9c4bf2
go A approval hash (go_sha256["1"]) =
  1811a7559ec6e1be0aa40821eb6cd586145c0b0343a2d1a2621e6e6363d7af69
go B approval hash (go_sha256["2"]) =
  b9bc4e6ab07a0964dbbd4b1955ccfd6a222562748a51855097035b8bfbcc77f5
```

Go B is approved by its own hash **and** by the SHA-256 of `queue_b.json`, which Go A writes. The
code refuses Go B unless Go A finished in the same root, the queue hash matches, Go A reported
no stop code, and the queue derived again from the Go A receipts equals the approved one. A stop
code from Go A (for example M1 failing) ends the stage and Go B is never asked for. The gate
exists because the Go B queue depends on the first-trade times that only Go A reveals.

Check without a network request:

```bash
uv run python -m schurfer_analytics.new_perp_stage1b_body plan
uv run python -m schurfer_analytics.new_perp_stage1b_body write-contract --check
```

## 1. What the body phase binds to

The runner reads one finished sealed metadata root. Before any request it verifies that root
against public facts that are hard-coded in the contract (`pins`). All of them come from the
metadata result record and the metadata contract; none is read from a sealed file by the author.

| Pin                                | Value                                                              |
| ---------------------------------- | ------------------------------------------------------------------ |
| metadata request-list SHA-256      | `31ae1a57970e7af98d2e52fd75534cf2604cd4a79bb889f25cd2f6a90fbada96` |
| metadata report file SHA-256       | `173347ae6785dd2fe5a5bbbce7e6fdfc10b82db37d98908e7ba0fc6f64458352` |
| metadata manifest SHA-256          | `f3098e0840be489df640fbdab66c464257bac3d6aa5315d58e70d86d594d1341` |
| launches in the sealed catalog (N) | 254                                                                |
| first in-window daily files, HEAD  | 247, all with a known size                                         |
| bytes of those files (T)           | 2,323,690,559 (2.164 GiB)                                          |

The runner refuses to start unless the report and manifest hashes match, the report says
`metadata_complete_coverage_pending` with `complete`, `binding_valid` and no stop and no known
failure, the manifest and every JSON file of the root match `evidence-hashes.json` exactly (no
extra, no missing file), `metadata_done.json` exists and `stop.json` does not, and the rows give
exactly 254 launches, 247 first files and T bytes. It re-verifies the same facts after the last
request. The output root is a new directory, disjoint from the metadata root and from every
earlier sealed root. Nothing of discovery's 8.5 GiB or the metadata allowance is reused.

## 2. Window and cut (stated once)

- Window: start `2023-02-01T00:00:00Z`, cut `2024-09-16T00:00:00Z` (the protocol's 2024-09-15 end,
  exclusive form). Nothing from the cut on is requested, recorded or counted.
- `X = E + 7 d` is the end of the exit bar. A path is `window_censored` only if `X > cut`;
  `X == cut` is a candidate. A path that crosses the cut gets **no bar and no funding request**.
- `lookback_censored` if `E - 30 d < window start`, so `T0 >= 2023-02-24T00:00:00Z` (up to the
  minute rounding) is the earliest candidate. Such a launch is still read in Go A and counted in M1.
- Every API span ends at `cut - 1 ms` and starts no earlier than the window start. A request that
  would break this cannot be built: the request class validates URL, parameters, window and meta
  on construction.
- A row from the cut on is dropped uncounted from an API page (this is the one fixed policy for
  such rows). In a first file such a row, or a row before the window start, stops the run with a
  fixed reason and is never recorded.
- 2024-09-16 to 2024-09-30, the discovery year, the HYP-031 test year and the HYP-012 v2 blind
  window are not touched. No data from 2025-10-01 or 2026-09-29 is requested.

## 3. Go A: the initial body queue

**Queue.** One GET per sealed launch that has a `first_file_day` (a first daily file inside the
window and within one day of `launchTime`) and a sealed HEAD result `ok` with an integer size:
`public.bybit.com/trading/{SYMBOL}/{SYMBOL}{first_file_day}.csv.gz`. The 247 files are sorted by
the opaque slot of the launch (a random 256-bit token from the metadata run), so the order says
nothing about ticker or launch time. The other 7 launches of the 254 (the public record: 247 files plus 7) are already
`premarket_or_ambiguous` by file dates and get no request.

**Preflight, no network.** Count, bytes and cap must match the pins: 247 files, T bytes, no file
without a size, no file above the per-file cap of 512 MiB (536,870,912 bytes, the discovery cap).
A file above the cap is not skipped and not counted apart: the run stops before the first request
(`preflight_failed`), because the cap and the budget below are contract numbers and the cohort
would otherwise change by size. Any mismatch stops the same way.

**Parse.** The gzip stream is read in memory in 64 KiB chunks. Only column 1 of each row (the
trade timestamp) is parsed, in nanoseconds, from up to nine fractional digits; columns 2 and
later (side, size, price, ids) are cut at the first comma and never read. The file must be
**exactly one gzip member with no trailing byte**: a second member or any byte after the first
ends the run with the fixed reason `gzip_trailing_data`, because the minimum over the whole file
would otherwise be unproven (open question 13). The facts kept per file
are the row count, the minimum and the maximum timestamp. The file is never stored. `T0` is the
**minimum**, not the first row.

**Why nanoseconds.** `E` rounds `T0 + 7 d` up to the next whole minute, unchanged on a minute.
Truncating `T0` to milliseconds first would put a trade 0.4 ms after a minute boundary on that
boundary and move `E` one minute early. Discovery truncated to milliseconds; the effect is about
one launch in a million there. The holdout keeps the exact value, and a test pins the case.

**Budget, recomputed from the formulas.** Every constant is computed in code, never typed twice.

| Quantity                                             | Formula                      | Value                                   |
| ---------------------------------------------------- | ---------------------------- | --------------------------------------- |
| planned bytes (T)                                    | sum of 247 sealed sizes      | 2,323,690,559                           |
| two full passes of every file (the retry headroom)   | 2 T                          | 4,647,381,118                           |
| last request: per-file cap plus 64 KiB read reserve  | 536,870,912 + 65,536         | 536,936,448                             |
| needed                                               | 2 T + cap + reserve          | 5,184,317,566                           |
| **class and go byte cap, rounded up to a whole MiB** | ceil(needed / MiB) x MiB     | **5,185,208,320** (4,945 MiB, 4.83 GiB) |
| attempt cap                                          | 2 x 247 (one retry for each) | **494**                                 |

A request starts only if `bytes used + 536,870,912 + 65,536` fits the go budget and the class
budget (the discovery rule, unchanged); a declared length above the cap reads nothing; every
received byte is accounted; `Accept-Encoding: identity`. The bound does not need per-file sizes:
whatever the order, the bytes already used before the last request are at most 2 T plus failed
reserves, so the cap covers one full pass plus one full second pass of every file plus the
reserve. **Worst case, written down:** three full sends of every file is 3 T = 6,971,071,677
bytes (6.49 GiB) and 741 sends. It is not covered; such a run stops cleanly on the byte or
attempt cap and never overshoots. A send whose process died before its receipt is charged its
whole cap plus reserve (512 MiB), which the headroom of about T absorbs.

**Time at one request per second.** 247 requests are at least 247 s of pacing; at the throughput
measured in discovery (6.28 GiB in 7.9 minutes) the transfer of T is about 2.7 minutes, so
about 7 minutes in all. At the attempt cap, 494 s of pacing plus about 6 minutes of transfer
is about 14 minutes. After an HTTP 429 the spacing doubles up to 8 s and the slowdown persists
across a resume: 494 sends at 8 s is the extreme, about 66 minutes.

## 4. The T0 rule and the derived queue (Go B)

Per launch, in this order, from the sealed metadata state and the Go A receipt. The statuses are
exclusive and their counts add up to N; `n_primary` is protocol section 2 with the two data
availability reasons explicit.

| Order | Status              | Condition                                                                                                                                                       | Requests after Go A |
| ----- | ------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------- |
| 1     | `t0_unavailable`    | no archive, no pre-cut file, pre-window first file, unverified filename identity, no listing, a first file that is absent, unavailable, too large or not queued | none                |
| 2     | `t0_ambiguous`      | metadata already proved `premarket_or_ambiguous`, or the file's minimum timestamp is on another UTC day than the file name, or `d` is outside `[-5 min, +24 h]` | none                |
| 3     | `identity_excluded` | catalog says `relist` or `multiplier_ambiguous`, or `ticker_reuse_suspected`                                                                                    | none                |
| 4     | `window_censored`   | `X > cut`                                                                                                                                                       | none                |
| 5     | `lookback_censored` | `E - 30 d < window start`                                                                                                                                       | none                |
| 6     | `funding_uncovered` | M4 finds the funding coarse check failed                                                                                                                        | decided by Go B     |
| 7     | `entry_unavailable` | no 1-minute trade bar starts at `E`                                                                                                                             | decided by Go B     |
| 8     | `primary`           | none of the above                                                                                                                                               | counted             |

`d = T0 - launchTime`: `aligned` is `-5 min <= d <= +60 min`, `delayed_open` is up to `+24 h`,
anything else is `premarket_or_ambiguous` (the protocol's M1 classes, compared in nanoseconds).
An ambiguous `T0` is never re-anchored to anything and never gets a bar, mark or funding request.
A launch that passes statuses 1 to 5 is an **entry candidate**. Only candidates get Go B
requests, sorted by slot, with `E`, `X` and the span derived from `T0` alone (no price enters):

| Class | Endpoint                                   | Parameters                                                                                                                     | Per candidate |
| ----- | ------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------ | ------------- |
| D3    | `api.bybit.com/v5/market/kline`            | `category=linear`, `interval=1`, `start=end=E`, `limit=1`                                                                      | 1             |
| D4    | `api.bybit.com/v5/market/mark-price-kline` | `interval=60`, `start=max(window start, T0+3d)`, `end=min(T0+21d, cut-1 ms)`, `limit=1000`                                     | 1             |
| D5    | `api.bybit.com/v5/market/kline`            | the same span as D4, trade bars                                                                                                | 1             |
| D6    | `api.bybit.com/v5/market/funding/history`  | `startTime` as D4, `endTime` first as `end`, then one millisecond before the oldest row of the previous full page, `limit=200` | 1 to 4 pages  |

**Funding pagination (the HYP-036 rule, unchanged from discovery).** A short page covers from the
span start; a full page covers only from its oldest row; a next page is requested only while the
coverage is undecided; four full pages without reaching the start are `pagination_exhausted`.
`queue_b.json` holds the exact first-round list (D3, D4, D5 and the first D6 page of every
candidate) as a hash, plus counts. Later D6 pages cannot be listed before the responses exist;
they are bounded by four per candidate and by the Go B ceilings.

**Response bounds, checked per row before any other field.** The reducers receive the exact bounds
of their own request. A bar start must lie on the step grid (hour for D4 and D5, minute for D3),
inside `[the hour or minute that contains the requested start, the requested end]`, and appear
once; a funding stamp must lie inside `[startTime, endTime]` and appear once. A row from the cut
is dropped uncounted (section 2). Anything else (`bar_off_grid`, `bar_out_of_range`,
`bar_duplicate`, `funding_out_of_range`, `funding_duplicate`) is a fixed schema stop, so a stale,
shifted or duplicated series can never look covered.

**Parse.** D3, D4, D5 keep the bar start times only (count, oldest, newest, runs of missing
hours, at most 20 runs, and whether the page may be cut by its limit); open, high, low, close,
volume and turnover are discarded unparsed. D6 keeps `fundingRateTimestamp` only (count, oldest,
newest, largest gap, full or not); `fundingRate` is discarded unparsed.

## 5. Go B ceilings, recomputed

The worst case uses the largest possible candidate count, one per sealed first file (247). The
class cap is a send count at the response cap plus the 64 KiB reserve, rounded up to a whole MiB:
it binds only if a class sends more than 125% of its requests with every response at the response
cap, which real responses (tens of KiB) never approach. The go byte cap is the sum of the class
caps.

| Class    | Max requests | Sends (125%) | Per response | Class byte cap formula     | Class byte cap            |
| -------- | ------------ | ------------ | ------------ | -------------------------- | ------------------------- |
| D3       | 247          | 309          | 64 KiB       | 309 x 65,536 + 65,536      | 20 MiB                    |
| D4       | 247          | 309          | 512 KiB      | 309 x 524,288 + 65,536     | 155 MiB                   |
| D5       | 247          | 309          | 512 KiB      | 309 x 524,288 + 65,536     | 155 MiB                   |
| D6       | 988          | 1,235        | 256 KiB      | 1,235 x 262,144 + 65,536   | 309 MiB                   |
| **Go B** | 1,729        | **2,162**    |              | sum of the four class caps | **639 MiB = 670,040,064** |

The "Requests (max)" of Go B is the sum of the class maxima: 247 + 247 + 247 + 988 = 1,729 first
attempts. The attempt cap 2,162 is the sum of the sends: 309 + 309 + 309 + 1,235. Retry headroom
is stated once: API classes get +25% sends over their worst-case first attempts, because the
repo's own note records about 1% transient errors on public kline requests; the first-file class
gets a full second pass, because its cost is bytes. Per-response caps are the discovery values.

**Expected, not a ceiling.** Go B needs `3 c + p` requests for `c` candidates and `p` funding
pages, with `c <= p <= 4 c`. Discovery measured `p = 1.05 c`. At `c = 247` that is about 1,000
requests; the extremes are `4 c = 988` and `7 c = 1,729`, and 2,162 attempts is the cap. At one
request per second Go B takes about 17 minutes expected (16.5 to 29 minutes at the extremes), 36
minutes at the attempt cap, and 4.8 hours if every attempt were throttled to 8 s spacing.

**Both gos together.** Attempts 494 + 2,162 = 2,656. Bytes 5,185,208,320 + 670,040,064 =
5,855,248,384 (5,584 MiB, 5.45 GiB). First attempts 247 + 1,729 = 1,976. These are ceilings; the
expected transfer is T plus tens of MiB.

## 6. Transport and retry rules (discovery's, restated by reference only)

The fetcher is discovery's (`new_perp_stage1.Fetcher`), subclassed for two reasons only: the
first-file reducer enforces the holdout window in nanoseconds, and no receipt keeps a raw-body
hash. Everything else is inherited and is not redefined here: one request at a time at least 1 s
apart, spacing doubled after HTTP 429 or a rate-limit code (cap 8 s, persisted across a resume);
at most 3 sends per request counted from the attempt journal; `Retry-After` honoured up to 300 s,
else 10 s then 30 s; HTTP 429, 500, 502, 503, 504 and transport failures retried; **HTTP 403 stops
the run at once**; any other 4xx, a redirect, a non-JSON or unexpected body, or a schema mismatch
stops it with a fixed reason; a 404 on a first file is a result (`absent`), and after three failed
sends a request is `unavailable`, which is missing evidence and never covered.

**Bybit error envelope.** An HTTP 200 body with `retCode` other than 0 keeps **only the integer
`retCode` and a `retMsg` of at most 200 characters**. `retCode` must be an `int` and not a `bool`
or float, checked before any success or retry classification. Only the table verified against the
Bybit V5 error page is retried: 10006 and 10018 (rate limit, each also doubles the spacing) and
10000 and 10016 (transient). Every other code, including 10019, is fatal and the stop names it;
an unknown code is never guessed. A wrong envelope shape is a schema mismatch.

**Privacy of the sealed root.** Requests carry the symbol only in the URL or parameters. Their key
is a digest of the class, the opaque slot, the page and the non-symbol parameters, never of a
symbol; receipts and attempts hold `symbol: ""`, the slot and the page, no URL, no raw-body hash.
HTTP library logging is disabled during every run and restored after. An unexpected exception is
sealed as `unexpected_error` and re-raised without its text, so no name reaches a console.

## 7. Verdicts, from receipts only

The verdict module reads the sealed metadata rows and the receipts and makes no request. Thresholds
are the protocol's, identical to discovery's, and are part of the hashed contract; **no holdout
fact tunes any of them.** "Candidates" and "episodes after funding" are as in section 4.

- **M1** over all N launches. Pass: `premarket_or_ambiguous` at most 10%, readable (`aligned` plus
  `delayed_open`) at least 90%, unreadable (every `t0_unavailable` reason) at most 15%. Pending
  first files can only raise the readable share, so a failure is known while files are pending.
  M1 compares the first public trade with `launchTime`; a bar at `E` is not the first nonzero
  volume bar and does not date the regular launch.
- **M2** (i) no file within one day of launch, at most 5% of N, from the sealed facts; (ii) the
  archive-only share `a / (N + a)` of the metadata report, at most 5%, an observed pre-cut file
  series and never proven; (iii) candidates whose listing misses more than 5% of their V1 path
  days (calendar days from `E` to `X - 1 ms`), at most 5% of the candidates. An unproven catalog
  folder or an unavailable share is not assessed, never a pass.
- **M3** over survivors (readable launches with a file near launch): `relist`,
  `multiplier_ambiguous` and `ticker_reuse_suspected` together at most 10%, unverifiable (no
  listing, or unverified filename identity; the base coin is not retained) at most 20%, survivors
  minus contradicting at least 100. The current catalog does not prove identity known at `E`, and
  `change_known_at_entry` is not observable.
- **M4** coarse funding check over the candidates: pagination finished and the timestamps form
  one chain with no step above 8 hours, start at most one step after the span start and end at
  most one step before its end unless the catalog delivered the symbol. Status `partial_pass`,
  never proof: it cannot show a missing settlement of a 1 h or 4 h schedule, and the catalog
  `fundingInterval` is a current snapshot. Uncovered share at most 5% of the candidates, a
  denominator fixed after Go A: a share above the limit is a **known failure even while launches
  are pending or a later stop occurs**, and a pass needs a complete assessment. Unknown funding is
  never taken as zero. A diagnostic counts uncovered launches by launch quarter and by whether the
  symbol was delivered inside the window.
- **M5** over episodes after funding: the entry trade bar at `E` exists, the hourly trade and mark
  series are continuous from the hour of `E` to the hour before `X`, at least 95%. A missing last
  file is a gap unless a catalog delivery at or before the first missing hour confirms an end. A
  page that may be cut by its limit, or whose gap list was truncated, is `unproven` and never
  green. Minute continuity over the whole path stays deferred to stage 4 (Amendment 1). A known
  failure is the optimistic share below 95%: an episode that is pending, unproven or still waiting
  for its funding decision counts as a success, an episode with a decided gap counts as bad.
- **M6** static catalog fields over episodes after funding: `minNotionalValue` at most USD 50,
  `maxLeverage` at least 2, `qtyStep` and `minOrderQty` present, at least 90%. It can fail an
  order and never shows that one opens: no price, rounding, risk limit or depth is checked. An
  episode still waiting for its funding decision may leave the denominator, so M6 is provisional
  while any is pending. Its failure is known only as an optimistic bound: every pending episode
  that is not feasible leaves, every other episode stays, and the share is still below 90%.
- **Floor:** `n_primary` at least 100 and at least 26 ISO weeks of primary launches. Go A checks
  the same floor on the candidates it knows, as an upper bound.
- **Stop codes** in protocol section 8 order: `not_reconstructible` (M1), `incomplete_coverage`
  (M2, M4 or M5 known failure), `identity_unresolved` (M3), `insufficient_events` (floor),
  `capital_infeasible` (M6). Only known failures count, and **a known failure is never erased by a
  later stop or by missing evidence.**
- **Outcome** is the first stop code, else `stage1b_stopped` if the root is sealed, else
  `stage1b_incomplete` if any check is not assessed, else `stage1b_a_complete` after Go A,
  else **`stage1b_pass_partial`**. After Go A only M1, M2, M3 and the floor upper bound are
  decided, and they are definitive: a stop code among them ends the stage. M6 after Go A is
  provisional and decides nothing; M4, M5 and `n_primary` need Go B.

## 8. What the result may and may not say

May: aggregate counts of launches by status, M1 classes, M2, M3, M4, M5 and M6 counts and
shares, stop codes, request counts, bytes, the hashes of the run, and the partial-check list.
`stage1b_pass_partial` means only that the contracted coverage checks found the registered holdout
recoverable enough for a future frozen exam. It is **not** an economic result, not power, not a
mean, not a trade signal and not a permission to trade.

May not: any price, rate, volume, return or order-book fact; any per-launch row, date, slot,
ticker or ranking; a month-level or launch-level performance proxy. The M4 diagnostic is by
launch quarter (the protocol said month; Amendment 6 changes it for the holdout, question 6).
**No holdout fact may tune a Stage 3 parameter, an entry rule, a threshold or a candidate list.**
The holdout stays one shared sealed exam batch: this phase reads coverage, never an outcome.

## 9. Stops, state and restart

- A stop (HTTP 403, a schema mismatch, a cap, a failed preflight, a changed queue, an unexpected
  error, a source-integrity failure after the last request) writes `stop.json` and seals the
  root. A new fetcher on it refuses to start; no verdict of a sealed root is a pass.
- A stop survives a crash between the fatal receipt and `stop.json`: a recovering fetcher writes
  the missing stop and refuses.
- A hard crash without a stop resumes only in the same root, under the same full binding (the
  request-list hash, the clean revision and the hashes of every published file). A parser fix or
  any other change starts a new root. A finished Go A or Go B cannot be run again.
- After a stop the only way on is a new root, a corrected revision and a new owner decision.
  Spent attempts and bytes are never refunded or carried to another root.
- **Nothing positive is published before the last check.** After the last response, and before
  any queue, report or done marker, the runner verifies again the sealed metadata root (every
  hash and every row), the clean revision and the publication of every bound file. Any exception
  writes the fixed stop `source_integrity_error`, seals the root and publishes only a stopped
  report. The `report` command prints a stored report only if its go finished, or if a stop sealed
  the root and the report is not positive.
- The output root must be outside the Git tree and disjoint from the sealed metadata root. This is
  checked before any file or HTTP client exists.
- The CLI runs only a clean, merged revision whose published files equal `origin/main`, whose
  checked-in contract equals `build_contract()`, and whose registry names this study.

## 10. Registry row and ledger text

Registry (added by this change; the row reads nothing): level **coverage**, window **backward
holdout** `[2023-02-01, 2024-09-16)`, read: the first daily trade file per launch (column 1
timestamps), the entry bar, hourly mark and trade bar start times, and funding timestamps of the
candidates; not read: any price, rate, volume, return, order book, announcement, spot catalog, the
discovery year, the test year and the blind window. Ceilings in section 0.

Ledger text (for the discovery ledger, after a result exists; this change adds no row): "I25
Stage 1b body phase, backward holdout, coverage level. Two gos on `request_list_sha256`
`4fa65c35...4bf2`. Counts, shares and stop codes only; no price, rate, volume or return read; the
holdout remains one shared sealed exam batch; +0 tested variants, +0 parameters." The ledger row
is written only with the result record.

## 11. Run procedure (a template, not permission)

```bash
PYTHONPATH=apps/analytics python -m schurfer_analytics.new_perp_stage1b_body fetch --go 1 \
  --approved-sha256 <go A hash> --metadata-root /ABS/SEALED/i25-stage1b-metadata-v2-e01d67c \
  --root /ABS/NEW-PRIVATE-BODY-ROOT
PYTHONPATH=apps/analytics python -m schurfer_analytics.new_perp_stage1b_body report \
  --root /ABS/NEW-PRIVATE-BODY-ROOT --phase a
# the owner reviews the aggregate Go A report and queue_b.json, then on a second go:
PYTHONPATH=apps/analytics python -m schurfer_analytics.new_perp_stage1b_body fetch --go 2 \
  --approved-sha256 <go B hash> --queue-sha256 <sha256 of queue_b.json> \
  --metadata-root /ABS/SEALED/i25-stage1b-metadata-v2-e01d67c --root /ABS/NEW-PRIVATE-BODY-ROOT
PYTHONPATH=apps/analytics python -m schurfer_analytics.new_perp_stage1b_body report \
  --root /ABS/NEW-PRIVATE-BODY-ROOT --phase final
```

Each `fetch` refuses an approved hash that is not this contract's hash for that go, a dirty tree,
a revision not in `origin/main`, files that differ from `origin/main`, a contract file that differs
from the code, a metadata root that differs from the pins, and an output root that overlaps the
metadata root. No AWS, no live capture, no order path, no discovery-year, test-year or
blind-window input exists anywhere in it.

## 12. Open questions, each with my recommendation

1. **One go or two?** Two: Go A reads the 2.16 GiB of first files and decides M1, M2, M3 and the
   floor bound; Go B is asked for only if none of them stops the stage, on an exact queue hash.
   One go would spend about 1,000 API reads after a failed M1. _Recommend two._
2. **Per-file cap.** 512 MiB with a stop before the first request if any sealed file exceeds it,
   against counting over-cap launches apart as discovery did. The 2.16 GiB total makes a file
   above 512 MiB (22% of it) very unlikely. _Recommend the stop; raise the cap in a new contract
   if it ever fires._
3. **Go A byte cap.** Two full passes plus the reserve (4.83 GiB), against one pass plus reserve
   (about 2.7 GiB) or the full three-send worst case (6.5 GiB). _Recommend two passes._
4. **API retry headroom.** +25% sends per class. _Recommend keep_ (observed transient rate about 1%).
5. **T0 in nanoseconds** instead of discovery's millisecond truncation. _Recommend accept._
6. **M4 diagnostic by launch quarter, not month.** Thirteen launches a month make a month cell
   close to a single launch; a quarter is not. _Recommend quarter._
7. **M3 is partial only.** The current catalog and file lists cannot prove historical identity
   known at `E`: the absence of a contradiction is not proof. "Unverifiable" has no base-coin
   check because the metadata rows do not retain `baseCoin`. _Recommend accept, labelled in the
   partial-check list._
8. **A missing hourly bar is a gap.** An absent bar is a gap. The code reads no volume: a present
   bar is present whatever its volume, and zero volume alone is not a gap (a test with an explicit
   `"volume": "0"` pins this; discovery question 1). _Recommend accept._
9. **The 8 h funding step** stays a coarse partial check (discovery question 2); stage 4 fetches
   the exact rows. _Recommend accept._
10. **Go B refused after any definitive Go A stop code.** _Recommend yes_: it spends no read on a
    failed gate. The alternative (run B anyway for diagnostics) spends holdout reads for nothing.
11. **Sealed output.** Public handoff is the aggregate report only; the root, slots, receipts and
    queue stay outside Git. _Recommend as written._
12. **Merge order.** This change needs the Stage 1b metadata result record merged first; both
    append to the seen-windows registry, so the later one needs `git merge origin/main`.
    _Recommend merge the result record first._
13. **Exactly one gzip member per first file.** A second member or a trailing byte stops the run;
    a legitimate multi-member archive file would stop it too (fail closed, no new request is
    wasted). _Recommend keep; relax only in a new contract after seeing why._

## 13. Verification performed

Synthetic tests only (a synthetic sealed root written by the real metadata functions, and a mock
Bybit with sentinel prices and rates): the budget numbers recomputed from the formulas, the pins,
the thresholds, the clock in nanoseconds, the window enforced in every reducer and request, the
sealed-input checks, the preflight, Go A and Go B end to end, error envelopes, every stop and the
sealed-root rule, resume under one binding only, sentinel and name scans of the root and of the
public report, and each threshold at its exact boundary. **No network request was made, no sealed
file was read and no holdout data was seen.** `make verify` results are in the pull request.
