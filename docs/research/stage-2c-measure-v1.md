# Stage 2C measurement v1: ETH funding (I35) and dated-futures basis (I26)

Status: **draft contract for the colleague's one design review. It authorizes no request
and no download.** Every network step below needs the owner's separate go, and the code
that makes the requests goes to its own review after this design is settled.

Study id: `stage-2c-measure-v1`. No HYP id is assigned. One is assigned only if a
protocol follows a pass (section 7).

This is the first measurement behind the paper screens in
[`stage-2c-paper-screens-v1.md`](stage-2c-paper-screens-v1.md), which left I35 and I26
at `needs_measurement`. It reads two facts and nothing else: the realized funding of
ETHUSDT and the entry basis of Bybit's dated futures on BTC and ETH, both inside the
discovery year. Both are public Bybit data. No key, no account and no AWS are involved.

## 1. Why these two, and why now

At the owner's size (USD 5,000) the carry ideas are a permission to build a process, not
income: the screens put the whole idea between USD 46 below idle USDT and USD 177 above
it a year. They are still the cheapest ideas left that do not need a statistical proof of
an edge: I26's return is fixed at entry, and I35 needs only a funding level. The
measurement costs about 30 requests, and each answer either stops an idea for good or
produces a protocol worth writing.

The account facts that blocked this were checked on 2026-11-22 (screens document, last
section): stETH counts as collateral at 0.98, there is no interest on idle margin, and the
owner's own eligibility for any opt-in route stays unverified. The measurement therefore
uses the **no-interest** reading of the screens' required levels.

## 2. Fixed inputs

- **Window:** the discovery year, 2024-10-01 00:00 UTC to 2025-09-30 23:59 UTC. The
  backward holdout (2023-02-01 to 2024-09-15) is not touched. Nothing from 2026-09-29
  onward is read, so the HYP-012 v2 blind window (R1) is not entered.
- **Capital and hurdle:** USD 5,000 of reserved capital, hurdle 5% a year on all of it,
  as in the screens. I35 uses the 8% target (5% plus the 3 point LST premium).
- **Data:** Bybit public v5 endpoints only: funding history, klines, instruments-info.
  Settled values only, never the predicted rate.
- **No return of any trade is computed.** The decision quantities are a mean funding
  level and entry basis levels, not strategy returns.

## 3. Request budget and stops

| Part                      | Requests | What                                                                              |
| ------------------------- | -------: | --------------------------------------------------------------------------------- |
| I35 funding               |        6 | ETHUSDT funding history, 200 rows per page, 1,095 rows                            |
| I35 sanity klines         |        3 | ETHUSDT 4 h klines, 1,000 bars per page, for the year                             |
| I26 coverage              |   0 to 4 | local instruments-info catalog first; a request only if absent                    |
| I26 dated-contract klines | up to 12 | 4 h klines of each BTC and ETH dated contract with expiry in or around the window |
| I26 spot or index klines  |        6 | 4 h klines of BTC and ETH spot, 3 pages each                                      |
| Retry reserve             |       20 | at most 3 attempts per page                                                       |
| **Hard ceiling**          |  **100** | the run stops when reached                                                        |

Stops, each recorded and none retried by hand:

- any request returning anything but a complete page after 3 attempts: `fetch_error`, and
  the run stops without a verdict;
- a response larger than the stated page bound: cut, not parsed;
- a gap in the funding series longer than 8 hours inside the window: `funding_gap`, and
  the I35 part stops without a verdict;
- the dated-contract probe (one request for one expired contract) returning no bars:
  `dated_history_unavailable`, and the I26 part stops without a verdict. Whether Bybit
  keeps klines of expired dated contracts is not known; this is the first thing the run
  learns.

Every request is written to a journal with the URL, the status, the byte count and the
SHA-256 of the body, before any value is read from it.

## 4. I35: decision rule fixed now

`f_mean` is the sum of settled ETHUSDT funding rates over the window divided by the
length of the window in years (the annualized mean funding on notional, the short
receiving). `f_90min` is the lowest annualized mean over any 90 day rolling window.

The required net funding `f` comes from the I35 table of the screens (H = 8%, no interest
on the leftover): **14.50%** at the conservative corner (alpha 0.50), **8.93%** at the
middle corner (alpha 0.70), **5.64%** at the favorable corner without interest
(alpha 0.90).

