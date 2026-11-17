# Bybit against Hyperliquid cost-only v1 (contract draft, 2026-11-17)

Status: **draft for the colleague's one design review. It authorizes no listing, no
request and no download.** Every network step below needs the owner's separate go, and
the Hyperliquid steps also need AWS credentials that the owner has not provided and this
contract does not assume (section 3). The registry row ships with this draft. The code goes to one
review after this design is settled.

Study id: `bybit-hl-cost-only-v1`. No HYP id is assigned. One is assigned only if a
two-leg laboratory protocol follows (section 12).

This is Stage 2A of the [consilium plan](../consilium/plan.md), written from the
Stage 2A frame in [`screens/b-2026-11-15.md`](../consilium/screens/b-2026-11-15.md)
section 3 and the paper screens in [`screens/a-2026-11-15.md`](../consilium/screens/a-2026-11-15.md).
It serves ideas I14 and I15 (Bybit against Hyperliquid, two legs) and the surviving cost
question of I21, through **Step B**, and idea I22 (a new Hyperliquid perp), through
**Step A** only.

## 1. Question

1. **Step A, listing only (I22 count and archive coverage for Step B).** Which
   Hyperliquid `l2Book` objects exist in the archive bucket, by date, and how large are
   they? From the object paths alone: how well does the archive cover each window, and on
   what date does each coin first appear (`first_observed_archive_date`)? No object body
   is read, so no price and no order book can enter.
2. **Step B, cost only (I14, I15, I21).** At one fixed instant per date and pair, what is
   the executable non-fee round-trip cost of a Bybit linear perp and of the same asset's
   Hyperliquid perp at USD 50, 250, 500 and 1,000? Does `x_bybit + x_hl` stay at or below
   31.0 bps?

**What 31.0 bps is.** Four taker fills cost 20.0 bps in fees (Bybit 5.5 per side,
Hyperliquid 4.5 per side, base tiers, see the
[shared cost sheet](../consilium/screens/a-2026-11-15.md)). The best gross figure on
record for the two-leg idea is +51.2 bps, selected after seeing results and not a
confirmation. 51.2 minus 20.0 is 31.2, rounded down to 31.0. The number is a voluntary
budget filter on **non-fee** cost. Passing it means only that execution does not rule
the research out. It does not say funding or basis covers the trade, and a pass releases
no return read.

**What this study is not.** It computes no return, no price move between instants, no
funding rate, no basis level and no open interest. It does not model order types,
latency, margin, collateral transfer or the failure of one leg. It does not measure
Hyperliquid fee tiers. It does not measure cost at the minutes where the funding gap of
I14 occurs (section 13, question 6). It is a necessary cost condition, never a signal.

## 2. Windows

Registry levels are those of the [seen-windows registry](seen-windows-registry.md).

| Window            | Dates (UTC)                         | Step A                                    | Step B          |
| ----------------- | ----------------------------------- | ----------------------------------------- | --------------- |
| Discovery year    | 2024-10-01 to 2025-09-30            | listed; per-coin results published        | the only window |
| Backward holdout  | 2023-02-01 to 2024-09-15            | listed; **aggregate counts only**, sealed | not requested   |
| Gap week          | 2024-09-16 to 2024-09-30            | listed as lookback; no per-date output    | not requested   |
| HYP-031 test year | 2025-10-01 to 2026-09-28            | **not requested**                         | not requested   |
| HYP-012 v2 blind  | from 2026-09-29, any venue, any use | **never requested**                       | never requested |
| Before 2023-02-01 | any                                 | not requested                             | not requested   |

Rules, fixed now:

1. **Request-time guard.** The request builder refuses any date outside the window its
   step allows (Step A: 2023-02-01 to 2025-09-30; Step B: 2024-10-01 to 2025-09-30) and
   any date on or after 2026-09-29. The guard is in the code that builds the request,
   not in the report. A refused date is a test failure, not a warning.
2. **No S3 listing crosses a forbidden date.** Step A lists one date prefix at a time, so
   dates after 2025-09-30 are never listed, unlike a paged announcement feed where the
   guard had to drop items after the read.
3. **The holdout is spent by nothing here.** Step A lists its dates to decide whether a
   coin existed before the discovery year, and to count launches for I22. For the holdout
   the report gives only aggregate counts and shares, no list of coins and no calendar
   distribution. The per-coin list is sealed outside Git and only its SHA-256 is
   published, as in the [catalyst contract](catalyst-feasibility-v1.md) section 7.
4. **The test year is not used.** The registry calls it `reserved` ("HYP-031 only"). The
   [program plan](program-plan-2026-11.md) calls it unread and unassigned after track B
   closed. The two statements disagree in wording, and this contract does not need the
   year. If the archive does not cover the discovery year, the outcome is
   `window_extension_needed` (section 4.5), and any other window needs a written amendment
   agreed by A, B and C **before any Step B request**, as the consilium plan requires.
