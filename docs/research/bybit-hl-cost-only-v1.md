# Bybit against Hyperliquid cost-only v1 (contract draft, 2026-11-17)

Status: **draft for the colleague's one design review. It authorizes no listing, no
request and no download.** Every network step below needs the owner's separate go, and
the Hyperliquid steps also need AWS credentials that the owner has not provided and this
contract does not assume (section 3). The registry row ships with this draft. The code goes to one
review after this design is settled.

Study id: `bybit-hl-cost-only-v1`. No HYP id is assigned. One is assigned only if a
two-leg laboratory protocol follows (section 12).

**Revision 2 (2026-11-17), after two colleague reviews of revision 1.** Changed: one fixed
quantity per leg priced on both sides of the book (5.6); HTTP errors classified before the
`Range` check (5.5); a fixed evaluation order in which integrity and source-quality stops
come before any tier verdict (5.7); identity from a pinned asset and contract-unit map, with
the price ratio demoted to a diagnostic flag (5.2); `insufficient_depth` split from
`depth_unobserved` (5.6); `cost_inconclusive` as a named boundary outcome (5.7); scope of a
tier-limited pass, the Innovation Zone check and per-variant reporting (5.7); the
Hyperliquid freshness threshold set by a rule from pilot cadence (5.5). The questions that
stay open are in section 13.

**Revision 3 (2026-11-17), after the review of revision 2.** Changed: `q` is no longer a
saved or published derived result, because `mid = S / q` recovers the price, and the
boundary between private raw bytes and the published report is stated (5.6); the signer
question is closed: `boto3` and `botocore` sign every Hyperliquid request, with SDK retries
disabled and every actual HTTP send passing through the attempt journal (section 8). No AWS
key is needed now.