| Outcome                 | Condition                                                                                                                                                          |
| ----------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `i35_pass_protocol`     | `f_mean` at least 14.50%, and `f_90min` above 0                                                                                                                    |
| `i35_fail`              | `f_mean` below 5.64%                                                                                                                                               |
| `i35_account_dependent` | anything between: the idea then depends on alpha, which depends on the owner's collateral setup, so it stays `needs_measurement` of the account, not of the market |

A `i35_pass_protocol` permits writing a protocol, never an order. One contract in one
year is a stress check of the level, not a confirmation: no power claim is made, and the
LST discount history is a separate source this run does not read.

## 5. I26: decision rule fixed now

For each asset (BTC, ETH) and each dated contract whose cycle has an entry inside the
window, take the entry bar: the first 4 h bar at or after 90 days before expiry in which
the contract trades (if it listed later, its first bar, with its real days-to-expiry
recorded). Let `F` be that bar's close, `S` the spot close of the same bar and `d` the
days to expiry. The entry basis is `a = (F / S - 1) * 365 / d`, annualized on notional.

The required levels come from the I26 table of the screens, without interest on the
buffer: **11.82%** conservative, **6.62%** favorable (alpha 0.90, no interest).

| Outcome                | Condition, per asset                                                |
| ---------------------- | ------------------------------------------------------------------- |
| `i26_pass_protocol`    | at least half the cycles have `a` at least 11.82%, and none below 0 |
| `i26_fail`             | fewer than half the cycles have `a` at least 6.62%                  |
| `i26_regime_dependent` | anything between                                                    |

The idea passes only if both assets pass. With about four cycles per asset these are
counts, not statistics, and a pass says the basis was wide enough in that year, not that
it will be. The delivery price rule of USDT expiry contracts and the real settlement fee
(0 or 5 bps) are not read here, and the verdict states them as open.

## 6. Output and publication

One JSON report with the request journal summary, `f_mean`, `f_90min`, each cycle's
`(contract, entry bar, d, a)` and the outcomes above, with its SHA-256 recorded. Raw
bodies stay in a private run directory outside git. The report is published only after
the outcomes are computed; nothing is chosen after a look.

## 7. What each outcome does

- `i35_fail` and `i26_fail`: the idea is stopped and recorded, with no later rescue by a
  different corner or hurdle.
- `*_regime_dependent` or `*_account_dependent`: recorded as such, no protocol, and the
  next fact is named in the result (the owner's collateral setup, or a longer history).
- `*_pass_protocol`: a protocol draft is written and reviewed before any later read. No
  holdout is spent by this document.
- Any stop in section 3: the part is recorded as `insufficient_data` with the reason.

## 8. Review and gates

1. The colleague reviews this contract once (design). The owner waived a second round for
   wording-only changes.
2. The code PR reuses the existing Bybit fetchers in `new_perp_stage1.py` where it can,
   adds the journal and the report, and is reviewed before merge.
3. The owner gives a separate go before the first request. The run is one execution from
   the repository root at a pinned revision, and its report is the only output.

## 9. Implementation notes (code PR)

The code is `apps/analytics/schurfer_analytics/stage_2c_measure.py`. Where the code had to
choose, it chose as follows. None of these changes a level or a window in sections 2 to 5.

- **Own fetcher.** The I25 fetchers are built never to read a rate or a price, so they cannot
  serve here. The module has a small fetcher with the journal of section 3 and shares only
  `publish_once`.
- **Approval hash.** `plan` prints a go hash over the window, budget, thresholds, endpoints
  and symbols. `fetch` refuses to start unless the owner's approved hash equals it, and
  refuses a root inside the repository or a root that is not empty.
- **Catalog by request.** The dated-contract catalog is read with two requests
  (`instruments-info`, base BTC and ETH, status `Closed`), not from the local catalog, so
  the run needs no other state. More than two pages per asset stops the I26 part.
- **Entry bar.** One request per cycle asks for 4 h bars from the entry time to seven days
  later and takes the earliest. No bar in that span is a cycle with `a` unknown: it counts in
  the denominator and never passes. If the first cycle has no bar, or the API refuses the
  symbol, the I26 part stops as `dated_history_unavailable`; this probe runs before any spot
  request.
- **Sanity klines.** The three ETHUSDT 4 h pages only report coverage (bars and missing
  bars). They feed no decision and no price is published.
- **Idea verdict.** Outcomes are reported per asset. `idea_passes` is true only when both
  assets are `i26_pass_protocol`. An asset with no cycle is `i26_no_cycles`.
- **Request count.** A run with no retries makes 25 requests: 6 funding, 3 ETH klines, 2
  catalog, 8 entry (four cycles per asset) and 6 spot klines. The ceiling is 100.