5. **Spread and depth can leak regime information.** The discovery year is already open
   at outcome level (the registry's Bybit and Hyperliquid rows). Step B adds
   **features-level** exposure, cost only, on at most 12 dates inside it. It is
   laboratory data, never a confirmation window.
6. **No dependence on the v2 blind window or on an amendment of rule R1.** Every date
   used is before 2025-10-01.

## 3. Preconditions and what is not assumed

- **AWS access is not assumed.** The Hyperliquid archive bucket is requester-pays:
  every `ListObjectsV2` and `GetObject` call must be signed, and the charges go to the
  signing account. This machine has no `aws` CLI, no `~/.aws` and no `boto3`. Whether
  the owner provides credentials, and under which account and spending cap, is the
  owner's open decision. Until then both steps are design only.
- **No credential reaches the repository or a log.** Credentials are read from the
  process environment of the run only, never from a file in the repository, never printed
  and never stored in `run.json`, the ledger or a receipt. The code refuses to start
  without them (status `credential_missing`) and makes no network call in that case.
  Test: a fake secret inside an injected error text is absent from every written file
  (section 11).
- **Least privilege.** The key the owner creates should allow only `s3:ListBucket` and
  `s3:GetObject` on `hyperliquid-archive`. Step A needs only `ListBucket`. This is a
  recommendation to the owner, not a requirement the code can check.
- **New dependencies are a review point.** `lz4` (frame decoding) and an S3 request
  signer (`boto3`, or a small SigV4 signer on the existing `httpx`) are not in
  `uv.lock`. Adding either changes the lockfile and goes through the code PR's review.
- **Bybit needs no credentials.** Its order book archive is public at
  `quote-saver.bycsi.com`. The 2026-11-13 [B2 scope result](bybit-presettlement-b2-scope-result-2026-11-13.md)
  established that host through the official history catalogue and by HEAD requests.
- **Pinned inputs.** The Bybit-side universe is the instrument catalog of the
  `catalyst-feasibility-v1` run (run binding `c477aebd...`, receipts kept outside Git).
  Its SHA-256 is pinned in the Step A code PR. Without it Step A cannot count pairs and
  I22 launches (section 13, question 9).

## 4. Step A: listing only

### 4.1 Scope and keys

Documented object layout (the code treats it as an expected shape, section 4.5):

```text
s3://hyperliquid-archive/market_data/[date]/[hour]/l2Book/[coin].lz4
```

with `date` as `YYYYMMDD` and `hour` as 0 to 23. Step A uses **only** `ListObjectsV2`
with the requester-pays header. It never calls `GetObject`, never sends a `Range`
header and never lists object keys of any data type except `l2Book`. Pass 0 uses a
delimiter listing that returns prefixes only. The client class has no method
that downloads an object body; a test asserts that (section 11).

### 4.2 Passes

| Pass | What it requests                                                                                                                             | Why                                                                                        |
| ---- | -------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| 0    | One delimiter listing under `market_data/20241001/` (hour prefixes) and one under `.../0/` (data types)                                      | Key-shape probe: confirms hour padding and the `l2Book` prefix. Keys only.                 |
| 1    | For every date 2023-02-01 to 2025-09-30 (973 dates): the prefix `market_data/{date}/0/l2Book/`                                               | Presence and size of each coin's hour 0 object. At most 3 pages per date.                  |
| 2    | For each coin first seen in the discovery year by pass 1: exact-key prefix listings for hours 0 to 23 of the day before and of its first day | Refines `first_observed_archive_date` from a day to an hour. 48 requests per coin at most. |

Pass 1 lists hour 0 only. Step B reads only hour 0 (section 5.4), so hour 0 presence is
exactly the coverage Step B needs. Pass 1 gives a coin's first day with a resolution of
one day, because a coin first listed at 15:00 has no hour 0 object on its first day. Pass
2 closes that gap for the discovery year. For the holdout, pass 2 is not run: a launch
there is dated to its first hour 0 day, which is enough for weekly counts.

Output recorded per listed object: key, size in bytes, last-modified time as returned,
and the listing's request id.

### 4.3 What the report contains

For the discovery year and, as aggregates only, the holdout:

- **Coverage by window.** Dates requested, dates with a **usable** hour 0 listing (at
  least 20 coins), longest run of unusable dates, usable dates per calendar month, median
  keys per date.
- **`first_observed_archive_date` per coin** (day from pass 1, hour from pass 2 for the
  discovery year), with a `reliability` flag (section 4.5, I22).
- **Tier inputs, discovery year only:** per coin, the median size of its hour 0 object
  over usable discovery dates, and the number of those dates.
- **Eligible pair counts.** Coins matched to a live Bybit USDT perp under the identity
  rules of section 5.2, per date and per calendar month, with `identity_ambiguous`
  counted apart.
- **Provenance.** Run binding hash, code revision, input hashes, request and byte
  counters, `listed_at` timestamps. The archive is not immutable (the venue says data is
  uploaded about monthly and "may be missing"), so the result is a dated snapshot, and Step
  B's grid is frozen from that snapshot.

The report shows no price, no order book and no object body.

### 4.4 Budget

| Item                                    | Limit                                                         |
| --------------------------------------- | ------------------------------------------------------------- |
| Attempts, all passes together           | 12,000 (planned first attempts at most 10,210)                |
| Sub-caps on first attempts              | pass 0: 10; pass 1: 3,000; pass 2: 7,200 (150 coins x 48)     |
| Response bytes                          | 256 MiB, counted as in B1 (section 8)                         |
| Request starts                          | 3 per second, one worker                                      |
| Attempts per request                    | 2                                                             |
| Estimated cost (indicative, unverified) | under USD 0.15: 12,000 list requests plus 256 MiB of transfer |

A pass cap that is reached ends the run as `listing_incomplete` with reason
`request_budget`, never as complete. Pass 2 stops closed at 150 coins; a larger set is a
finding that needs an amendment, not a silent truncation.

### 4.5 Status codes and criteria

**Run level** (checked first; any value other than `listing_complete` ends the run and the
report refuses to run, so a partial listing is never read as a complete archive):

| Status                                                                                                 | Meaning                                                                                                           |
| ------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------- |
| `listing_complete`                                                                                     | Every planned request ended terminal and in budget                                                                |
| `credential_missing`                                                                                   | No credentials in the environment; no request was made                                                            |
| `access_denied`                                                                                        | HTTP 403 on a signed request, for example requester-pays not accepted                                             |
| `schema_mismatch`                                                                                      | Keys do not match the expected `market_data/.../l2Book/....lz4` shape, or the pass 0 probe contradicts the layout |
| `listing_incomplete` with reason `request_budget`, `byte_budget`, `page_budget` or `transport_failure` | A cap was reached or a request failed all attempts                                                                |

A stopped run does not resume. A corrected revision starts a new run in a new root and
passes the stopped root with `--prior-root` (section 8).

**Coverage per window** (discovery year only; the holdout gets counts, no verdict):

| Status            | Criterion                                                                                                                                           |
| ----------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| `archive_covered` | usable dates are at least 80% of the window, the longest unusable run is at most 14 days, and at least 11 of 12 months have 15 or more usable dates |
| `archive_partial` | usable dates are at least 30% of the window but the criterion above fails                                                                           |
| `archive_absent`  | usable dates are under 30% of the window                                                                                                            |

**Step A outcome:**

| Outcome                    | Criterion                                                                                                                                |
| -------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| `proceed_to_grid`          | discovery is `archive_covered`; at least 40 distinct eligible bases over the year; at least 10 months can host a grid date (section 5.4) |
| `window_extension_needed`  | discovery is `archive_partial`, or it is covered but the pair or month criteria fail. No Step B. Options go to A, B and C.               |
| `stop_archive_unavailable` | discovery is `archive_absent`. Stage 2A cannot be costed from Hyperliquid history in an allowed window. No extension is chosen here.     |

**I22 launch count (a separate verdict from Step A's outcome).** A candidate launch is a
Hyperliquid coin whose first hour 0 appearance is inside the window, with a Bybit USDT
perp for the same base that launched at least 24 hours before it and was not delivered
(the live rule of the catalyst contract, section 5). Candidates are an **upper bound**:
`first_observed_archive_date` is not the Hyperliquid launch time. The venue's metadata
has no listing date, so Stage 2B must confirm launch time and asset identity
independently before any use.

A candidate is **reliable** only if at least 14 usable dates precede it with no more than
3 consecutive unusable dates among them. Otherwise it is `first_observed_unreliable`: it
may be an old coin that a gap or the archive start hid. A coin seen on the first usable
date of the archive is `left_censored` and is never a launch.

| Status (per window)       | Criterion                                                                                                                      |
| ------------------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| `i22_count_pass`          | reliable candidates at least 40 (discovery) or 30 (holdout), and candidate ISO weeks at least 8 in both                        |
| `i22_unknown_archive`     | reliable candidates are under the minimum but reliable plus unreliable reach it: incomplete history is unknown, not a negative |
| `i22_insufficient_events` | reliable plus unreliable candidates are under the minimum                                                                      |
| `i22_lab_only`            | discovery is `i22_count_pass` and the holdout is not                                                                           |

The screen asked for at least 40 launches over the windows we may use. This draft adds the
holdout column and the weeks floor to mirror the catalyst contract (question 9).

## 5. Step B: cost only

Step B starts only if Step A ended `proceed_to_grid`, its result and the generated grid
are on `main`, and the owner gives a separate go (section 10).

### 5.1 Inputs, all pinned by SHA-256 before the first request

- the Step A result and its listing snapshot;
- the Bybit instrument catalog of section 3;
- the **grid file**: every observation (date, base, Hyperliquid key, Bybit symbol, tier),
  generated by the rules below and reviewed in its own PR.

### 5.2 Pair identity

- The pair key is the base asset. A Hyperliquid coin `kX` maps to the Bybit multiplier
  ticker with a 1,000 prefix, and other multiplier tickers (`1000000MOG`, `SHIB1000`) map
  to their base by stripping the multiplier, on both sides, as in the catalyst contract.
  The multiplier is carried into the identity check below.
- A base with more than one live Bybit USDT perp on the date is `identity_ambiguous`. It
  is counted and never sampled.
- A match at ticker level is not proof of the same asset (see the
  [identity cases](instrument-identity-cases.md)). Step B therefore runs one same-instant
  sanity gate on the two mid prices: if `abs(ln(mid_hl x multiplier / mid_bybit))`
  exceeds 0.05, the observation is `identity_mismatch`. Only the boolean is kept. The
  ratio is **not stored and not reported**, so no basis level leaves the run.
- Hyperliquid spot names (a leading `@`) are never matched.

### 5.3 Tiers

Tiers use only Step A listing metadata, never a price or a funding value. For each
eligible base with at least 10 usable discovery dates, take the median size of its hour 0
object. Split the bases into terciles by that size: `T1` largest (most active), `T2`,
`T3` smallest. Ties break by base name. A base below 10 dates is `untiered` and never
sampled. Object size is a proxy for update activity, so it is a proxy for liquidity; it is
not a measurement of it (question 6).

### 5.4 Grid generation

Deterministic, pinned before any Step B request, reproducible from the Step A result:

1. **Months.** The twelve calendar months 2024-10 to 2025-09.
2. **One date per month.** Among usable dates of the month on which every tier has at
   least 3 eligible pairs, take the date with the smallest
   `SHA-256("bybit-hl-cost-only-v1:date:" + YYYY-MM-DD)`. A month with no such date is
   `unfilled` and stays in the denominator of the report. At least 10 filled months are
   needed for `proceed_to_grid`.
3. **Three pairs per tier and date.** The 3 eligible pairs of the tier with the smallest
   `SHA-256("bybit-hl-cost-only-v1:pair:" + date + ":" + base)`.
4. **Size.** At most 12 x 3 x 3 = **108 observations**, 36 per tier.
5. **No replacement.** A pair or file that turns out absent, stale or unreadable is
   recorded and **not** replaced. The denominators stay as planned.

### 5.5 The instant and the book

The instant is `T = D 00:00:30 UTC` for every observation: 30 seconds after the hour
boundary, which to my knowledge is a funding settlement boundary for every Bybit funding
interval (1, 2, 4 and 8 hours), so the measurement is settlement-adjacent. It is the one instant that both archives hand over inside a small
prefix: a Bybit day file starts with a snapshot, and the Hyperliquid hour 0 file starts at
the hour. A later instant would need the Bybit deltas up to it, up to a large share of a
file that can reach 460 MB.

**Prefix reading.** Each file is read with one HTTP `Range` request from byte 0, never
the whole object:

| Venue                            | URL or key                                                                            | Range cap | Decoding                                |
| -------------------------------- | ------------------------------------------------------------------------------------- | --------- | --------------------------------------- |
| Hyperliquid                      | `market_data/{YYYYMMDD}/0/l2Book/{coin}.lz4`, signed, requester-pays                  | 1 MiB     | lz4 frame, streamed                     |
| Bybit, `ob200` first             | `https://quote-saver.bycsi.com/orderbook/linear/{SYMBOL}/{D}_{SYMBOL}_ob200.data.zip` | 2 MiB     | zip local header, raw deflate, streamed |
| Bybit, `ob500` if `ob200` is 404 | same path with `ob500`                                                                | 2 MiB     | same                                    |

A response that is not HTTP 206 with a matching `Content-Range` is `range_unsupported`.
The body is not read, and the run stops closed: a host that ignores `Range` could send a
whole file. This is an integrity failure, not a retry case. Neither host's support is
established in this repository; the pilot (section 5.8) is where it is learned.

**Bybit book.** The first message in the prefix must be a `snapshot`. Apply messages in
file order while `ts < T` (never reading ahead of the instant; a message at `ts == T` is
not applied). `u` must not decrease (the B2 pilot saw no regressions and did not validate
`u + 1` adjacency, so adjacency is not required here). The book is **stale** if
`T - last_ts > 5,000 ms`, the limit the burst cost study used.

**Hyperliquid book.** Take the last snapshot message with `time < T`, same 5,000 ms
staleness rule. Depth per side is whatever the message carries; the expected shape, in
particular the number of levels, is unverified until the pilot.

**Both.** `book_empty_or_crossed` if either side is empty or best bid is at or above best
ask. Non-finite or non-positive price or size is `book_broken`.

### 5.6 Measure

For `S` in `{50, 250, 500, 1000}` USD, at the one instant `T`:

```text
p_ask(S)  = S / base_quantity bought by walking S USD up the asks
p_bid(S)  = S / base_quantity sold by walking S USD down the bids
mid0      = (best_ask + best_bid) / 2
x_venue(S) = (p_ask(S) - p_bid(S)) / mid0 x 10^4        [bps]
x_total(S) = x_bybit(S) + x_hl(S)
```

Fees are not in `x`. `VWAP` already contains half the spread at entry and half at exit, so
the spread is not added again. If either side of either venue holds less than `S` USD of
depth, that size is `insufficient_depth` and `x_total(S)` is `+infinity` for ranking. For
Hyperliquid, a side with exactly the maximum levels of the archive and still short of `S`
is flagged `truncation_possible`; it is still `insufficient_depth`, the conservative
reading.

**Stored per observation:** status, venue, size, `x_venue`, half spread in bps, levels used
and book age. **Never stored or published:** any price level, any mid, the cross-venue
ratio.

**Forbidden, and tested as absent:** `mid(t + dt) - mid(t)` or any difference between two
instants, any return, any funding value, any open interest, any second instant.

### 5.7 Statuses and decision rules

**Observation statuses**, in precedence order. A status marked `integrity` stops the
whole run:

| Status                                               | Meaning                                                                                  |
| ---------------------------------------------------- | ---------------------------------------------------------------------------------------- |
| `range_unsupported` (integrity)                      | not 206 with `Content-Range`; the run stops                                              |
| `schema_mismatch` (integrity)                        | file framing or message shape differs from the expected one; the run stops               |
| `hl_object_absent`                                   | the listed object is gone or 404                                                         |
| `bybit_file_absent`                                  | `ob200` and `ob500` both 404                                                             |
| `transport_failed`                                   | all attempts failed                                                                      |
| `prefix_insufficient`                                | the cap was reached before a message at or after `T` was seen. No larger range is tried. |
| `book_not_started`                                   | the first message of a file has `ts >= T`                                                |
| `book_broken`, `book_stale`, `book_empty_or_crossed` | section 5.5                                                                              |
| `identity_mismatch`                                  | section 5.2                                                                              |
| `ok`, `insufficient_depth` (per size)                | resolved                                                                                 |

**Resolved** means `ok` or `insufficient_depth` at the size in question.

**Tier verdict at a size `S`** (the gate size is `S* = 250`; the other sizes are reported
and gate nothing):

| Status                  | Criterion                                                                                                                                                  |
| ----------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `coverage_insufficient` | resolved observations are under two thirds of the 36 planned, or fewer than 8 distinct dates, or fewer than 8 distinct bases                               |
| `execution_infeasible`  | lower median of `x_total(S)` over resolved observations is above 31.0 bps                                                                                  |
| `cost_pass`             | otherwise (lower median at or below 31.0 bps). Flagged `borderline` if a 10,000-draw date-cluster bootstrap 95% interval of the lower median contains 31.0 |

The lower median of `n` values is the `ceil(n / 2)`-th smallest. It is used so that
`+infinity` never enters an average. A median of exactly 31.0 passes. The bootstrap seed is
fixed in the code PR and is a diagnostic: it gates nothing.

**Study outcome:**

| Outcome                     | Criterion                                                                                                                           |
| --------------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| `continue_to_3b`            | the pilot passed, the run is complete, and at least one tier is `cost_pass` at `S* = 250`                                           |
| `stop_coverage`             | no tier is `cost_pass` and at least one tier is `coverage_insufficient`. No negative claim.                                         |
| `stop_execution_infeasible` | all three tiers are `execution_infeasible` at `S* = 250`. This is the screen's `execution_infeasible`, reached with no return read. |
| `stop_source_quality`       | the pilot failed, or an integrity status stopped the run, or `prefix_insufficient` is at least 20% of attempted observations        |

`continue_to_3b` restricts any two-leg protocol to the tiers that passed, and the largest
size at which a tier passed (`capacity`) bounds the notional that protocol may use. It is
not a pass of the idea and not a return read. Besides the gate, the report gives, per
tier and size, the share of observations where `x_hl < x_bybit` and the medians of each
venue alone. That answers I21's surviving question (is Hyperliquid's non-fee cost lower
for some group of coins) and gates nothing.

### 5.8 Pilot

The 12 observations with the smallest `SHA-256("bybit-hl-cost-only-v1:pilot:" + obs_id)`,
taken from the 108, run first. They are not extra and are never re-drawn. The pilot
passes only if all 12 are terminal, at least 10 are resolved, no integrity status
occurred, and at most 1 is `prefix_insufficient`. A failed pilot is `stop_source_quality`
with every receipt kept; a corrected revision starts a new run in a new root. The full run
continues with the other 96 observations only after the pilot result is reviewed and the
owner gives a separate go.

### 5.9 Budget

| Item                                      | Limit                                                                                                             |
| ----------------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| Response bytes, both hosts, all attempts  | **512 MiB**, hard; the screen's ceiling                                                                           |
| Planned first attempts, at every cap      | 108 x (1 MiB + 2 MiB) = **324 MiB** (Hyperliquid 108, Bybit 216)                                                  |
| Left for retries, 404 bodies and fallback | 188 MiB                                                                                                           |
| Attempts                                  | at most 800 (planned first attempts at most 324: 108 Hyperliquid, 108 Bybit `ob200`, up to 108 `ob500` fallbacks) |
| Attempts per file                         | 2                                                                                                                 |
| Request starts                            | 3 per second per host, one worker                                                                                 |
| Estimated cost (indicative, unverified)   | under USD 0.10 for the Hyperliquid share of the bytes; Bybit is public                                            |

Typical use should be far below the cap: a minute of Bybit deltas is expected to compress
to tens or hundreds of kilobytes. That is an expectation, not a measurement, and the pilot
reports it. If the byte ceiling is reached the run ends as budget-stopped, no snapshot is
published, and no verdict is read from a partial sample.

## 6. Why these numbers

- **Tiers, not one pooled median.** I14's coins are not the
  typical coin, and a pooled median over a grid whose mix is the design's choice is as
  arbitrary as a tier split. Tiers use listing metadata only (question 2).
- **12 dates, one per month.** Dates share a market regime, so more pairs on few dates
  buy little. One date per month spreads the dependence over the year. The 12 date
  clusters make the bootstrap interval coarse, which is why it is a flag and not a gate.
- **36 per tier, two thirds resolved.** 24 resolved observations in a tier on at least 8
  dates and 8 bases is thin. It is meant to tell a median near 15 bps from one near 60 and cannot
  separate values near 31, which the `borderline` flag reports.
- **108 observations at 3 MiB.** The byte cap, not the statistics, sets the size: more
  observations at these range caps would exceed the 512 MiB ceiling once retries are
  counted.

## 7. Consistency with earlier bounded-fetch contracts

| Rule here                                                        | Earlier contract it follows                                                              |
| ---------------------------------------------------------------- | ---------------------------------------------------------------------------------------- |
| Attempt committed to SQLite before HTTP; 1 KiB body reservation  | [B1 transport runbook](b1-bounded-fetch-runbook-v1.md)                                   |
| 512 MiB ceiling, 3 requests per second, no refund, no reset      | B1 (512 MiB, 3 per second), B2 HEAD scope (3 per second, 2 attempts per file)            |
| `run.json` binds clean revision, inputs and limits before HTTP   | B1, B2, [catalyst contract](catalyst-feasibility-v1.md) section 6                        |
| A stopped run does not resume; `--prior-root` carries the budget | Catalyst contract sections 6 and 11 (cumulative budget, new root per revision)           |
| Key-shape probe; schema mismatch stops closed                    | Catalyst "schema probe" and "undocumented endpoints"                                     |
| A cap is a stop reason, never "complete"                         | Catalyst `page_budget` (C-F5)                                                            |
| Holdout aggregates only, list sealed outside Git                 | Catalyst "holdout privacy" (section 7)                                                   |
| No replacement of a missing item; denominators preserved         | B1 pilot, B2 source pilot                                                                |
| Fixed pilot, never resampled                                     | B1 (64 IDs) and B2 (24 files)                                                            |
| Bybit book rebuilt in file order, never reading ahead; 5 s stale | [Burst cost history](bybit-burst-cost-history-v1.md) (`bybit_burst_cost.py`, `STALE_MS`) |
| Both `ob200` and `ob500` are accepted                            | B2: 656 of 724 selected books were `ob500`; one variant exists per symbol-day            |

**New in this contract**, and so the places a reviewer should look hardest: S3 listing
with signed requester-pays requests; `Range` prefix reads of archive files; lz4 frame
streaming; reading the instant from a prefix.

## 8. Ledger and transport

The design follows the catalyst study. Each step has its own run root under ignored
`runtime/research/bybit-hl-cost-only/` (`step-a/`, `step-b/`).

- **`run.json`, written once before any request:** contract version and SHA-256, clean Git
  revision, code and input hashes (Step A result, catalog, grid), endpoint, host names,
  limits, `requester_pays: true`, and the prior roots. **No credential, no key id and no
  account id.** A dirty tree or a different revision refuses to resume.
- **`ledger.sqlite`, full synchronous commits:** one row per charged attempt, written
  **before** the request opens. A kill consumes the attempt. Byte reservations are
  committed before each body read and are not refunded. Counters are reconciled with the
  receipts on resume, and an orphan or missing receipt is refused. A missing ledger is an
  error, never a reason to reset.
- **`receipts/NNNNN.json`, immutable, one per finished attempt:** sequence, step, request
  identity (host, key or URL, `Range`, date, coin or symbol), HTTP status, `Content-Range`,
  response size, body SHA-256, outcome from a fixed vocabulary, `retry_after` if any.
  Error bodies are never copied; a failed response keeps only its status and hash.
- **Bodies.** Step A keeps the parsed key list (key, size, last-modified) in the receipt
  and not the XML. Step B keeps the exact prefix bytes in `bodies/NNNNN.bin`, outside Git,
  with the SHA-256 in the receipt, so a corrective re-parse needs no re-fetch.
- **Redirects** are not followed. HTTP content encoding must be identity, so a
  compressed response cannot hide growth.
- **Resume.** Only a transport failure on the same revision resumes, up to two attempts
  per request. A schema or integrity failure is never retried.
- **`--prior-root`.** A corrected revision starts a new run and passes every stopped root.
  Their attempts and bytes count against the **same** step budget, are recorded in
  `provenance.prior_runs`, and a completed observation of a prior root is reused only if
  that root's `rules` and its parse and extraction definitions (compared as normalized
  syntax trees, as the catalyst study does) equal the new run's. Otherwise it is not
  reused and its cost still counts.
