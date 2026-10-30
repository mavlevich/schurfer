# Seen windows registry

Status: **first version, 2026-10-30.** It lists which data windows the program has
already looked at, and how much of them was seen, so a later study can tell whether a
window is still clean for confirmation. It decides nothing by itself. New studies add a
row before their first fetch; the [discovery ledger](discovery-ledger.md) keeps the
per-hypothesis detail.

Levels of exposure, from strongest to weakest:

- **outcomes:** returns, labels or verdict statistics were read;
- **features:** values of candidate inputs were read (rates, distributions, extremes);
- **coverage:** only counts, timestamps, file sizes or availability were read.

A window seen at the outcomes or features level is discovery-only for any hypothesis
that uses the same inputs. A coverage-only window stays usable for confirmation.

## Bybit public history

| Window                      | Data                                                                       | Seen by                                                      | Level    |
| --------------------------- | -------------------------------------------------------------------------- | ------------------------------------------------------------ | -------- |
| 2024-10-01 to 2026-09-28    | Funding settlements of live and delisted USDT perpetuals: rates, intervals | Post-funding feasibility check, 2026-10-25                   | features |
| 2024-10-01 to 2026-09-28    | Spot and linear 4 h klines and funding, live spot-perp pairs               | [Carry economics screen](bybit-carry-economics-screen-v1.md) | outcomes |
| 2024-10-01 to 2025-09-30    | HYP-031 discovery (funding windows, 1 m klines, trade archive)             | Registered, not opened                                       | reserved |
| 2025-10-01 to 2026-09-28    | HYP-031 test year                                                          | Registered, not opened                                       | reserved |
| 2024-09-16 to 2024-09-22    | HYP-031 fetcher smoke: event and window counts, request rates              | [HYP-031 runbook](bybit-post-funding-minute-v1.md)           | coverage |
| 2020-03-25 to 2026-10-28    | Trade archive file listings per symbol; order book archive file sizes      | This registry's checks, 2026-10-30                           | coverage |
| 2021-06 to 2025-03, samples | Row counts of klines, open interest (1 h, 5 min) and funding, ~60 symbols  | This registry's checks, 2026-10-30                           | coverage |

## Own capture (Schurfer collectors and cohorts)

| Window                          | What                                                                     | Level                       |
| ------------------------------- | ------------------------------------------------------------------------ | --------------------------- |
| 2026-07-22 to 2026-09-28        | Pump, source-lead, momentum-flow and radar studies (HYP-011 to HYP-030)  | outcomes                    |
| 2026-08-10 to 2026-09-14        | Monster-precursor work                                                   | outcomes                    |
| 2026-08-24T11:10Z to 2026-09-28 | Pump-short maker-entry prospective cohort: episode and venue counts only | coverage (2026-10-30)       |
| 2026-08-25 to 2026-09-28        | Liquidation cascade episode counts only (read 2026-09-06 on 12 days)     | coverage, outcomes to 09-06 |
| 2026-08-14 onward               | HYP-015 paper probes                                                     | sealed until 2026-11-04     |
| From 2026-09-29                 | Every venue, every source                                                | HYP-012 v2 blind window     |

The 2026-07 to 2026-09 own-capture period is the most reused data the program has.
Nothing new is confirmed on it.

## Still clean for confirmation

- **Bybit public history before 2024-09-16,** for any hypothesis: nothing beyond
  coverage has been read. The strategy uses 2023-02-01 to 2024-09-15 as its backward
  holdout; 2022 stays unread. It is "untouched" for the program only; the market events in
  it are public knowledge, so a hypothesis built from memory of a named event (a crash,
  a famous squeeze) does not count as confirmed on it.
- **Forward data after HYP-012 v2 is terminal,** minus v2's closed windows.
- **The HYP-031 test year,** for HYP-031 only.

## Coverage facts from the 2026-10-30 checks

Counts only, no values read.

- **Trade archive** (public.bybit.com/trading): 1130 USDT symbols, earliest file
  2020-03-25. Of them, 317 are not live now. Symbols with files in a month:

  | Month   | Symbols |
  | ------- | ------- |
  | 2022-01 | 116     |
  | 2022-07 | 175     |
  | 2023-01 | 182     |
  | 2023-07 | 226     |
  | 2024-01 | 308     |
  | 2024-07 | 397     |
  | 2026-09 | 791     |

  10 symbols miss more than 5% of the days in their span.

- **V5 API for delisted symbols:**
  - delisted with a last archive file in 2023 or 2024 (47 symbols): klines, open
    interest and funding all returned data;
  - delisted in 2022 (9 symbols): klines and funding for 4, open interest for 6.
    So 2022 has a survivorship gap that the archive alone does not close.
- **Open interest:**
  - 1 h and 5 min history exist from 2021 (BTCUSDT, none at 2020-06);
  - 1 h is the planning default: about a tenth of the 5 min request count.
- **Order book archive** (quote-saver.bycsi.com; that it is Bybit's official source is
  to be confirmed before use):
  - daily `ob500`, later `ob200`, snapshots for linear perpetuals, from 2023-01-18,
    including symbols delisted since;
  - files run from 1 MB to 460 MB per symbol-day.

  So it is used per event window and reduced to top-of-book and depth features, never
  stored whole.