**Revision 4 (2026-11-17), after the second review.** Changed: the byte ceiling is enforced
before parsing, because a listing page is bounded by key size and not by key count (1,000
distinct valid keys are 1,174,037 bytes). `boto3` only signs; our code sends and reads the
response through a bounded stream with a per-response limit, and an oversized response is
cut, never parsed, never retried and classified `response_too_large` (section 8). The
fake-server tests are stated as required tests of the code PR, which does not exist yet.
Merged `origin/main` (the I25 protocol, #608) into the branch.

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
  owner's open decision. **No key is needed now.** Until the decision, both steps are
  design only, and a Step A go needs a concrete request list and budget first (section 10).
- **No credential reaches the repository or a log.** Credentials are read from the
  process environment of the run only, never from a file in the repository, never printed
  and never stored in `run.json`, the ledger or a receipt. The code refuses to start
  without them (status `credential_missing`) and makes no network call in that case.
  Test: a fake secret inside an injected error text is absent from every written file
  (section 11).
- **Least privilege.** The key the owner creates should allow only `s3:ListBucket` and
  `s3:GetObject` on `hyperliquid-archive`. Step A needs only `ListBucket`. This is a
  recommendation to the owner, not a requirement the code can check.
- **New dependencies are a review point.** `boto3` (with `botocore`) signs every
  Hyperliquid request, and `lz4` decodes frames; neither is in `uv.lock`, so the code PR
  changes the lockfile and goes through review. There is no hand-written SigV4. Bybit
  requests stay on `httpx`. The conditions under which `boto3` is allowed are in section 8.
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
- **Eligible pair counts, name level.** Coins whose name matches a live Bybit USDT perp
  by the naming rules of section 5.2, per date and per calendar month, with
  `identity_ambiguous` counted apart. These are an **upper bound**: the pinned identity
  map of section 5.2 can only be built after the coin list exists, so the grid PR applies
  it and re-checks the thresholds below on map-verified pairs.
- **Provenance.** Run binding hash, code revision, input hashes, request and byte
  counters, `listed_at` timestamps. The archive is not immutable (the venue says data is
  uploaded about monthly and "may be missing"), so the result is a dated snapshot, and Step
  B's grid is frozen from that snapshot.

The report shows no price, no order book and no object body.

### 4.4 Budget

| Item                                    | Limit                                                                                   |
| --------------------------------------- | --------------------------------------------------------------------------------------- |
| Attempts, all passes together           | 12,000 (planned first attempts at most 10,210)                                          |
| Sub-caps on first attempts              | pass 0: 10; pass 1: 3,000; pass 2: 7,200 (150 coins x 48)                               |
| Response bytes                          | 256 MiB, enforced per 1 KiB block read, each listing page also cut at 2 MiB (section 8) |
| Request starts                          | 3 per second, one worker                                                                |
| Attempts per request                    | 2                                                                                       |
| Estimated cost (indicative, unverified) | under USD 0.15: 12,000 list requests plus 256 MiB of transfer                           |

A pass cap that is reached ends the run as `listing_incomplete` with reason
`request_budget`, never as complete. Pass 2 stops closed at 150 coins; a larger set is a
finding that needs an amendment, not a silent truncation.

### 4.5 Status codes and criteria

**Run level** (checked first; any value other than `listing_complete` ends the run and the
report refuses to run, so a partial listing is never read as a complete archive):

| Status                                                                                                                       | Meaning                                                                                                           |
| ---------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| `listing_complete`                                                                                                           | Every planned request ended terminal and in budget                                                                |
| `credential_missing`                                                                                                         | No credentials in the environment; no request was made                                                            |
| `access_denied`                                                                                                              | HTTP 403 on a signed request, for example requester-pays not accepted                                             |
| `schema_mismatch`                                                                                                            | Keys do not match the expected `market_data/.../l2Book/....lz4` shape, or the pass 0 probe contradicts the layout |
| `listing_incomplete` with reason `request_budget`, `byte_budget`, `page_budget`, `response_too_large` or `transport_failure` | A cap was reached or a request failed all attempts                                                                |

A stopped run does not resume. A corrected revision starts a new run in a new root and
passes the stopped root with `--prior-root` (section 8).

**Coverage per window** (discovery year only; the holdout gets counts, no verdict):

| Status            | Criterion                                                                                                                                           |
| ----------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| `archive_covered` | usable dates are at least 80% of the window, the longest unusable run is at most 14 days, and at least 11 of 12 months have 15 or more usable dates |
| `archive_partial` | usable dates are at least 30% of the window but the criterion above fails                                                                           |
| `archive_absent`  | usable dates are under 30% of the window                                                                                                            |

**Step A outcome:**

| Outcome                    | Criterion                                                                                                                                                                                                                                                                |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `proceed_to_grid`          | discovery is `archive_covered`; at least 40 distinct eligible bases over the year; at least 10 months can host a grid date (section 5.4). Name-level counts here; the grid PR repeats the check on map-verified pairs, and a failure there is `window_extension_needed`. |
| `window_extension_needed`  | discovery is `archive_partial`, or it is covered but the pair or month criteria fail. No Step B. Options go to A, B and C.                                                                                                                                               |
| `stop_archive_unavailable` | discovery is `archive_absent`. Stage 2A cannot be costed from Hyperliquid history in an allowed window. No extension is chosen here.                                                                                                                                     |

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
- the **identity map** of section 5.2;
- the **grid file**: every observation (date, base, Hyperliquid key, Bybit symbol, tier),
  generated by the rules below and reviewed in its own PR together with the map.

### 5.2 Pair identity

- **Name level is only a candidate rule.** Matching a Hyperliquid coin `kX` to the Bybit
  ticker with a 1,000 prefix, and stripping other multipliers (`1000000MOG`, `SHIB1000`),
  as in the catalyst contract, proposes candidates. A ticker match is not proof of the
  same asset (see the [identity cases](instrument-identity-cases.md)).
- **Identity comes from a pinned map, not from prices.** The map has one row per base:
  the Bybit symbol, the Hyperliquid coin, the **contract unit on each venue** (tokens per
  contract: `kPEPE` and `1000PEPEUSDT` both quote 1,000 tokens, so their unit ratio is 1),
  and the evidence for the row (a venue document or a recorded manual check). It is built
  from venue metadata and documentation, reviewed in the grid PR, and pinned by SHA-256
  **before the first Step B request**. It contains no price.
- **Statuses.** A base with no map row is `identity_unverifiable`: counted, never
  sampled. A base whose unit ratio is not exactly 1 is `unit_ratio_not_one`: counted,
  never sampled in v1, so a leg's quantity is the same number of tokens on both venues up
  to the one-instant basis. A base with more than one live Bybit USDT perp on the date
  (for example `PEPEUSDT` and `1000PEPEUSDT`) is `identity_ambiguous`: counted, never
  sampled.
- **A price ratio is a diagnostic flag, never an identity test.** At the instant, the
  code may compute `abs(ln(mid_hl x unit_ratio / mid_bybit)) > 0.05` and set a boolean
  `price_ratio_flag`. It never changes an identity status and never removes an
  observation, because a real basis above 5% is possible and similar prices do not prove
  one asset. The ratio is **not stored and not reported as a level**; only the boolean
  and per-tier counts of it leave the run. The report gives the tier medians with and
  without flagged observations.
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
   least 3 eligible pairs (a pair is eligible only with a row in the pinned identity map,
   a unit ratio of 1 and a live Bybit perp, section 5.2), take the date with the smallest
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

**HTTP classification, in this order.** The status code is classified first, and the
`Range` check applies only to a successful response.

| Response                                        | Status and handling                                                                                                                                             |
| ----------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| transport error, timeout, HTTP 429 or 5xx       | retry rules of section 8 (honour a whole-second `Retry-After` up to 300 s, else 10 s then 30 s; 2 attempts per file); after the last attempt `transport_failed` |
| HTTP 404 on a Bybit `ob200` URL                 | not an error: the same day's `ob500` URL is requested next. Both 404: `bybit_file_absent`                                                                       |
| HTTP 404 on a Hyperliquid key                   | `hl_object_absent`                                                                                                                                              |
| HTTP 403 on a signed Hyperliquid request        | `access_denied` (integrity: the run stops)                                                                                                                      |
| 3xx, and any other 4xx (400, 401, 410, 416 ...) | `http_rejected` with the code recorded; not retried; the observation is unresolved and counts against coverage                                                  |
| HTTP 200 or 206                                 | now the `Range` check: 206 with `Content-Range` starting at byte 0 and ending at or before the cap is accepted. Anything else is `range_unsupported`            |

A **200** to a `Range` request, or a 206 whose `Content-Range` does not match, is
`range_unsupported`. The body is not read, and the run stops closed: a host that ignores
`Range` could send a whole file. This is an integrity failure, not a retry case. Neither
host's support is established in this repository; the pilot (section 5.8) is where it is
learned.

**Bybit book.** The first message in the prefix must be a `snapshot`. Apply messages in
file order while `ts < T` (never reading ahead of the instant; a message at `ts == T` is
not applied). `u` must not decrease (the B2 pilot saw no regressions and did not validate
`u + 1` adjacency, so adjacency is not required here). The book is **stale** if
`T - last_ts > 5,000 ms`, the limit the burst cost study used.

**Hyperliquid book.** Take the last snapshot message with `time < T`. Depth per side is
whatever the message carries; the expected shape, in particular the maximum number of
levels, is unverified until the pilot.

**Hyperliquid freshness threshold.** Bybit keeps the fixed 5,000 ms limit. For Hyperliquid
the limit `L_hl` is set by this rule, fixed now, from the pilot's **snapshot cadence**
only: `L_hl = max(5,000 ms, p99 of the gaps between consecutive snapshots in the pilot's
12 Hyperliquid prefixes, rounded up to a whole second)`. It is computed and recorded in a
pinned note before the full run, from timestamps alone, and never from a cost or an
`x` value; no pilot observation's cost is computed before it is pinned. If it exceeds 30,000 ms the outcome is `stop_source_quality`: the archive is
too sparse for a one-instant read.

**Both.** `book_empty_or_crossed` if either side is empty or best bid is at or above best
ask. Non-finite or non-positive price or size is `book_broken`.

### 5.6 Measure

For `S` in `{50, 250, 500, 1000}` USD, on each venue separately, at the one instant `T`:

```text
mid0   = (best_ask + best_bid) / 2
q      = S / mid0                  # fixed once per venue, size and instant, in that venue's
                                   # own contract units, continuous (no lot rounding in v1)
C(q)   = cost of buying q by walking the asks
P(q)   = proceeds of selling q by walking the bids
x_venue(S) = (C(q) - P(q)) / S x 10^4        [bps]
x_total(S) = x_bybit(S) + x_hl(S)
```

One leg enters on one side of the book and exits on the other: a long buys `q` at the
asks and sells the same `q` at the bids, a short sells `q` at the bids and buys the same
`q` back at the asks. Both use the same `q`, so `C(q) - P(q)` is the cost of one round
trip of one position and the two numbers are comparable. `q x mid0 = S`, so the
denominator is `S`. Within the top level this reduces to `(ask - bid) / mid0`, and the
spread is not added again: walking the book already contains half the spread at entry
and half at exit. Because the identity map admits only unit ratio 1 (section 5.2), the
two venues' `q` are the same number of tokens up to the one-instant basis.

**Depth must cover `q` on both sides.** Each side is checked against `q` base units, not
against `S` dollars. Let `L_max` be the maximum number of levels a side can carry in that
archive variant: 200 for `ob200`, 500 for `ob500`, and for Hyperliquid the maximum
observed in the pilot, fixed before the full run (until it is fixed, every Hyperliquid
side counts as at its limit).

| Case                                                             | Status                                                                                                                               |
| ---------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| both sides of both venues hold at least `q`                      | `ok`                                                                                                                                 |
| a side holds less than `q` and has **fewer** than `L_max` levels | `insufficient_depth`: the book was fully observed and is too thin. `x_total(S) = +infinity` for ranking                              |
| a side holds less than `q` and has exactly `L_max` levels        | `depth_unobserved`: a data limit, not proof of a thin book. No cost value, never `+infinity`, not resolved, counted against coverage |

If one venue is `insufficient_depth` and the other `depth_unobserved`, the observation is
`insufficient_depth` (a fully observed thin book is a fact about that observation either
way). `depth_unobserved` can lower a tier's coverage but is never by itself a reason for
`stop_execution_infeasible`.

**Stored per observation, in the sealed result and in the published report:** status,
venue, size `S`, `x_venue`, half spread in bps, the number of levels used, book age, book
variant and `price_ratio_flag`. **`q` is not saved.** Since `mid0 = S / q`, a saved `q`
would give the mid price back, and across two venues their ratio. `C(q)`, `P(q)`, `mid0`,
the best bid and ask and the cross-venue ratio are computed in memory and discarded. The
cost number needs none of them after it is computed.

**Where the boundary lies.** Three places, kept apart:

1. **Private raw bytes** (`bodies/NNNNN.bin`, section 8): the exact prefix of each archive
   file. They contain prices and sizes, stay outside Git on the run machine, are never
   published, and exist so that a corrective re-parse needs no new request.
2. **In-memory intermediates:** the quantities and book-derived values above. Never
   written to a file, a log, a receipt or an exception message.
3. **The sealed result and the published report:** only the fields listed above, in bps and
   counts. A schema check rejects any other field (example 38).

**Forbidden, and tested as absent:** `mid(t + dt) - mid(t)` or any difference between two
instants, any return, any funding value, any open interest, any second instant, and any
saved field from which a price level can be recovered (`q`, a mid, a best price, a walked
cost or proceeds, a cross-venue ratio).

### 5.7 Statuses and decision rules

**Observation statuses.** A status marked `integrity` stops the whole run.

| Status                                                              | Meaning                                                                                  |
| ------------------------------------------------------------------- | ---------------------------------------------------------------------------------------- |
| `range_unsupported` (integrity)                                     | a successful response that ignored or mismatched `Range` (section 5.5)                   |
| `schema_mismatch` (integrity)                                       | file framing or message shape differs from the expected one                              |
| `access_denied` (integrity)                                         | HTTP 403 on a signed Hyperliquid request                                                 |
| `hl_object_absent`                                                  | the listed object is gone (404)                                                          |
| `bybit_file_absent`                                                 | `ob200` and `ob500` both 404                                                             |
| `transport_failed`                                                  | retriable failure on every attempt                                                       |
| `http_rejected`                                                     | redirect or other 4xx, code recorded, not retried                                        |
| `prefix_insufficient`                                               | the cap was reached before a message at or after `T` was seen. No larger range is tried. |
| `book_not_started`                                                  | the first message of a file has `ts >= T`                                                |
| `book_broken`, `book_stale`, `book_empty_or_crossed`                | section 5.5                                                                              |
| `identity_unverifiable`, `unit_ratio_not_one`, `identity_ambiguous` | section 5.2; these never reach the grid, so they appear only in the denominator counts   |
| `depth_unobserved`                                                  | a side at its level limit and short of `q` (section 5.6); unresolved                     |
| `ok`, `insufficient_depth` (per size)                               | **resolved**                                                                             |

**Evaluation order for the study result, fixed.** Each step is checked only if the earlier
ones did not stop the run, and **a stop at steps 1 to 4 cannot be overridden by a passing
tier**. Tier numbers computed after a stop at step 3 or 4 may be shown as diagnostics,
labelled as not a verdict.

1. **Integrity.** Any `integrity` status: `stop_source_quality`, nothing sealed.
2. **Budget.** The 512 MiB or the attempt ceiling reached: `stop_budget`, no snapshot.
3. **Pilot.** The pilot failed (section 5.8), or `L_hl` exceeds 30,000 ms:
   `stop_source_quality`.
4. **Source quality on the full set.** `prefix_insufficient` is at least 20% of the 108
   planned observations (22 of 108 is 20.37%): `stop_source_quality`.
5. **Tier verdicts**, then the **study outcome**, below.

**Tier verdict at a size `S`** (the gate size is `S* = 250`; the other sizes are reported
and gate nothing). Evaluated in this order, the first that holds:

| Status                  | Criterion                                                                                                                                                                                                                                               |
| ----------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `coverage_insufficient` | resolved observations are under two thirds of the 36 planned (24), or fewer than 8 distinct dates, or fewer than 8 distinct bases. `depth_unobserved` and every unresolved status count against this.                                                   |
| `cost_inconclusive`     | the 95% interval of the lower median of `x_total(S)` contains 31.0 bps. The interval is the 2.5th to 97.5th percentile of 10,000 date-cluster bootstrap draws (whole dates resampled with replacement; the seed is fixed in the code PR before the run) |
| `execution_infeasible`  | otherwise, and the interval lies above 31.0 bps                                                                                                                                                                                                         |
| `cost_pass`             | otherwise: the interval lies below 31.0 bps                                                                                                                                                                                                             |

The lower median of `n` values is the `ceil(n / 2)`-th smallest, so `+infinity` never
enters an average. Twelve date clusters are too few to decide a median near the
boundary, so a boundary case is **named, not rounded**: `cost_inconclusive` stops
spending and does not pass. The screen's plain rule (median above 31.0 stops the family)
is kept for a clear case and made stricter for a pass.

**Study outcome**, first that holds:

| Outcome                     | Criterion                                                                                                                                |
| --------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| `stop_source_quality`       | steps 1, 3 or 4 above                                                                                                                    |
| `stop_budget`               | step 2 above                                                                                                                             |
| `continue_to_3b`            | at least one tier is `cost_pass` at `S* = 250`                                                                                           |
| `stop_coverage`             | no tier passes and at least one tier is `coverage_insufficient`. No negative claim.                                                      |
| `stop_cost_inconclusive`    | no tier passes, none is `coverage_insufficient`, and at least one is `cost_inconclusive`. Stops spending; no pass and no negative claim. |
| `stop_execution_infeasible` | all three tiers are `execution_infeasible` at `S* = 250`. This is the screen's `execution_infeasible`, reached with no return read.      |

**Scope of a pass.** `continue_to_3b` is not a pass of the idea and not a return read.

- **Tiers.** A two-leg protocol may use only the tiers that passed, and the largest size
  at which a tier passed (`capacity`, among 250, 500 and 1,000) bounds its notional. If
  only `T1` (the largest Hyperliquid files, section 5.3) passes, Stage 3B may use only
  `T1` instruments, and **its first gate**, before any return is read, is whether `T1`
  contains the funding-gap occurrences the idea needs at all (a count, under its own
  contract). A `T1`-only pass does not stand for typical alts.
- **Innovation Zone.** 31.0 stays the base filter, and its gate does not change. It comes
  from 51.2 minus the base fees of 20.0, which is 31.2, rounded **down** to 31.0, so 0.2
  bps is the rounding reserve. A Bybit Innovation Zone pair pays 11.0 bps a side, so its
  fees are 31.0 and the same arithmetic leaves 51.2 minus 31.0 = **20.2 bps** for non-fee
  cost. The report therefore carries a column `innovation_zone` (`true`, `false` or
  `unknown`) per Bybit symbol and the medians split by it where known. The catalog pinned
  now has no zone flag; its source is fixed in the code PR (an instrument field if one
  exists, otherwise Bybit's published list). **Before Stage 3B**, the flag must be checked
  for every ticker of a passing tier and the 20.2 bps figure stated for the zone pairs
  among them. No gate changes now.
- **Variants.** The report gives the `ob200` and `ob500` results and coverage separately
  (counts per status, resolved share, medians). The selection policy is frozen before the
  run: `ob200` first, `ob500` only after a 404. The gate uses the pooled sample. A tier
  where both variants have at least 8 resolved observations and their lower medians fall
  on different sides of 31.0 is flagged `variant_sensitive` (a diagnostic, not a gate).
- **I21.** Besides the gate, the report gives, per tier and size, the share of
  observations where `x_hl < x_bybit` and each venue's median alone. That answers I21's
  surviving question (is Hyperliquid's non-fee cost lower for some group of coins) and
  gates nothing.

### 5.8 Pilot

The 12 observations with the smallest `SHA-256("bybit-hl-cost-only-v1:pilot:" + obs_id)`,
taken from the 108, run first. They are not extra and are never re-drawn. The pilot
passes only if all 12 are terminal, at least 10 are resolved, no integrity status
occurred, at most 1 is `prefix_insufficient`, and `L_hl` (section 5.5) is at most 30,000 ms.
The pilot also fixes the Hyperliquid level limit `L_max` (section 5.6) and publishes its
cadence statistics before any cost is computed. A failed pilot is `stop_source_quality`
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
  clusters make the bootstrap interval coarse, which is why a median whose interval
  contains 31.0 is `cost_inconclusive` and not a pass or a fail.
- **36 per tier, two thirds resolved.** 24 resolved observations in a tier on at least 8
  dates and 8 bases is thin. It is meant to tell a median near 15 bps from one near 60 and cannot
  separate values near 31, which `cost_inconclusive` reports.
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
- **`boto3` signs; our code sends and reads.** The SDK has its own retries, redirects and
  whole-body reads, and a listing response is parsed only after the SDK has read all of it,
  so a size limit cannot be put on the SDK's read. The contract therefore uses `boto3`
  (with `botocore`) **only to sign**: the client, built once from the three standard
  environment variables passed explicitly and with `retries={"total_max_attempts": 1}`,
  produces a short-lived presigned URL (`generate_presigned_url`, 60 seconds, with the
  requester-pays parameter). **Our own `httpx` code sends it and reads the response
  through a bounded stream.** (A `before-send` hook or any other mechanism is allowed only
  if the large-response test below passes for it.) Consequences:
  - **One HTTP send is one attempt, and every send is ours.** The SDK client makes no HTTP
    call, so it has no hidden retry, region redirect, checksum validation, credential
    refresh or metadata lookup to count. The journal row is committed **before** the send
    opens, and the send is refused if the step's attempt or byte budget is exhausted. Our
    own loop makes a second attempt as a new journaled send, under the retry rules of
    section 5.5; attempts per request stay at most 2. Redirects are not followed.
  - **Bytes are bounded before parsing.** Before each body read of at most 1 KiB the
    journal commits a 1 KiB reservation (the B1 convention), so the step cap is enforced at
    block granularity and the charged total never exceeds it. Each response also has a
    **per-response read limit `L`**: 2 MiB for a listing page (`MaxKeys` is fixed at 1,000;
    each key may be up to 1,024 bytes, and a synthetic page of 1,000 distinct valid keys is
    already 1,174,037 bytes, so the key count alone bounds nothing) and the range length
    (1 MiB, Step B) for a ranged `GetObject`. The read stops at `L` bytes and the stream is
    closed.
  - **What is cut, and how it is classified.** A response that would pass `L` is cut at
    exactly `L` bytes. The cut bytes are charged and **never parsed**: a truncated XML or
    frame is not interpreted. The attempt is `response_too_large`. It is never retried, and
    in Step A it ends the run as `listing_incomplete` with reason `response_too_large`, so
    an oversized page is a finding for a reviewed change in a new run root, not a silent
    truncation. In Step B a body longer than its range is also cut and is
    `range_unsupported`.
  - **Credentials and URLs.** The presigned URL carries the access key id and a signature
    in its query string. It exists only in memory for the send, is never logged, never put
    in a receipt (which keeps host, bucket, key, range and date), and is stripped from
    any transport exception before the error is recorded (class and status only).
  - **The proof is a set of tests of the code PR.** They are **requirements on that PR.
    No test, no fake server and no code exists yet.** Against a local fake S3 server on
    `localhost` (as the B1 transport tests do), the number of HTTP requests the server
    receives must equal the number of journal rows, in these runs: a plain call, a forced
    HTTP 500, a forced 301 region redirect, a dropped connection, and the same with
    `AWS_MAX_ATTEMPTS=10` set in the environment; the SDK client must make zero HTTP
    calls; and the large-response runs of examples 66 to 69 must hold. The `boto3` version
    is pinned in `uv.lock`; a version change repeats the tests.
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
  Request signing happens inside the SDK client, which is built once from the environment
  and never logs a request. A fake secret placed in the environment appears in no file the
  run writes (example 59).

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

| Step                                 | Gate                                                                                                                                                                                                     |
| ------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| This contract                        | the colleague's design discussion; fixes folded in                                                                                                                                                       |
| AWS access decision                  | the owner: whether, which account, which spending cap. Not assumed.                                                                                                                                      |
| Registry row and Step A code PR      | one colleague review; merged on green. Adds `boto3` and `lz4`, so a lockfile review.                                                                                                                     |
| Step A listing                       | the owner's go, after a concrete request list (the exact prefixes and the budget table) is published; clean detached worktree at the merge commit; credentials in the session environment, never in chat |
| Step A report and result record      | the owner's go for the read; the result PR also pins the Step A result SHA                                                                                                                               |
| Grid PR                              | the generated grid and its SHA-256, reviewed; on `main` before any Step B request                                                                                                                        |
| Step B code PR                       | one colleague review; may be written in parallel with Step A, merged before Step B                                                                                                                       |
| Step B pilot                         | the owner's go                                                                                                                                                                                           |
| Step B full run                      | a separate owner's go, after the pilot result is reviewed                                                                                                                                                |
| Step B report and result record      | the owner's go for the read                                                                                                                                                                              |
| Two-leg laboratory protocol (if any) | a new HYP id; sizes, tiers, horizons, exits and thresholds fixed before any return is read                                                                                                               |

No dates are promised. A go for one step is not a go for the next.

## 11. Synthetic examples for the rules

Each rule is testable on hand-built input. The code PR turns each row into a test. No row
needs a network call or real market data. All numbers are invented. Quantities in rows 1 to 8
are in coins and show `q`, `C` and `P` only as intermediates the code does not save; every
`x` was recomputed by hand from the formulas of section 5.6. Rows 62 to 70 are
required tests of the code PR for the `boto3` signing path and the bounded read, against a
local fake server. None of them exists yet.

| #   | Rule                                                | Input                                                                                                                          | Expected                                                                                                                                                        |
| --- | --------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | Fixed quantity, one level                           | Book A: asks `[(100,5),(101,5)]`, bids `[(99.9,5),(99,5)]` (price, coins). `S = 250`                                           | `mid0 = 99.95`, `q = 2.50125`, `C = 250.125`, `P = 249.8749`, `x = 10.005` bps                                                                                  |
| 2   | Depth walks to the second level                     | Book A, `S = 750`                                                                                                              | `q = 7.50375`, `C = 752.879`, `P = 747.371`, `x = 73.43` bps                                                                                                    |
| 3   | Depth short on both sides                           | Book A, `S = 1000`: `q = 10.005`, each side holds 10 coins, 2 levels of a 200-level limit                                      | `insufficient_depth`, `x_total = +inf`                                                                                                                          |
| 4   | Same quantity on both sides (reviewer case)         | asks `[(100,2.5)]`, bids `[(99.9,3)]`, `S = 250`: `q = 2.50125`                                                                | `insufficient_depth`: the asks hold 2.5 < `q`. A rule that sells `S` dollars and buys `S` dollars separately would have passed at about 10.005 bps              |
| 5   | Truncated book is not a thin book                   | a Hyperliquid side with exactly `L_max` levels holding less than `q`                                                           | `depth_unobserved`: no cost value, not `+inf`, not resolved, counted against coverage                                                                           |
| 6   | Fully observed thin book next to a truncated one    | Bybit side thin with 40 of 200 levels, Hyperliquid side at `L_max` and short                                                   | `insufficient_depth`                                                                                                                                            |
| 7   | Unobserved depth alone never stops the family       | a tier whose 36 observations are 20 `ok` below 31.0 and 16 `depth_unobserved`                                                  | `coverage_insufficient` (20 resolved < 24), so `stop_coverage` at most; never `stop_execution_infeasible`                                                       |
| 8   | Sum of two venues                                   | Book A as Bybit, `S = 250`; Hyperliquid asks `[(50,10),(50.5,10)]`, bids `[(49.9,10),(49.5,10)]`                               | `x_bybit = 10.005`, `x_hl = 20.02`, `x_total = 30.025`                                                                                                          |
| 9   | Lower median, even count                            | `x_total = [10, 20, 40, 50]`                                                                                                   | lower median 20                                                                                                                                                 |
| 10  | Lower median keeps infinity out                     | `x_total = [10, 20, +inf, +inf]`                                                                                               | lower median 20, no arithmetic on `inf`                                                                                                                         |
| 11  | Tier: interval clear of 31.0                        | lower median 20, interval `[14, 26]` / lower median 40, interval `[33, 47]`                                                    | `cost_pass` / `execution_infeasible`                                                                                                                            |
| 12  | Tier: boundary named, not rounded                   | lower median 28, interval `[22, 33]` / lower median 35, interval `[29, 41]` / lower median exactly 31.0, interval `[27, 36]`   | `cost_inconclusive` in all three                                                                                                                                |
| 13  | Coverage floor                                      | 36 planned, 23 resolved / 24 resolved                                                                                          | `coverage_insufficient` / eligible for a verdict (if dates and bases hold)                                                                                      |
| 14  | Outcome: pass tier plus others                      | tiers: `cost_pass`, `execution_infeasible`, `coverage_insufficient`                                                            | `continue_to_3b`, scope the pass tier only                                                                                                                      |
| 15  | Outcome: no pass, coverage gap                      | tiers: `execution_infeasible`, `execution_infeasible`, `coverage_insufficient`                                                 | `stop_coverage`, never `stop_execution_infeasible`                                                                                                              |
| 16  | Outcome: no pass, boundary                          | tiers: `execution_infeasible`, `execution_infeasible`, `cost_inconclusive`                                                     | `stop_cost_inconclusive`                                                                                                                                        |
| 17  | Source quality beats a passing tier (reviewer case) | `T1` is `cost_pass` at 250; 22 of 108 observations are `prefix_insufficient` (20.37%)                                          | `stop_source_quality`; the `T1` result is a diagnostic only. With 21 of 108 (19.44%) the study continues to the tier verdicts                                   |
| 18  | Integrity beats everything                          | one `range_unsupported` after 60 observations, all tiers passing so far                                                        | `stop_source_quality`, nothing sealed                                                                                                                           |
| 19  | Budget stop                                         | cumulative response bytes reach 512 MiB                                                                                        | `stop_budget`; no snapshot published; no verdict read                                                                                                           |
| 20  | T1-only pass                                        | only `T1` is `cost_pass`                                                                                                       | `continue_to_3b` with scope `[T1]`; the first 3B gate is whether `T1` holds the needed funding gaps                                                             |
| 21  | Innovation Zone column                              | a passing tier whose Bybit symbols include 2 with `innovation_zone = true`                                                     | the report lists both and states 20.2 bps for them; the gate is unchanged; the 3B check is mandatory                                                            |
| 22  | Variant sensitivity                                 | one tier: `ob200` lower median 25 (9 resolved), `ob500` lower median 37 (10 resolved)                                          | `variant_sensitive` flag; the gate still uses the pooled sample                                                                                                 |
| 23  | 404 falls back to `ob500`                           | `ob200` URL returns 404, `ob500` returns 206 with `Content-Range: bytes 0-2097151/90000000`                                    | the `ob500` book is used and labelled `ob500`; no `range_unsupported`                                                                                           |
| 24  | 404 on both                                         | `ob200` and `ob500` both 404                                                                                                   | `bybit_file_absent`; the observation stays planned; no substitute                                                                                               |
| 25  | `Range` ignored                                     | HTTP 200 to a `Range` request                                                                                                  | `range_unsupported`, body unread, run stopped                                                                                                                   |
| 26  | `Range` mismatched                                  | HTTP 206 with `Content-Range: bytes 100-2097151/...`                                                                           | `range_unsupported`, run stopped                                                                                                                                |
| 27  | Short file is fine                                  | HTTP 206 with `Content-Range: bytes 0-80000/80001`                                                                             | accepted as a complete file; the book rules decide                                                                                                              |
| 28  | Retriable errors are not `Range` errors             | HTTP 429 then 206 / HTTP 503 twice / `Retry-After: 7`                                                                          | `ok` after one retry / `transport_failed` / the wait is 7 s                                                                                                     |
| 29  | Access and other 4xx                                | HTTP 403 on a signed Hyperliquid request / HTTP 400 on a Bybit URL                                                             | `access_denied`, run stopped / `http_rejected`, not retried                                                                                                     |
| 30  | Hyperliquid freshness rule                          | pilot gaps p99 = 3.2 s / 7.2 s / 31 s                                                                                          | `L_hl` = 5,000 ms / 8,000 ms / `stop_source_quality`                                                                                                            |
| 31  | Freshness set before costs                          | a run that computes a pilot `x` before `L_hl` is pinned                                                                        | refused                                                                                                                                                         |
| 32  | Unit map, matching units                            | `kPEPE` (1,000 tokens) against `1000PEPEUSDT` (1,000 tokens)                                                                   | unit ratio 1: eligible                                                                                                                                          |
| 33  | Unit map, other units                               | a Hyperliquid coin of 1 token against a Bybit contract of 1,000 tokens                                                         | `unit_ratio_not_one`: counted, never sampled                                                                                                                    |
| 34  | No map row                                          | a base with a name match and no row in the pinned map                                                                          | `identity_unverifiable`: counted, never sampled                                                                                                                 |
| 35  | Ambiguous base                                      | `PEPEUSDT` and `1000PEPEUSDT` both live                                                                                        | `identity_ambiguous`                                                                                                                                            |
| 36  | Price ratio is a flag only                          | mid ratio 1.2 after the unit ratio, identity map row present                                                                   | `price_ratio_flag = true`; identity status unchanged; the observation is resolved; no ratio field in any output                                                 |
| 37  | Similar prices prove nothing                        | two different assets with mid ratio 1.001 and no map row                                                                       | `identity_unverifiable`, not sampled                                                                                                                            |
| 38  | Forbidden computations                              | a result schema with any of `return`, `funding`, `basis`, `mid_change`, `q`, `mid`, `best_bid`, `best_ask`, `cost`, `proceeds` | schema validation fails                                                                                                                                         |
| 39  | Date guard, Step B                                  | requests for 2024-09-30, 2025-10-01, 2026-09-29                                                                                | all refused before any call                                                                                                                                     |
| 40  | Date guard, Step A                                  | requests for 2023-01-31, 2025-10-01, 2026-09-29                                                                                | all refused; 2023-02-01 and 2025-09-30 allowed                                                                                                                  |
| 41  | Step A never downloads                              | a call to any `GetObject` or `Range` method on the Step A client                                                               | raises; no such method exists                                                                                                                                   |
| 42  | Stale book                                          | last Bybit message `ts` 5,000 ms before `T` / 5,001 ms                                                                         | `ok` / `book_stale`                                                                                                                                             |
| 43  | Never reading ahead                                 | a delta at `ts = T` and one at `ts = T + 1`                                                                                    | neither is applied                                                                                                                                              |
| 44  | Prefix too short                                    | a prefix that ends with `ts < T` and no file end                                                                               | `prefix_insufficient`, no second request                                                                                                                        |
| 45  | First message not a snapshot                        | prefix starting with a delta                                                                                                   | `book_broken`                                                                                                                                                   |
| 46  | Hash selection of dates                             | eligible dates 2024-10-15, 10-16, 10-17: digests `6949...`, `5e37...`, `6753...`                                               | 2024-10-16 is chosen                                                                                                                                            |
| 47  | Hash selection of pairs                             | bases AAA, BBB, CCC on 2024-10-15: digests `e664...`, `8747...`, `a241...`                                                     | order BBB, CCC, AAA; the first 3 are taken                                                                                                                      |
| 48  | No replacement                                      | one chosen pair's Hyperliquid object is 404                                                                                    | `hl_object_absent`, the observation stays planned, no substitute                                                                                                |
| 49  | Tier split                                          | 9 bases with sizes 1 to 9                                                                                                      | `T3 = {1,2,3}`, `T2 = {4,5,6}`, `T1 = {7,8,9}`                                                                                                                  |
| 50  | Untiered                                            | a base with 9 usable dates                                                                                                     | `untiered`, never sampled                                                                                                                                       |
| 51  | Archive coverage                                    | 292 of 365 usable / 291 of 365                                                                                                 | `archive_covered` / `archive_partial` (if the other criteria hold)                                                                                              |
| 52  | Longest gap                                         | a 15-day unusable run in an otherwise full year                                                                                | not `archive_covered`                                                                                                                                           |
| 53  | First observed, reliable                            | first listing after 14 usable dates                                                                                            | reliable candidate                                                                                                                                              |
| 54  | First observed, left-censored                       | first listing on the archive's first usable date                                                                               | `left_censored`, not a launch                                                                                                                                   |
| 55  | I22 unknown versus negative                         | 35 reliable and 10 unreliable candidates, minimum 40                                                                           | `i22_unknown_archive`; 35 and 4 gives `i22_insufficient_events`                                                                                                 |
| 56  | Holdout privacy                                     | any report field with a holdout coin name or a holdout date                                                                    | schema validation fails                                                                                                                                         |
| 57  | Prior-root budget                                   | prior root used 400 attempts and 100 MiB                                                                                       | the new run's limits are the totals minus 400 and 100 MiB                                                                                                       |
| 58  | Prior root with other rules                         | prior root's `rules` differ                                                                                                    | refused, or its receipts not reused                                                                                                                             |
| 59  | Credential hygiene                                  | a fake secret inside an injected error text                                                                                    | absent from `run.json`, the ledger and every receipt                                                                                                            |
| 60  | Missing credentials                                 | no credential in the environment                                                                                               | `credential_missing`; zero network calls                                                                                                                        |
| 61  | Attempt before HTTP                                 | process killed after the ledger commit                                                                                         | the attempt stays charged on resume                                                                                                                             |
| 62  | Attempts: plain call                                | one `ListObjectsV2` against the local fake server                                                                              | the server sees 1 request; the journal has 1 row                                                                                                                |
| 63  | Attempts: HTTP 500                                  | the fake server answers 500 once, then 200                                                                                     | the server sees 2 requests; the journal has 2 rows (our loop's second attempt)                                                                                  |
| 64  | Attempts: region redirect                           | the fake server answers 301 with another region                                                                                | the redirect is not followed; server requests equal journal rows; the result is `http_rejected`                                                                 |
| 65  | Attempts: environment override                      | `AWS_MAX_ATTEMPTS=10`, the fake server answers 500 every time                                                                  | exactly 2 sends for the request, then `transport_failed`; the variable changes nothing                                                                          |
| 66  | The SDK client sends nothing                        | build the client and presign a listing and a ranged `GetObject` with the network blocked                                       | no HTTP call is made by the SDK; the presigned URL is not written to any file                                                                                   |
| 67  | Oversized listing page is cut                       | a fake page of 3 MiB with `L = 2 MiB`                                                                                          | exactly 2 MiB read and charged, not parsed, `response_too_large`, no retry, run `listing_incomplete`                                                            |
| 68  | Large legitimate page                               | a fake page of 1,174,037 bytes (1,000 distinct keys of 1,024 bytes) with 100 MiB already charged                               | read in full, parsed, charged 1,174,037 bytes (plus block rounding), no stop                                                                                    |
| 69  | Ceiling is never passed                             | 254.9 MiB charged, then a 1,174,037-byte page                                                                                  | the 1 KiB block reservations stop the read at the 256 MiB cap; the attempt ends `listing_incomplete` with reason `byte_budget`; the total never exceeds 256 MiB |
| 70  | Presigned URL stays out of errors                   | the fake server drops the connection; the transport exception text holds the URL                                               | the recorded error has class and status only; the fake key id and signature are in no file                                                                      |

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

My position is given for each. None is decided. Points settled by the second review are
in the revision note at the top and are not repeated here, except where a limit remains.

1. **Window if the archive does not cover the discovery year.** The registry says the
   test year is `reserved` (HYP-031 only) and the program plan says unassigned. Position:
   do not extend automatically; list nothing outside 2023-02-01 to 2025-09-30. Should
   this contract allow a coverage-only listing pass of the test year (dates and key
   counts, no sizes) if discovery is `archive_partial`, or must that wait for an
   amendment? Separately, which of the two statements about the test year should the
   registry carry?
2. **Gate size and the tier rule.** The screen says one median over the grid at the
   working size and a stop above 31.0. This draft gates at `S* = 250`, passes the family
   if any one of three tiers is `cost_pass`, restricts the protocol to passing tiers, and
   makes a boundary case `cost_inconclusive` instead of a stop or a pass. Both the tier
   rule and the bootstrap interval are departures from the plain median. Alternatives: a
   pooled median with equal tier weights, or gating at 1,000.
3. **One instant, settlement-adjacent (`T = 00:00:30`).** The only cheap instant for
   Bybit's day files. It says nothing about non-settlement hours. Is it enough for
   I14 and I15, and acceptable as input to I21's general question? An extra stratum costs
   up to a large share of a day file per observation.
4. **`Range` on `quote-saver.bycsi.com` and on the S3 bucket, and the byte model.** Range
   support is not established anywhere in the repository. If it fails, the contract stops
   closed. Fallback options: skip files above a HEAD-sized limit, or restrict to small
   symbols. The range caps (1 MiB, 2 MiB) and 108 observations come from that model.
5. **Innovation Zone.** Revision 2 keeps 31.0 as the base filter and adds a column and a
   mandatory pre-3B check. The limit that remains: the catalog pinned now has no zone
   flag, so until its source is fixed the column is `unknown` and a tier could pass at 31.0
   while its zone pairs need 20.2. Is a check before 3B enough, or should zone pairs be a
   separate tier now?
6. **Tiers by Hyperliquid object size, and the sample is not the I14 sample.** Size is an
   activity proxy only. The pairs where the funding gap occurred need a funding read to
   select, which this contract forbids. So a pass is a necessary condition for the typical
   coin of the passing tier and says little about the coins of the gap. Revision 2 makes
   the first 3B gate a count of the needed gaps inside the passing tiers. Is that
   acceptable, or should the contract add a funding-based stratum under its own, later
   amendment?
7. **Hyperliquid file shape and level limit.** The archive's maximum level count and
   message fields are unverified. Revision 2 treats a short side at the limit as
   `depth_unobserved` (a data gap), and fixes the limit from the pilot. The limit that
   remains: if the pilot cannot establish `L_max`, every Hyperliquid side counts as at its
   limit, which pushes outcomes toward `stop_coverage`.
8. **Credentials and account.** The signer is settled: `boto3` with SDK retries off and
   every send journaled (section 8). What remains is the owner's decision: whether to
   provide an AWS key, under which account, with which spending cap. No key is needed now,
   and none is assumed. Before any Step A request the owner receives the concrete request
   list and budget and gives a separate go.
9. **I22 thresholds and the Bybit catalog pin.** I mirrored the catalyst floors (40 and
   30 launches, 8 weeks) where the screen gave only 40. The pin uses the catalyst run's
   instrument catalog, which sits outside Git. Alternative: the HYP-031 discovery index.
   Also, are hour 0 presence plus an hourly refinement enough to date a launch, given
   Stage 2B must confirm it anyway?
10. **Thresholds.** 80% usable dates, 14-day gap, 11 of 12 months, two thirds resolved,
    8 dates and 8 bases per tier, 36 per tier, 20% `prefix_insufficient`, 10,000
    bootstrap draws. All are mine. With 12 date clusters the interval is wide: is
    `cost_inconclusive` going to swallow most outcomes, and if so is it better to buy more
    dates than more pairs per date?
11. **`ob200` against `ob500`.** The selection policy is frozen (`ob200` first) and the
    report splits the variants. Remaining question: force `ob200` only, which loses
    observations where only `ob500` exists, or keep the pooled gate with a diagnostic flag?
12. **Book rules.** No `u + 1` adjacency, `ts` rather than engine `cts`, 5 s Bybit
    staleness. The B2 pilot supports the first two on 12 files. Hyperliquid's limit now
    follows the pilot's cadence by a fixed rule; is the rule (`max` of 5 s and the p99 gap)
    the right one, and is 30 s the right ceiling?