- **Seal.** `seal` publishes a deterministic result with a SHA-256 manifest. The result
  holds `x` values in bps and statuses only (section 5.6). The result record PR pins its
  SHA-256.
- **Credential hygiene.** Errors are logged by class and status, never by message text.
  Request signing happens inside one function that never receives a path for logging.

## 9. Cost of being wrong

- **Step A.** A listing mistake costs at most 12,000 list requests, under USD 0.15 at the
  venue's published S3 prices (unverified; the owner's billing decides). It reads no
  market value.
- **Step B.** The ceiling is 512 MiB. A mistake in the instant or the tier logic is a
  code defect caught in review or by the pilot, not a risk to capital.
- **Data exposure is the real cost.** Step B reads cost features on at most 12 discovery
  dates. That window is already open and is not a confirmation window, so the cost is
  small. Reading any other window would spend something that cannot be bought back,
  which is why the guards of section 2 are in the request builder.

## 10. What happens next

| Step                                 | Gate                                                                                                               |
| ------------------------------------ | ------------------------------------------------------------------------------------------------------------------ |
| This contract                        | the colleague's design discussion; fixes folded in                                                                 |
| AWS access decision                  | the owner: whether, which account, which spending cap. Not assumed.                                                |
| Registry row and Step A code PR      | one colleague review; merged on green. Adds `lz4` and a signer, so a lockfile review.                              |
| Step A listing                       | the owner's go; clean detached worktree at the merge commit; credentials in the session environment, never in chat |
| Step A report and result record      | the owner's go for the read; the result PR also pins the Step A result SHA                                         |
| Grid PR                              | the generated grid and its SHA-256, reviewed; on `main` before any Step B request                                  |
| Step B code PR                       | one colleague review; may be written in parallel with Step A, merged before Step B                                 |
| Step B pilot                         | the owner's go                                                                                                     |
| Step B full run                      | a separate owner's go, after the pilot result is reviewed                                                          |
| Step B report and result record      | the owner's go for the read                                                                                        |
| Two-leg laboratory protocol (if any) | a new HYP id; sizes, tiers, horizons, exits and thresholds fixed before any return is read                         |

