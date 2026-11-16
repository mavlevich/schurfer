# Catalyst feasibility v1 result, 2026-11-16

**Study outcome: `stop_direction_a`.** No bucket is `feasible`, so by the registered rule
direction A does not continue to a laboratory protocol. Two buckets (`A2`, `A4d`) are
`lab_only`, which goes forward only on the owner's separate decision. The new-perp launch
count for `I25` passes its planned threshold. This was a count of events and timestamps
under the fixed [contract](catalyst-feasibility-v1.md), `catalyst-feasibility-v1`. It read
no price, trade, spread, depth, funding rate, open interest or return. It was not a test,
an exam or a strategy registration.

## Chronology and evidence

- **Contract, code and registry row.** Merged as #600 (`3c974cc`) after the colleague's
  reviews. The registry row was in main before the first request.
- **Five fetch runs.** The first four stopped. Each stop was written up and fixed through
  a reviewed PR before any count was read; every run passed the earlier stopped roots with
  `--prior-root`, so all requests count against one budget.

| Run | Revision  | Stopped on                                                                   | Fix (PR)                                                             |
| --- | --------- | ---------------------------------------------------------------------------- | -------------------------------------------------------------------- |
| 1   | `3c974cc` | page 24 of Bybit delistings, `schema_mismatch`, cause not visible            | key-only `failure` record, articles without a machine time (#602)    |
| 2   | `dcb2475` | same page, more than 10% untimed articles                                    | untimed era stop and coverage rule (#603)                            |
| 3   | `b835cd8` | first page of Bybit `product_updates`, no `publishTime` key at all           | series not requested, `A4f` and `A4r` not reconstructible (#604)     |
| 4   | `893384b` | HTTP 429 three times on page 45 of `binance:49`; Upbit's first page also 429 | pauses between retries, reuse of read pages, unreachable rule (#605) |
| 5   | `48676de` | complete                                                                     | none                                                                 |

- **Fetch (run 5)** ran on the owner's go from a clean detached worktree at `48676de`. It
  continued from 152 pages read by earlier runs, read the remaining 58 requests, and ended
  complete. **287 requests** in all of 3,000 and **3,594,593** reserved response bytes of
  64 MiB.
  - Reused pages: `binance:48` 61, `binance:161` 19, `binance:49` 44, Bybit delistings 24,
    instrument catalogs 4. The roots of the first two runs were skipped as parse-incompatible.
  - Run binding `c477aebd...`, outside Git under `schurfer-runs/` next to the main checkout.
- **Report** ran on the owner's go from the same revision at 2026-11-16 14:00 UTC.
  - Kept as a deterministic [gzip](evidence/catalyst-feasibility-v1/report.json.gz) with a
    [manifest](evidence/catalyst-feasibility-v1/manifest.json) and the
    [fetch summary](evidence/catalyst-feasibility-v1/fetch-summary.json). Report SHA-256
    `39f06357...`.
  - The discovery year's event list (222 events) is in the report. The backward
    holdout's event list is sealed outside Git (`holdout-events.json`, 17,187 bytes); only
    its hash `52baf153df9af11cef24d996388cd7e356052021354f94ebac37ef0472ccb8ee` is
    here. No holdout title, ticker or date was read for this record.

## Buckets

Minimums are fixed in the contract and were not tuned: discovery 40 clusters, 40
announcements, 20 bases, 8 ISO weeks; holdout 30, 30, 15, 8; timed share at least 90%;
ambiguous share at most 10%. In every bucket and window the timed share is 100% and the
ambiguous share is 0%, so only counts, coverage and reconstructibility decide.

### Discovery year (2024-10-01 to 2025-09-30)

| Bucket | Verdict               | Clusters | Announcements | Bases | ISO weeks | Note                                                    |
| ------ | --------------------- | -------: | ------------: | ----: | --------: | ------------------------------------------------------- |
| `A1`   | `insufficient_events` |       15 |            11 |    15 |        11 | Binance only, Upbit unreachable                         |
| `A2`   | `lab_only`            |       71 |            59 |    71 |        36 | passes every discovery minimum                          |
| `A3`   | `insufficient_events` |       53 |            21 |    38 |        19 | 42 of 53 events from the free-text parser, Binance only |
| `A4d`  | `lab_only`            |       83 |            83 |    83 |        37 | passes every discovery minimum                          |
| `A4f`  | `not_reconstructible` |        0 |             0 |     0 |         0 | no machine publication time in the feed                 |
| `A4r`  | `not_reconstructible` |        0 |             0 |     0 |         0 | no machine publication time in the feed                 |

Why results are lost before they count, per base (matched, no candidate, no live perp, no base):
`A1` 15 / 9 / 11 / 0, `A2` 71 / 12 / 67 / 6, `A3` 53 / 27 / 6 / 0, `A4d` 83 / 3 / 6 / 1. A
listing whose base had no Bybit USDT perp, or one launched less than 24 hours earlier, does
not count by design. For `A2`, 67 results fall there against 71 matches.

### Backward holdout (2023-02-01 to 2024-09-15), aggregate counts only

| Bucket | Clusters | Announcements | Bases | ISO weeks | Articles | Gap or note                                                     |
| ------ | -------: | ------------: | ----: | --------: | -------: | --------------------------------------------------------------- |
| `A1`   |       13 |            12 |    13 |        10 |       46 | Binance only                                                    |
| `A2`   |        2 |             2 |     2 |         2 |      136 | 134 articles with no base read from the title (see below)       |
| `A3`   |       61 |            21 |    52 |        18 |       55 | Binance only; 49 of 61 events from the free-text parser         |
| `A4d`  |       13 |            11 |    13 |         8 |       13 | feed read back only to 2024-05-27, `uncovered_sources: [bybit]` |
| `A4f`  |        0 |             0 |     0 |         0 |        0 | not reconstructible                                             |
| `A4r`  |        0 |             0 |     0 |         0 |        0 | not reconstructible                                             |

Reading the table against the verdicts:

- **`A2` is `lab_only` because of the holdout count**, not the discovery year. Of its 136
  holdout articles, 134 gave no base, against 6 of 106 in the discovery year. The code reads
  the base of an `A2` article only from a `...USDT` pair in the title, and the contract
  gives no free-text fallback for this bucket. That the holdout titles mostly lack such a
  pair is a likely explanation, but it is not verified: the holdout titles were not looked
  at. The verdict stands as registered.
- **`A4d` is `lab_only` on coverage and would fail its holdout minimums anyway.** The Bybit
  delistings feed carries machine times only from 2024-05-27 on, so 112 of the holdout's 593
  days (18.9%) were read. In them there are 13 clusters, 13 bases and 8 weeks, below the
  holdout minimums of 30 and 15.
- **`A1` and `A3` rest on Binance alone, so they are conservative.** Upbit was unreachable
  (below). Upbit lists many assets and could have added events: with it, either bucket might
  have reached the discovery minimums. `A1` has 15 clusters and 11 announcements against 40,
  and `A3` has 21 announcements against 40, without it. Their verdict `insufficient_events`
  may be caused by the missing source alone, and this study cannot say.
- **`A4f` and `A4r`** are decided without a count. The first page of Bybit's
  `product_updates` feed has no `publishTime` key at all. The author-filled
  `dateTimestamp` is not a fallback. The conclusion rests on that first page only; older
  pages were not read.

## Source quality

| Source and window | Articles | Whole-minute share (gate at most 20%) | Id order discordant, worst catalog (gate at most 5%) | Author time differs by over 60 s | Passes |
| ----------------- | -------: | ------------------------------------: | ---------------------------------------------------: | -------------------------------: | ------ |
| Binance discovery |      993 |                                  1.4% |                                                0.11% |                   not applicable | yes    |
| Binance holdout   |    1,493 |                                  0.5% |                                                0.03% |                   not applicable | yes    |
| Bybit discovery   |      151 |                                 10.6% |                                         unobservable |                            96.0% | yes    |
| Bybit holdout     |       29 |                                  6.9% |                                         unobservable |                            89.7% | yes    |

- Bybit's `dateTimestamp` differs from its machine `publishTime` by more than 60 s in 96.0%
  and 89.7% of articles, which supports never using it as a time.
- Edit checks are unobservable for Binance and Bybit (one machine time per article), and the
  order check is unobservable for Bybit (no numeric id). They gate nothing, as the contract
  says. Articles without a machine time inside the read range: 0 for each source (the 13 on the
  last Bybit delistings page lie beyond the coverage and are not counted).
- Upbit has no quality line: its series was never read.

## Series coverage

| Series                      | Read to                                                   |
| --------------------------- | --------------------------------------------------------- |
| `binance:48`                | end (time boundary, 61 pages)                             |
| `binance:161`               | end (time boundary, 19 pages)                             |
| `binance:49`                | end (time boundary, 99 pages)                             |
| `bybit_ann:delistings`      | untimed era, covered from 2024-05-27T06:26:18Z (24 pages) |
| `bybit_inst:*`              | end (Trading, Delivering, Closed)                         |
| `upbit:notices`             | **unreachable**                                           |
| `bybit_ann:product_updates` | not requested, no machine publication time                |
| Coinbase                    | not requested, not reconstructible                        |

**Upbit.** The first page failed all three attempts, with HTTP 429, 400 and 429, the last
two after pauses of 10 s and 30 s. The 400 means this is not only a rate limit. The cause is
unknown: response bodies are not kept, no page was opened by hand, and the endpoint is
undocumented. The series is recorded as `unreachable`, so `A1` and `A3` are counted from
Binance only and the report names `unreachable_sources: [upbit]`.

## New perp launches (`I25`)

Distinct linear USDT symbols from Bybit's `instruments-info` catalog (`Trading`,
`Delivering` and `Closed`, 714 symbols in all) by `launchTime`:

| Window    | Launches | Bases | ISO weeks with a launch |
| --------- | -------: | ----: | ----------------------: |
| Discovery |      263 |   263 |                      53 |
| Holdout   |      254 |   254 |                      74 |

The planned threshold for moving `I25` to a laboratory protocol is at least 100 launches and
at least 20 weeks in the discovery year. **It passes, 263 and 53.** Limits that the protocol
must keep:

- The count measures the coverage of Bybit's API catalog, not a verified full history. The
  public trade archive was not reconciled with it.
- `launchTime` is the field the contract uses, and it is not shown to be the moment trading
  opened. The protocol must verify the lifecycle meaning of the field against first trades
  before any return is read.
- Bases are matched at ticker level and a relisted or reused ticker would look like one
  base. Every launch is its own base here (263 of 263).
- Identity of each launch must be verified before any price, as for the other buckets.

## Limits of this count

- Identity is ticker-level after multiplier stripping, so the counts are an upper bound.
- A3 reads most of its bases with the free-text parser (42 of 53 discovery events, 49 of 61
  holdout events). The report marks them. Treat A3 counts with that exposure in mind.
- Feeds move while they are read, and some pages were read in different runs. Articles are
  counted once by id. A withdrawn article at a seam between runs is not recoverable.
- Delivery lag is not measured: sources are `timed`, never `deliverable`.

## Decisions for the owner

1. **`I25`.** Move to a laboratory protocol on the discovery year? The count supports it.
   The protocol is a new design with its own contract, not part of this study.
2. **`A2` and `A4d`, `lab_only`.** The registered rule is `stop_direction_a`, and a
   `lab_only` bucket goes forward only on the owner's decision. For `A2` the discovery year
   is strong (71 clusters) and the holdout count is the problem. The contract allows one
   disclosed corrective recount for a parser defect that must not change the minimums. Using
   it means looking at the structure of holdout titles, which this study did not do. That is
   a separate decision.
3. **Upbit.** Leave it unreachable, or write an amendment before any further request. The
   400 suggests the request itself may need a different definition.

No registered minimum, window, bucket or rule was changed after a count was read.
