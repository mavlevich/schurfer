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

| Window                                             | Data                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            | Seen by                                                                                                                                                                                                                                                                                                                            | Level                      |
| -------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------- |
| 2024-10-01 to 2026-09-28                           | Funding settlements of live and delisted USDT perpetuals: rates, intervals                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      | Post-funding feasibility check, 2026-10-25                                                                                                                                                                                                                                                                                         | features                   |
| 2024-10-01 to 2026-09-28                           | Spot and linear 4 h klines and funding, live spot-perp pairs                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    | [Carry economics screen](bybit-carry-economics-screen-v1.md)                                                                                                                                                                                                                                                                       | outcomes                   |
| 2024-10-01 to 2025-09-30                           | HYP-031 discovery: funding windows and 1 m klines; native trade tape only in the minutes [s - 5 min, s + 5 min) around its event settlements (narrowed 2026-11-06 with HYP-032's fixed mask)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    | [Discovery read](bybit-post-funding-minute-v1.md) 2026-11-05 (fail)                                                                                                                                                                                                                                                                | outcomes                   |
| 2025-10-01 to 2026-09-28                           | HYP-031 test year                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               | Registered; unread (the minute variant closed at discovery)                                                                                                                                                                                                                                                                        | reserved                   |
| 2024-10-01 to 2025-09-30                           | HYP-032 power step 1: 1 h klines, 1 h open interest and funding, live and delisted USDT perpetuals; counts, funnels and the tape-scope symbol-day list                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          | [HYP-032 protocol](bybit-compressed-spring-v1.md), read 2026-11-06                                                                                                                                                                                                                                                                 | features                   |
| 2024-10-01 to 2025-09-30                           | HYP-032 tape scope: trade archive listings inside the window and sampled HEAD sizes                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             | [HYP-032 protocol](bybit-compressed-spring-v1.md), read 2026-11-06                                                                                                                                                                                                                                                                 | coverage                   |
| 2024-10-01 to 2025-09-30                           | HYP-032 power step 2: Bybit trade tape of the primary scope, per-minute taker aggregates outside the fixed mask, plus masked minutes around unreserved hour boundaries                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          | [HYP-032 protocol](bybit-compressed-spring-v1.md), read 2026-11-07                                                                                                                                                                                                                                                                 | features                   |
| 2024-10-16 to 2025-09-30                           | HYP-032 power step 4: H-hour returns (4, 12, 24 h) at random liquid instrument-hours and at random compressed hours against their price-state matched controls, never at a condition 4 event; standard deviations and design effects only, no mean; paths never within an hour of an event's 24 h path                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          | [HYP-032 protocol](bybit-compressed-spring-v1.md), read 2026-11-07                                                                                                                                                                                                                                                                 | outcomes (dispersion only) |
| 2024-10-01 to 2025-09-30                           | HYP-033 steps 1 and 2: first hourly impulses, funding sign, 2 h open interest change ranks, impulse-matched control pairs and pre-impulse 4 h volatility, from HYP-032's features snapshot; counts, pairs and MDE only, nothing after any decision time                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         | [HYP-033 protocol](bybit-squeeze-continuation-v1.md), read 2026-11-08                                                                                                                                                                                                                                                              | features                   |
| 2024-10-01 to 2025-09-30                           | HYP-034 fetch and steps 1 and 2: 60-minute premium index klines of the HYP-032 universe; dislocation and ordinary-hour counts, last-hour direction, z4-matched and (Amendment 1) z4- and z1-matched control pairs, balance of pre-t 1 h and 4 h returns, overlap components and pre-t 4 h volatility (hourly closes after 1,172 of the 1,957 events entered later units' volatility, spread only); no minute trade or net after any decision                                                                                                                                                                                                                                                                                                                                                                                                                                    | [HYP-034 protocol](bybit-premium-dislocation-v1.md), read 2026-11-08                                                                                                                                                                                                                                                               | features                   |
| 2024-10-01 to 2025-09-30                           | HYP-035 steps 1 and 2: daily 00:00 UTC cross-sections of 7-day BTC-relative log returns, ranks, top-decile units and same-day middle-rank controls matched on hourly sd and turnover bin, pre-t beta and 24 h returns, and the daily portfolio returns over the seven 24 h segments before each formation window, from HYP-032's features snapshot; nothing after a decision for that decision, but a segment can cover an earlier hold of the same instrument (2,652 of 9,706 events), so hourly proxies of part of the holds entered the dispersion estimate; no mean, net or minute return read                                                                                                                                                                                                                                                                              | [HYP-035 protocol](bybit-relative-strength-v1.md), read 2026-11-08                                                                                                                                                                                                                                                                 | outcomes (dispersion only) |
| 2024-10-01 to 2025-09-30                           | HYP-036 step 1: B, V and their ranks on HYP-032's tape, undefined reasons and the mask check, and the recount of B and V from the 20 sample days' raw trade files fetched again (about 0.2 GB); no event rate, coverage or MDE                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  | [HYP-036 protocol](bybit-absorbed-buying-v1.md), read 2026-11-08                                                                                                                                                                                                                                                                   | features                   |
| 2024-12-29 to 2025-01-26, 2025-06-22 to 2025-07-20 | HYP-036 step 2 pilot blocks: Bybit trade tape of the full pool (per-minute taker aggregates under HYP-032's fixed mask), events, same-time controls, balance and MDE from data before each t; no trade return computed                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          | [HYP-036 protocol](bybit-absorbed-buying-v1.md), read 2026-11-09 (stopped at the pilot gate)                                                                                                                                                                                                                                       | features                   |
| 2024-10-17 to 2025-09-30                           | HYP-036 step 3: Bybit trade tape of the full pool scope, counts, pairs and MDE; no trade return computed                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        | [HYP-036 protocol](bybit-absorbed-buying-v1.md), not run (v1 stopped at the pilot gate)                                                                                                                                                                                                                                            | features                   |
| 2024-12-29 to 2025-01-26, 2025-06-22 to 2025-07-20 | HYP-036 matching check: the pilot blocks' pre-t features again (same events, controls from the whole same-hour pool under one changed rule), coverage, balance incl. turnover, MDE; no trade return computed                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    | [HYP-036 matching check](bybit-absorbed-buying-v2-matching-check.md), read 2026-11-09 (fail)                                                                                                                                                                                                                                       | features                   |
| 2024-10-17 to 2025-09-30                           | Next-candidate planning: family 5 counts (pumps, open-interest turns, risk-set controls, balance, MDE) from 1 h klines and open interest of the HYP-036 pool; majors counts from the HYP-036 pilot blocks' tape (top 10); no trade return computed                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              | [Next-candidate note](next-candidate-feasibility-v1.md), read 2026-11-09                                                                                                                                                                                                                                                           | features                   |
| 2024-10-27 to 2024-12-08, 2025-03-23 to 2025-05-04 | Liquid top-10 flow pilot: Bybit trade tape of the monthly top 10 (per-minute taker aggregates under HYP-032's fixed mask), events, same-instrument controls within 7 days, balance, concentration and MDE from data before each t; no trade return computed                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     | [Liquid top-10 pilot](bybit-liquid-top10-pilot-v1.md), read 2026-11-09 (fail)                                                                                                                                                                                                                                                      | features                   |
| 2024-09-16 to 2024-09-22                           | HYP-031 fetcher smoke: event and window counts, request rates                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   | [HYP-031 runbook](bybit-post-funding-minute-v1.md)                                                                                                                                                                                                                                                                                 | coverage                   |
| 2020-03-25 to 2026-10-28                           | Trade archive file listings per symbol; order book archive file sizes                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           | This registry's checks, 2026-10-30                                                                                                                                                                                                                                                                                                 | coverage                   |
| 2021-06 to 2025-03, samples                        | Row counts of klines, open interest (1 h, 5 min) and funding, ~60 symbols                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       | This registry's checks, 2026-10-30                                                                                                                                                                                                                                                                                                 | coverage                   |
| 2024-10-01 to 2025-10-01 00:00                     | HYP-032 exploratory returns: H = 4, 12, 24 h hourly gross, funding and net at 30 bps for every hour passing conditions 1 to 3 and for the condition 1 to 4 events; BTC-adjusted, halves, without top 5; 6 variants                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              | HYP-032, exploratory reads of 2026-11-11 (another agent; figures as reported by it, scratch scripts deleted, run logs on the owner's machine, see the [evidence index](exploratory-reads-2026-11-11-evidence.md)), recorded in the [discovery ledger](discovery-ledger.md) and the [program plan](program-plan-2026-11.md)         | outcomes                   |
| 2024-10-01 to 2025-10-01 00:00                     | HYP-033 exploratory returns: all first impulses, event, f_only, g_only, neither at 4 h; event at 8 and 12 h; f_only splits by G, zw, funding depth and simultaneous impulses, horizons 2 to 12 h, 1 to 8 slot portfolios, a post-hoc wave filter; 1 m entry check on 59 events (60 Bybit 1 m kline requests); 33 variants                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       | HYP-033, exploratory reads of 2026-11-11 (another agent; figures as reported by it, scratch scripts deleted, run logs on the owner's machine, see the [evidence index](exploratory-reads-2026-11-11-evidence.md)), recorded in the [discovery ledger](discovery-ledger.md) and the [program plan](program-plan-2026-11.md)         | outcomes                   |
| 2024-10-01 to 2025-10-01 00:00                     | HYP-034 exploratory returns: primary and V1 events long at 4, 8, 12 h, V1 short at 4, 8, 12 h, simultaneity splits, portfolios, a lowered +0.30% short threshold; 1 m entry check on 60 events (60 Bybit 1 m kline requests); 16 variants                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       | HYP-034, exploratory reads of 2026-11-11 (another agent; figures as reported by it, scratch scripts deleted, run logs on the owner's machine, see the [evidence index](exploratory-reads-2026-11-11-evidence.md)), recorded in the [discovery ledger](discovery-ledger.md) and the [program plan](program-plan-2026-11.md)         | outcomes                   |
| 2024-09-30 23:00 to 2025-09-30 23:59               | HYP-031 before settlement, exploratory returns: long into settlement on the minute windows (r_prev and F_hat(t - 60 min) selections), on the full hourly grid of 4 h and 8 h contracts with F_hat(t - 1 h) from hourly premium, split by true and false triggers; 1 m premium index klines of 100 intervals (100 requests, seed 42) for an S1 and S2 pilot at d = 30, 15, 10, 5 min; 10 variants                                                                                                                                                                                                                                                                                                                                                                                                                                                                                | HYP-031 family, exploratory reads of 2026-11-11 (another agent; figures as reported by it, scratch scripts deleted, run logs on the owner's machine, see the [evidence index](exploratory-reads-2026-11-11-evidence.md)), recorded in the [discovery ledger](discovery-ledger.md) and the [program plan](program-plan-2026-11.md)  | outcomes                   |
| 2024-10-16 to 2025-09-11, 8 settlements            | HYP-031 seconds: public trade archive files of 8 settlements (21 daily files), ticks [t - 5 s, t + 300 s], returns from t + 5 s and t + 10 s                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    | HYP-031 seconds, exploratory reads of 2026-11-11 (another agent; figures as reported by it, scratch scripts deleted, run logs on the owner's machine, see the [evidence index](exploratory-reads-2026-11-11-evidence.md)), recorded in the [discovery ledger](discovery-ledger.md) and the [program plan](program-plan-2026-11.md) | outcomes                   |
| 2024-10-01 to 2025-09-30 23:59:59.999              | Pump long, post-pump short and maker entry lab series v1 to v3: hourly returns at H = 2, 4, 8 h with actual funding, taker 30 bps and maker 15 bps, from the local features (HYP-032 run), premium (HYP-034) and minute windows (HYP-031 discovery) snapshots; no network request; 54 configurations                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            | Pump long / post-pump short lab series, exploratory reads of 2026-11-11/12 (another agent; scripts and results kept with digests, see the [evidence index](exploratory-reads-2026-11-11-evidence.md)), recorded in the [discovery ledger](discovery-ledger.md)                                                                     | outcomes                   |
| 2024-09-30 to 2025-09-30                           | B1 feasibility fetches: 60-minute premium index klines (open, high, low, close) of the 672 instruments of the pinned HYP-031 discovery index (5,703 requests); then 1-minute premium index klines of the superset intervals (an hourly premium low at or below -0.20%) and a 2,000-interval sample drawn without any rate (count printed before that fetch); settled rates of those intervals for S1 only; done 2026-11-12 (hourly 4,620 responses, minutes 54,909 intervals). Amendment 2 adds a third fetch: 1-minute premium index klines of the event-superset intervals (passing the premium-free S2 filters at some d) that the second fetch did not request, about 35,200 intervals, chosen without any premium or rate; no perpetual price, kline, trade or return. The premium inside [t - d, t] overlaps the would-be hold: laboratory exposure on the discovery year | [B1 design](bybit-presettlement-feasibility-v1.md), fetch from 2026-11-12                                                                                                                                                                                                                                                          | features                   |

## Other venues, public history

| Window                         | Data                                                                                                                                                                                                                                                                                            | Seen by                                                                                                                                                                                                                                                                                                                                             | Level    |
| ------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------- |
| 2024-09-30 16:00 to 2025-09-30 | Hyperliquid `meta`, 4 h `candleSnapshot` of 80 coins shared with Bybit, `fundingHistory` around 817 Bybit settlements at or below -0.30% (about 210 requests), HLP `vaultDetails` point counts; Bybit against Hyperliquid funding and basis returns, regimes A (8 h Bybit interval) and B (1 h) | Bybit against Hyperliquid screen, exploratory reads of 2026-11-11 (another agent; figures as reported by it, scratch scripts deleted, run logs on the owner's machine, see the [evidence index](exploratory-reads-2026-11-11-evidence.md)), recorded in the [discovery ledger](discovery-ledger.md) and the [program plan](program-plan-2026-11.md) | outcomes |

## Own capture (Schurfer collectors and cohorts)

| Window                          | What                                                                     | Level                                             |
| ------------------------------- | ------------------------------------------------------------------------ | ------------------------------------------------- |
| 2026-07-22 to 2026-09-28        | Pump, source-lead, momentum-flow and radar studies (HYP-011 to HYP-030)  | outcomes                                          |
| 2026-08-10 to 2026-09-14        | Monster-precursor work                                                   | outcomes                                          |
| 2026-08-24T11:10Z to 2026-09-28 | Pump-short maker-entry prospective cohort: episode and venue counts only | coverage (2026-10-30)                             |
| 2026-08-25 to 2026-09-28        | Liquidation cascade episode counts only (read 2026-09-06 on 12 days)     | coverage, outcomes to 09-06                       |
| 2026-08-14 onward               | HYP-015 paper probes                                                     | outcomes (formal read 2026-11-05, reject_hold12h) |
| From 2026-09-29                 | Every venue, every source                                                | HYP-012 v2 blind window                           |

The 2026-07 to 2026-09 own-capture period is the most reused data the program has.
Nothing new is confirmed on it.

## Exposures inside the HYP-012 v2 blind window

One row per check or incident in which a market value from the window (a price, rate,
spread or return) actually reached the output. Values are not copied here. Routine
health checks (service states, connection errors, lag, counters, time of the last write)
are not rows. Routine checks print only the fields they need and filter market values
out before printing; old logs are not reopened to rebuild this list.

| When (UTC)              | What was seen                                                                                                                                                               | By                                                     | Level                                     |
| ----------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------ | ----------------------------------------- |
| 2026-11-10, about 14:00 | Bybit `/v5/market/tickers` last price of BTCUSDT, SOLUSDT and DOGEUSDT, one instant each; the rest of the responses was not printed and the saved files were deleted unread | [Venue cost check](venue-cost-check-v1.md), by mistake | features (three prices, one instant)      |
| 2026-11-10, about 13:35 | `analytics` log tail during the post-recreate health check: one `snapshot.recorded` line, one instrument, with a price and a percentage change at the +1h offset            | Maintenance health check, Redis recreate               | features (one value pair, one instrument) |

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

## HYP-037 registered preflight scope, 2026-11-11

Registered before the first local snapshot read. See the
[preflight contract](hyp037-impulse-policy-preflight-v1.md). Status at registration
is registered, not yet read. The run's hashes and completion are recorded after it.

| Window                        | Data and allowed output                                                                                                                                                                                                                                                                                         | Level                                               |
| ----------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------- |
| 2024-10-01 through 2025-09-30 | Existing HYP-032 features snapshot a3b54d6c...: hourly first impulses, F/G membership, counts, concurrency and pre-impulse 4-hour volatility for planning MDE. Two candidates A/B. No minute entry/exit prices, setup means or net returns. Earlier setup paths may occur in later pre-entry dispersion windows | features and pre-entry dispersion, reused discovery |

The snapshot is hash-pinned. HYP-031's reserved year, the backward holdout and
HYP-012 v2's blind window are outside this scope. No new download is registered.

Completed the registered HYP-037 local preflight on 2026-11-11, run binding created
at 21:29:59.716972 UTC. Source snapshot SHA-256 matched before parsing and before
sealing. The scope above was seen at features/pre-entry-dispersion level. Counts
SHA-256 is `a9ef438e49c8bbf64a2c1c634b0f213f129ae3fc439bd9f3d591e562e5e16169`.
The old HYP-033 counts and frozen event IDs were hash-verified and compared, with
all 986 filtered identities equal. See [result](hyp037-impulse-policy-preflight-result-v1.md).
No setup mean or net, minute outcome bars or blind-window data were read.

## HYP-037 dependence calibration registration, 2026-11-11

Registered before the additional read, following the second preflight review and
its required fixed failure rule. See [amendment](hyp037-power-calibration-amendment-v1.md)
and its machine registration. Reuse the identical snapshot and frozen A/B IDs.

| Window                        | Registered scope                                                                                                                                                                                                                                                                                                                                                 | Level                                                 |
| ----------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------- |
| 2024-10-01 through 2025-09-30 | Seven 4-hour pre-impulse return proxies ending 0/24/48/72/96/120/144 hours before each impulse starts, plus the 1-hour waiting bar ending at decision d. Output only defined counts, week-bootstrap and independent variances, dependence ratios, calibrated planning MDE and assumed-effect power. No individual proxies, proxy means, trade net or setup means | pre-entry dispersion and dependence, reused discovery |

Every proxy ends before entry at d+1 minute. Some later events' proxies can overlap
earlier events' hold paths; that exposure is acknowledged. Full original weeks
must remain represented. One candidate stops without outcome reading if its
unrounded calibrated MDE exceeds 50 bps; an unestimable calibration blocks that
candidate. The other proceeds independently. First invocation must follow a clean
committed registration and code revision.

HYP-037 dependence calibration completed 2026-11-11 from clean `538abdc`, run
binding created 22:21:19.989253 UTC. Scope is the eight registered proxies above;
level recorded conservatively as outcomes (proxy dispersion/dependence only).
Earlier setup paths may overlap a later proxy. No individual proxy, proxy mean,
setup mean, trade net or minute outcome was emitted or read as a trading result.
Calibration SHA-256 is `65197f39aef5bb2164be67bfc9360bfdce81cc552b66639937efca0960900775`.
Both candidates stop on calibrated power. See [result](hyp037-power-calibration-result-v1.md).

## B1 local report and data-quality diagnosis, 2026-11-12

Existing Bybit discovery premium snapshots (2024-09-30 through 2025-09-30): registered
S1/S2/S3 aggregates and a consistency diagnosis of minute closes versus hourly
extrema, including three native premium examples. Level: **features**, no perpetual
price or strategy return. [Local result](bybit-presettlement-feasibility-result-2026-11-12.md)
blocks B2 with `superset_rebuild_needed`; it reads no new window and releases no test
or holdout. Computed on clean local revision `1a9f9f8` from the three pinned inputs.

## B1 hourly-envelope diagnosis and scope proposal, 2026-11-12

Same pinned discovery funding/liquidity index and minute-premium request metadata:
prior-only eligibility counts and exact requested-minute timestamp completeness;
plus the already inspected native 0GUSDT hour and its 60 minute-premium rows, now
checked through request identity and serialization. No strategy return or new market
request. Level: **features for the existing premium diagnostic, coverage for proposed
scope counts**. [Proposal](b1-minute-scope-repair-proposal-v1.md) is not active.
An illustrative response in the public API documentation, with May 2022 timestamps,
also reached output while checking field semantics; it is an unverified documentation
example, not a fetched historical series or a confirmation observation.

## B1 review integration and classifier-containment proof, registered 2026-11-13

The pinned HYP-031 discovery index only: run the real B1 `classify` over every
forecast interval and d30/d15/d10 using constant synthetic premium closes. Output
only counters, omitted IDs (if any), source fingerprints and timings. No real premium,
perpetual price or return is read by this audit. Level: **coverage / synthetic code
check**, not an economic read. The amended report is 4h-only; validation sample
membership never selects an event. The historical 0.1% minute/hour gate is explicitly
retired after its observed failure, because hourly premium no longer selects the
universe. Supplemental fetching remains disabled pending the bounded pilot runner,
design acceptance and a new owner go.

## B1 bounded transport tooling, 2026-11-13

Implementation and tests only. The fixed scope/pilot artifacts are read as request
metadata to check their pinned hashes and 11,660/64 counts. HTTP tests use generated
premium rows and a localhost server, including a partial response and restart.
No new market request, premium value, price or strategy return is read. Level:
**coverage / synthetic code check**. The real classifier-containment evidence and
report thresholds are unchanged. The bounded pilot has not run; a separate fetch
go and review remain required. See [runbook](b1-bounded-fetch-runbook-v1.md).

## B1 bounded supplemental premium fetch registration, 2026-11-13

Registered before the first request, following the owner's instruction to continue
and subsequent request, "может хватит ревью ?", to stop adding review rounds.
Operational interpretation: waive another code-review round, run the already frozen
64-ID pilot, and continue the same supplemental scope only if its unchanged gate
passes. This is not permission to relax a data gate or open B2/outcome reading.

- Venue/market: Bybit linear perpetual **premium index**, native symbol and minute
  timestamp, existing discovery bounds 2024-09-30 through 2025-09-30 only.
- Level: **features**, historical minute premium; this can overlap the hypothetical
  holding path. No perpetual prices, trade returns, test year or blind window.
- Frozen scope SHA-256:
  `0b11db40c505452e05620c22d1aa9aa3038cd95c6ae3ce2e524f232d8168ab6a`.
  Frozen pilot SHA-256:
  `cab2d6d09ce986e1408b9ea298822a203d87ca20708fd28a8c7a9f3e71b82855`.
- 64 fixed pilot IDs; at least 61 complete and no integrity failure, no replacement.
  Up to 11,660 supplemental 4h intervals in total, with successful pilot reuse.
- Persisted limits remain 128 attempts / 10 MiB for the pilot, 23,320 attempts /
  512 MiB globally including the pilot and retries; at most 3 requests per second.
- Run from the clean committed transport revision following `23d8177`. Immutable
  `run.json` binds that exact revision, source/input hashes, IDs and limits before
  HTTP. Retain native-response receipts, the SQLite ledger and eventual manifest.

The [transport runbook](b1-bounded-fetch-runbook-v1.md) and report thresholds are
unchanged. The later result must distinguish pilot transport success from S1/S3
acceptance; a successful download alone releases no strategy stage.

The registered fetch completed on clean `1cc0949`: pilot 64/64 and full supplement
11,660/11,660 complete, 11,660 attempts including the pilot, no retries. Reserved
body bytes: 167,326,720 globally and 951,296 for the pilot. Native responses, ledger
and sealed supplemental snapshot are retained; snapshot SHA-256 is
`3fd3205c7f6f2ab129db6d77b4b3e514a7416d1aa6fbb010b3ee577bc9a6dfc8`.
The [fetch inventory](evidence/b1-prior-only-4h-2026-11-13/fetch/inventory.json)
preserves the clean run binding and per-receipt digest manifest. No strategy return
was read. The next read is the already registered B1 S1/S3 feature-level report on
the same discovery scope, with all financial and data gates unchanged.

## B1 prior-only 4h report, 2026-11-13

Completed the already registered S1/S3 read on clean `8175878`, from the four pinned
discovery inputs. Level: **features**, not strategy outcomes. Pooled and 4h S1 pass;
4h S3 passes at d30/d15/d10, with 1,637 / 1,594 / 1,574 events. No non-4h or pooled
S3 result is admitted. The derived S3 identity export uses the same frozen
classifier and checks all counts against the accepted result; it introduces no new
feature or filter. Result SHA-256:
`96ed96d58514815203aa221a35bce5ed19a37b9b5316947148375a033170311e`.
See [result](bybit-presettlement-feasibility-result-2026-11-13.md). No strategy return,
test year, backward holdout or HYP-012 v2 blind-window read is released by this entry.

## Incidental documentation-search exposure, 2026-11-13

A web search intended to verify the order-book archive's official host returned
irrelevant recent Bybit community snippets with BTC, ETH, NEAR and ZRO quotes and
technical indicators. The posting times and market values were not verified;
conservatively record this as incidental quote-level exposure inside the v2 blind
period. It is not a systematic market-path or strategy-outcome read. The snippets
are not used in any feature, rule or result; no claim that these incidental quotes
were unseen is made. Subsequent source verification used the official history
catalogue's exact public metadata URL, without visiting a market/chart page.

## B2 source and HEAD inventory registration, 2026-11-13

Following the owner's continuation instruction after B1 passed, the
[B2 contract](bybit-presettlement-maker-feasibility-v1.md) fixes entry model, offsets,
wait/latency/quantity sensitivities and the metadata scope before any fill or book
body read. The accepted B1 event identities remain unchanged. Level for this step:
**coverage and source metadata only**, not price paths or returns.

The official history catalogue links to quote-saver.bycsi.com. The retained browser
observation and one matching HEAD establish that link; the standalone 0GUSDT
2025-09-30 provenance example is not part of event selection and no body was read.
The frozen metadata scope has 724 symbol-days on 151 instruments, 2,172 HEAD URLs
(ob500, ob200, trades) and at most 4,344 attempts at 3 requests/s. No GET for an
archive, redirect following, trade row or order-book level is allowed in this step.
Missing/unknown sizes remain explicit. Ten source-quality pilot IDs (5 per pinned
delivery stratum) are fixed before HEAD results, without existence/size resampling.
The pilot's book/tape bodies and all strategy economics remain unread.

The HEAD run completed from clean `e3e33c6`: 2,172 attempts, 1,448 available files,
724 absent depth alternatives, no retries or unknown sizes. Every frozen symbol-day
has a book and tape file, totalling 75,793,957,219 bytes. No body was consumed.
See [metadata result](bybit-presettlement-b2-scope-result-2026-11-13.md).

All ten original pilot books are ob500. Two additional ob200-only schema fixtures,
PROMPTUSDT 2025-08-24 and ASRUSDT 2025-08-23, were frozen by the recorded hash rule
after HEAD but before any body/fill read. They do not replace or resample the
original ten or add a strategy candidate. The resulting source-only plan is 24
files / 1,555,848,890 nominal bytes, capped at 2 GiB on the wire including retries
and partials, 48 attempts, 3 requests/s. Archive bodies still have not been read.

While checking current official API field semantics, illustrative BTC response
examples with 2022/2023 timestamps also reached output. These are unverified
documentation examples, not historical market-path observations or confirmation
evidence; none is used in a study input or return. The documentation does not
establish whether old archive CSV/book versions carry today's BT/RPI/cts fields.

## B2 fixed source-pilot read registration, 2026-11-13

Following the owner's instruction to continue the main B2 work, the fixed 24-file
plan is registered for source-quality inspection, before the first archive body
read. Plan SHA-256 is
`39c5bda6f438f51e90fbb899a2eb93250acf995727d032a6e725f5a011fe8630`.
The 12 symbol-days and original/extra-format strata are unchanged from the metadata
result. The transfer remains capped at 2 GiB including partials/retries, two
attempts per file / 48 globally, three request starts per second. Every native
file is retained with a checksum and frozen HEAD size/ETag, under a clean revision.

Level: source schema, counts, clocks, units and quality diagnostics only. Full-day
book state is reconstructed for continuity and strict-prior-quote checks; no
numeric quotes or price paths are emitted. Native trade minutes :55 through :04
are excluded before price/size/turnover conversion and counted only as rows,
preserving the seconds-study tape reservation. Missing BT/RPI fields and engine
clocks remain unknown. No fills, strategy outcomes, test-year, backward-holdout
or HYP-012 v2 blind-window read is authorized by this entry.

The first source-quality implementation rejected all 12 pairs because native
book archives end after their named UTC day. It computed no fills or returns;
its exact report is retained. A timestamp-only boundary check found closing
snapshots 0.655 to 1.904 seconds after the next midnight. Before the second read,
register source-quality v2: off-day book rows are counted and skipped before
level conversion or tape alignment. This clips the source to the existing
population rather than extending the read window. No signal, execution policy,
threshold, pilot ID or native input changes. Reserved trade minutes remain opaque.