No dates are promised. A go for one step is not a go for the next.

## 11. Synthetic examples for the rules

Each rule is testable on hand-built input. The code PR turns each row into a test. No row
needs a network call or real market data. All numbers are invented.

| #   | Rule                            | Input                                                                                              | Expected                                                                   |
| --- | ------------------------------- | -------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------- |
| 1   | Vwap and `x` at one size        | asks `[(100,5),(101,5)]`, bids `[(99.9,5),(99,5)]`, `S = 250`                                      | `p_ask = 100`, `p_bid = 99.9`, `x = 10.005` bps                            |
| 2   | Depth walks to the second level | same book, `S = 750`                                                                               | `p_ask = 100.3311`, `p_bid = 99.5976`, `x = 73.39` bps                     |
| 3   | Insufficient depth              | same book, `S = 1000`: bids hold 994.5 USD                                                         | `insufficient_depth`, `x_total = +inf`                                     |
| 4   | Depth shortage on both sides    | same book, `S = 1100`                                                                              | `insufficient_depth` on both sides                                         |
| 5   | Sum of two venues               | Bybit book of 1; a second book asks `[(50,10),(50.5,10)]`, bids `[(49.9,10),(49.5,10)]`, `S = 250` | `x_hl = 20.02`, `x_total = 30.025`                                         |
| 6   | Lower median, even count        | `x_total = [10, 20, 40, 50]`                                                                       | lower median 20, `cost_pass`                                               |
| 7   | Lower median keeps infinity out | `x_total = [10, 20, +inf, +inf]`                                                                   | lower median 20, no arithmetic on `inf`                                    |
| 8   | Median boundary                 | lower median exactly 31.0 / 31.01                                                                  | `cost_pass` / `execution_infeasible`                                       |
| 9   | Coverage floor                  | 36 planned, 23 resolved / 24 resolved                                                              | `coverage_insufficient` / eligible for a verdict (if dates and bases hold) |
| 10  | Study outcome precedence        | tiers: pass, infeasible, coverage_insufficient                                                     | `continue_to_3b` for the pass tier only                                    |
| 11  | No pass and a coverage gap      | tiers: infeasible, infeasible, coverage_insufficient                                               | `stop_coverage`, never `stop_execution_infeasible`                         |
| 12  | Date guard, Step B              | requests for 2024-09-30, 2025-10-01, 2026-09-29                                                    | all refused before any call                                                |
| 13  | Date guard, Step A              | requests for 2023-01-31, 2025-10-01, 2026-09-29                                                    | all refused; 2023-02-01 and 2025-09-30 allowed                             |
| 14  | Step A never downloads          | a call to any `GetObject` or `Range` method on the Step A client                                   | raises; no such method exists                                              |
| 15  | Stale book                      | last Bybit message `ts` 5,000 ms before `T` / 5,001 ms                                             | `ok` / `book_stale`                                                        |
| 16  | Never reading ahead             | a delta at `ts = T` and one at `ts = T + 1`                                                        | neither is applied                                                         |
| 17  | Prefix too short                | a prefix that ends with `ts < T`                                                                   | `prefix_insufficient`, no second request                                   |
| 18  | Range ignored                   | HTTP 200 to a `Range` request                                                                      | `range_unsupported`, body unread, run stopped                              |
| 19  | First message not a snapshot    | prefix starting with a delta                                                                       | `book_broken`                                                              |
| 20  | Hash selection of dates         | eligible dates 2024-10-15, 10-16, 10-17: digests `6949...`, `5e37...`, `6753...`                   | 2024-10-16 is chosen                                                       |
| 21  | Hash selection of pairs         | bases AAA, BBB, CCC on 2024-10-15: digests `e664...`, `8747...`, `a241...`                         | order BBB, CCC, AAA; the first 3 are taken                                 |
| 22  | No replacement                  | one chosen pair's Bybit file is 404                                                                | `bybit_file_absent`, the observation stays planned, no substitute          |
| 23  | `ob200` then `ob500`            | `ob200` 404, `ob500` present                                                                       | the `ob500` book is used and labelled                                      |
| 24  | Identity gate keeps no ratio    | mid ratio 1.2 after the multiplier                                                                 | `identity_mismatch`; the output has no ratio field                         |
| 25  | Multiplier mapping              | `kPEPE` against `1000PEPEUSDT`; `PEPE` against both `PEPEUSDT` and `1000PEPEUSDT` live             | the first maps; the second is `identity_ambiguous`                         |
| 26  | Forbidden computations          | a result schema with any of `return`, `funding`, `basis`, `mid_change`                             | schema validation fails                                                    |
| 27  | Tier split                      | 9 bases with sizes 1 to 9                                                                          | `T3 = {1,2,3}`, `T2 = {4,5,6}`, `T1 = {7,8,9}`                             |
| 28  | Untiered                        | a base with 9 usable dates                                                                         | `untiered`, never sampled                                                  |
| 29  | Archive coverage                | 292 of 365 usable / 291 of 365                                                                     | `archive_covered` / `archive_partial` (if the other criteria hold)         |
| 30  | Longest gap                     | a 15-day unusable run in an otherwise full year                                                    | not `archive_covered`                                                      |
| 31  | First observed, reliable        | first listing after 14 usable dates                                                                | reliable candidate                                                         |
| 32  | First observed, left-censored   | first listing on the archive's first usable date                                                   | `left_censored`, not a launch                                              |
| 33  | I22 unknown versus negative     | 35 reliable and 10 unreliable candidates, minimum 40                                               | `i22_unknown_archive`; 35 and 4 gives `i22_insufficient_events`            |
| 34  | Holdout privacy                 | any report field with a holdout coin name or a holdout date                                        | schema validation fails                                                    |
| 35  | Prior-root budget               | prior root used 400 attempts and 100 MiB                                                           | the new run's limits are the totals minus 400 and 100 MiB                  |
| 36  | Prior root with other rules     | prior root's `rules` differ                                                                        | refused, or its receipts not reused                                        |
| 37  | Budget stop                     | cumulative response bytes reach 512 MiB                                                            | run ends budget-stopped; no snapshot published                             |
| 38  | Credential hygiene              | a fake secret inside an injected error text                                                        | absent from `run.json`, the ledger and every receipt                       |
| 39  | Missing credentials             | no credential in the environment                                                                   | `credential_missing`; zero network calls                                   |
| 40  | Attempt before HTTP             | process killed after the ledger commit                                                             | the attempt stays charged on resume                                        |

## 12. Not in this study

- A return, a mid-price move, a funding rate, a basis level or open interest, on any
  window.
- Any second instant, any window outside the discovery year in Step B, and any listing of
  the HYP-031 test year or of any date on or after 2026-09-29.
- Order types, latency, margin and collateral transfer, the failure of one leg, and
  Hyperliquid fee tiers or staking discounts. The cost sheet's fees are taken as given.
- Cost at non-settlement instants, and cost at the minutes of an I14 funding gap.
- I22's cost, and I22's independent launch-time and identity confirmation (Stage 2B).
- Any two-leg laboratory protocol. It starts only after `continue_to_3b`, under a new HYP
  id, with horizons, exits and thresholds fixed before any return is read.
- A live collector of either venue's book. Order book history is reserved for execution
  cost evaluation, as the consilium state records.

## 13. Open design questions for the colleague

My position is given for each. None is decided.

1. **Window if the archive does not cover the discovery year.** The registry says the
   test year is `reserved` (HYP-031 only) and the program plan says unassigned. Position:
   do not extend automatically; list nothing outside 2023-02-01 to 2025-09-30. Should
   this contract allow a coverage-only listing pass of the test year (dates and key
   counts, no sizes) if discovery is `archive_partial`, or must that wait for an
   amendment? Separately, which of the two statements about the test year should the
   registry carry?
2. **Gate size and the tier rule.** The screen says one median over the grid at the
   working size. This draft gates at `S* = 250` and passes the family if any one of three
   tiers passes, then restricts the protocol to passing tiers. That departs from a plain
   pooled median. Alternative: a pooled median with equal tier weights, or gating at 1,000.
3. **One instant, settlement-adjacent (`T = 00:00:30`).** The only cheap instant for
   Bybit's day files. It says nothing about non-settlement hours. Is it enough for
   I14 and I15, and acceptable as input to I21's general question? An extra stratum costs
   up to a large share of a day file per observation.
4. **`Range` on `quote-saver.bycsi.com`, and the byte model.** Range support is not
   established anywhere in the repository. If it fails, the contract stops closed. Fallback
   options: skip files above a HEAD-sized limit, or restrict to small symbols. The range
   caps (1 MiB, 2 MiB) and 108 observations come from that model.
5. **Fee tier and the 31.0 derivation.** 31.0 assumes base fees of 20.0 bps. A Bybit
   Innovation Zone pair costs 11.0 bps a side, which makes fees 31.0 and the budget
   about 20.2. The Bybit catalog stored by the catalyst run has no zone flag. Should the
   budget become pair specific, or should Innovation pairs be a separate stratum?
6. **Tiers by Hyperliquid object size, and the sample is not the I14 sample.** Size is an
   activity proxy only. The pairs where the funding gap occurred need a funding read to
   select, which this contract forbids. So a pass is a necessary condition for the
   typical coin and says little about those coins. Is that acceptable, or should the
   contract add a funding-based stratum under its own, later amendment?
7. **Hyperliquid file shape and truncation.** The archive's level count and message
   fields are unverified. A side that hits the maximum level count and is still short is
   treated as `insufficient_depth`, which biases toward stopping. Agree?
8. **Credentials and dependencies.** Who creates the key, with which account and cap?
   `boto3` against a small SigV4 signer on `httpx`, and `lz4`, both change `uv.lock`.
   Does the colleague want that decided before the Step A code is written?
9. **I22 thresholds and the Bybit catalog pin.** I mirrored the catalyst floors (40 and
   30 launches, 8 weeks) where the screen gave only 40. The pin uses the catalyst run's
   instrument catalog, which sits outside Git. Alternative: the HYP-031 discovery index.
   Also, are hour 0 presence plus an hourly refinement enough to date a launch, given
   Stage 2B must confirm it anyway?
10. **Thresholds.** 80% usable dates, 14-day gap, 11 of 12 months, two thirds resolved,
    8 dates and 8 bases per tier, 36 per tier, 20% `prefix_insufficient`. All are mine.
    Are 12 date clusters enough for a median that may sit near 31?
11. **`ob200` against `ob500`.** Both are accepted and labelled. Mixing them in one tier
    may confound depth with variant. Report by variant, or force `ob200` only?
12. **Book rules.** No `u + 1` adjacency, `ts` rather than engine `cts`, 5 s staleness.
    The B2 pilot supports the first two on 12 files; Hyperliquid's snapshot cadence is
    unknown, so 5 s may be wrong for quiet coins.
