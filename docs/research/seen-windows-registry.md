# Seen windows registry

Status: **first version, 2026-10-30.** It lists which data windows the program has
already looked at, and how much of them was seen, so a later study can tell whether a
window is still clean for confirmation. It decides nothing by itself. New studies add a
row before their first fetch; the [discovery ledger](discovery-ledger.md) keeps the
per-hypothesis detail.

Publication correction, 2026-11-13: the specific local B1 four-hour supplement and
B2 registrations below were committed before requests but not published in main
before them. Their [actual chronology](b1-b2-publication-chronology-2026-11-13.md)
is retained; this publication is retrospective, not backdated. Subsequent B2 native
computations require the relevant contract and scope in main first.

Levels of exposure, from strongest to weakest:

- **outcomes:** returns, labels or verdict statistics were read;
- **features:** values of candidate inputs were read (rates, distributions, extremes);
- **coverage:** only counts, timestamps, file sizes or availability were read.

A window seen at the outcomes or features level is discovery-only for any hypothesis
that uses the same inputs. A coverage-only window stays usable for confirmation.

## Bybit public history

| Window                                             | Data                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         | Seen by                                                                                                                                                                                                                                                                                                                            | Level                      |
| -------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------- |
| 2024-10-01 to 2026-09-28                           | Funding settlements of live and delisted USDT perpetuals: rates, intervals                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   | Post-funding feasibility check, 2026-10-25                                                                                                                                                                                                                                                                                         | features                   |
| 2024-10-01 to 2026-09-28                           | Spot and linear 4 h klines and funding, live spot-perp pairs                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 | [Carry economics screen](bybit-carry-economics-screen-v1.md)                                                                                                                                                                                                                                                                       | outcomes                   |
| 2024-10-01 to 2025-09-30                           | HYP-031 discovery: funding windows and 1 m klines; native trade tape only in the minutes [s - 5 min, s + 5 min) around its event settlements (narrowed 2026-11-06 with HYP-032's fixed mask)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 | [Discovery read](bybit-post-funding-minute-v1.md) 2026-11-05 (fail)                                                                                                                                                                                                                                                                | outcomes                   |
| 2025-10-01 to 2026-09-28                           | HYP-031 test year                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            | Registered; unread (the minute variant closed at discovery)                                                                                                                                                                                                                                                                        | reserved                   |
| 2024-10-01 to 2025-09-30                           | HYP-032 power step 1: 1 h klines, 1 h open interest and funding, live and delisted USDT perpetuals; counts, funnels and the tape-scope symbol-day list                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       | [HYP-032 protocol](bybit-compressed-spring-v1.md), read 2026-11-06                                                                                                                                                                                                                                                                 | features                   |
| 2024-10-01 to 2025-09-30                           | HYP-032 tape scope: trade archive listings inside the window and sampled HEAD sizes                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          | [HYP-032 protocol](bybit-compressed-spring-v1.md), read 2026-11-06                                                                                                                                                                                                                                                                 | coverage                   |
| 2024-10-01 to 2025-09-30                           | HYP-032 power step 2: Bybit trade tape of the primary scope, per-minute taker aggregates outside the fixed mask, plus masked minutes around unreserved hour boundaries                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       | [HYP-032 protocol](bybit-compressed-spring-v1.md), read 2026-11-07                                                                                                                                                                                                                                                                 | features                   |
| 2024-10-16 to 2025-09-30                           | HYP-032 power step 4: H-hour returns (4, 12, 24 h) at random liquid instrument-hours and at random compressed hours against their price-state matched controls, never at a condition 4 event; standard deviations and design effects only, no mean; paths never within an hour of an event's 24 h path                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       | [HYP-032 protocol](bybit-compressed-spring-v1.md), read 2026-11-07                                                                                                                                                                                                                                                                 | outcomes (dispersion only) |
| 2024-10-01 to 2025-09-30                           | HYP-033 steps 1 and 2: first hourly impulses, funding sign, 2 h open interest change ranks, impulse-matched control pairs and pre-impulse 4 h volatility, from HYP-032's features snapshot; counts, pairs and MDE only, nothing after any decision time                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      | [HYP-033 protocol](bybit-squeeze-continuation-v1.md), read 2026-11-08                                                                                                                                                                                                                                                              | features                   |
| 2024-10-01 to 2025-09-30                           | HYP-034 fetch and steps 1 and 2: 60-minute premium index klines of the HYP-032 universe; dislocation and ordinary-hour counts, last-hour direction, z4-matched and (Amendment 1) z4- and z1-matched control pairs, balance of pre-t 1 h and 4 h returns, overlap components and pre-t 4 h volatility (hourly closes after 1,172 of the 1,957 events entered later units' volatility, spread only); no minute trade or net after any decision                                                                                                                                                                                                                                                                                                                                                                                                                                 | [HYP-034 protocol](bybit-premium-dislocation-v1.md), read 2026-11-08                                                                                                                                                                                                                                                               | features                   |
| 2024-10-01 to 2025-09-30                           | HYP-035 steps 1 and 2: daily 00:00 UTC cross-sections of 7-day BTC-relative log returns, ranks, top-decile units and same-day middle-rank controls matched on hourly sd and turnover bin, pre-t beta and 24 h returns, and the daily portfolio returns over the seven 24 h segments before each formation window, from HYP-032's features snapshot; nothing after a decision for that decision, but a segment can cover an earlier hold of the same instrument (2,652 of 9,706 events), so hourly proxies of part of the holds entered the dispersion estimate; no mean, net or minute return read                                                                                                                                                                                                                                                                           | [HYP-035 protocol](bybit-relative-strength-v1.md), read 2026-11-08                                                                                                                                                                                                                                                                 | outcomes (dispersion only) |
| 2024-10-01 to 2025-09-30                           | HYP-036 step 1: B, V and their ranks on HYP-032's tape, undefined reasons and the mask check, and the recount of B and V from the 20 sample days' raw trade files fetched again (about 0.2 GB); no event rate, coverage or MDE                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               | [HYP-036 protocol](bybit-absorbed-buying-v1.md), read 2026-11-08                                                                                                                                                                                                                                                                   | features                   |
| 2024-12-29 to 2025-01-26, 2025-06-22 to 2025-07-20 | HYP-036 step 2 pilot blocks: Bybit trade tape of the full pool (per-minute taker aggregates under HYP-032's fixed mask), events, same-time controls, balance and MDE from data before each t; no trade return computed                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                       | [HYP-036 protocol](bybit-absorbed-buying-v1.md), read 2026-11-09 (stopped at the pilot gate)                                                                                                                                                                                                                                       | features                   |
| 2024-10-17 to 2025-09-30                           | HYP-036 step 3: Bybit trade tape of the full pool scope, counts, pairs and MDE; no trade return computed                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     | [HYP-036 protocol](bybit-absorbed-buying-v1.md), not run (v1 stopped at the pilot gate)                                                                                                                                                                                                                                            | features                   |
| 2024-12-29 to 2025-01-26, 2025-06-22 to 2025-07-20 | HYP-036 matching check: the pilot blocks' pre-t features again (same events, controls from the whole same-hour pool under one changed rule), coverage, balance incl. turnover, MDE; no trade return computed                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 | [HYP-036 matching check](bybit-absorbed-buying-v2-matching-check.md), read 2026-11-09 (fail)                                                                                                                                                                                                                                       | features                   |
| 2024-10-17 to 2025-09-30                           | Next-candidate planning: family 5 counts (pumps, open-interest turns, risk-set controls, balance, MDE) from 1 h klines and open interest of the HYP-036 pool; majors counts from the HYP-036 pilot blocks' tape (top 10); no trade return computed                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           | [Next-candidate note](next-candidate-feasibility-v1.md), read 2026-11-09                                                                                                                                                                                                                                                           | features                   |
| 2024-10-27 to 2024-12-08, 2025-03-23 to 2025-05-04 | Liquid top-10 flow pilot: Bybit trade tape of the monthly top 10 (per-minute taker aggregates under HYP-032's fixed mask), events, same-instrument controls within 7 days, balance, concentration and MDE from data before each t; no trade return computed                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  | [Liquid top-10 pilot](bybit-liquid-top10-pilot-v1.md), read 2026-11-09 (fail)                                                                                                                                                                                                                                                      | features                   |
| 2024-09-16 to 2024-09-22                           | HYP-031 fetcher smoke: event and window counts, request rates                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                | [HYP-031 runbook](bybit-post-funding-minute-v1.md)                                                                                                                                                                                                                                                                                 | coverage                   |
| 2020-03-25 to 2026-10-28                           | Trade archive file listings per symbol; order book archive file sizes                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        | This registry's checks, 2026-10-30                                                                                                                                                                                                                                                                                                 | coverage                   |
| 2021-06 to 2025-03, samples                        | Row counts of klines, open interest (1 h, 5 min) and funding, ~60 symbols                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    | This registry's checks, 2026-10-30                                                                                                                                                                                                                                                                                                 | coverage                   |
| 2024-10-01 to 2025-10-01 00:00                     | HYP-032 exploratory returns: H = 4, 12, 24 h hourly gross, funding and net at 30 bps for every hour passing conditions 1 to 3 and for the condition 1 to 4 events; BTC-adjusted, halves, without top 5; 6 variants                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           | HYP-032, exploratory reads of 2026-11-11 (another agent; figures as reported by it, scratch scripts deleted, run logs on the owner's machine, see the [evidence index](exploratory-reads-2026-11-11-evidence.md)), recorded in the [discovery ledger](discovery-ledger.md) and the [program plan](program-plan-2026-11.md)         | outcomes                   |
| 2024-10-01 to 2025-10-01 00:00                     | HYP-033 exploratory returns: all first impulses, event, f_only, g_only, neither at 4 h; event at 8 and 12 h; f_only splits by G, zw, funding depth and simultaneous impulses, horizons 2 to 12 h, 1 to 8 slot portfolios, a post-hoc wave filter; 1 m entry check on 59 events (60 Bybit 1 m kline requests); 33 variants                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    | HYP-033, exploratory reads of 2026-11-11 (another agent; figures as reported by it, scratch scripts deleted, run logs on the owner's machine, see the [evidence index](exploratory-reads-2026-11-11-evidence.md)), recorded in the [discovery ledger](discovery-ledger.md) and the [program plan](program-plan-2026-11.md)         | outcomes                   |
| 2024-10-01 to 2025-10-01 00:00                     | HYP-034 exploratory returns: primary and V1 events long at 4, 8, 12 h, V1 short at 4, 8, 12 h, simultaneity splits, portfolios, a lowered +0.30% short threshold; 1 m entry check on 60 events (60 Bybit 1 m kline requests); 16 variants                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    | HYP-034, exploratory reads of 2026-11-11 (another agent; figures as reported by it, scratch scripts deleted, run logs on the owner's machine, see the [evidence index](exploratory-reads-2026-11-11-evidence.md)), recorded in the [discovery ledger](discovery-ledger.md) and the [program plan](program-plan-2026-11.md)         | outcomes                   |
| 2024-09-30 23:00 to 2025-09-30 23:59               | HYP-031 before settlement, exploratory returns: long into settlement on the minute windows (r_prev and F_hat(t - 60 min) selections), on the full hourly grid of 4 h and 8 h contracts with F_hat(t - 1 h) from hourly premium, split by true and false triggers; 1 m premium index klines of 100 intervals (100 requests, seed 42) for an S1 and S2 pilot at d = 30, 15, 10, 5 min; 10 variants                                                                                                                                                                                                                                                                                                                                                                                                                                                                             | HYP-031 family, exploratory reads of 2026-11-11 (another agent; figures as reported by it, scratch scripts deleted, run logs on the owner's machine, see the [evidence index](exploratory-reads-2026-11-11-evidence.md)), recorded in the [discovery ledger](discovery-ledger.md) and the [program plan](program-plan-2026-11.md)  | outcomes                   |
| 2024-10-16 to 2025-09-11, 8 settlements            | HYP-031 seconds: public trade archive files of 8 settlements (21 daily files), ticks [t - 5 s, t + 300 s], returns from t + 5 s and t + 10 s                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 | HYP-031 seconds, exploratory reads of 2026-11-11 (another agent; figures as reported by it, scratch scripts deleted, run logs on the owner's machine, see the [evidence index](exploratory-reads-2026-11-11-evidence.md)), recorded in the [discovery ledger](discovery-ledger.md) and the [program plan](program-plan-2026-11.md) | outcomes                   |
| 2024-10-01 to 2025-09-30 23:59:59.999              | Pump long, post-pump short and maker entry lab series v1 to v3: hourly returns at H = 2, 4, 8 h with actual funding, taker 30 bps and maker 15 bps, from the local features (HYP-032 run), premium (HYP-034) and minute windows (HYP-031 discovery) snapshots; no network request; 54 configurations                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         | Pump long / post-pump short lab series, exploratory reads of 2026-11-11/12 (another agent; scripts and results kept with digests, see the [evidence index](exploratory-reads-2026-11-11-evidence.md)), recorded in the [discovery ledger](discovery-ledger.md)                                                                     | outcomes                   |
| 2024-09-30 to 2025-09-30                           | B1 feasibility fetches: 60-minute premium index klines (open, high, low, close) of the 672 instruments of the pinned HYP-031 discovery index (5,703 requests); then 1-minute premium index klines of the superset intervals (an hourly premium low at or below -0.20%) and a 2,000-interval sample drawn without any rate (count printed before that fetch); settled rates of those intervals for S1 only; done 2026-11-12 (hourly 4,620 responses, minutes 54,909 intervals). B1 amendment 2 adds a third fetch: 1-minute premium index klines of the event-superset intervals (passing the premium-free S2 filters at some d) that the second fetch did not request, 35,217 intervals, chosen without any premium or rate; no perpetual price, kline, trade or return. The premium inside [t - d, t] overlaps the would-be hold: laboratory exposure on the discovery year | [B1 design](bybit-presettlement-feasibility-v1.md), fetch from 2026-11-12                                                                                                                                                                                                                                                          | features                   |

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

| When (UTC)                   | What was seen                                                                                                                                                                                                   | By                                                                                                                                                      | Level                                     |
| ---------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------- |
| 2026-11-17 or shortly before | Roughly one-year price charts of radar tokens BLESS and RARE, spanning dates after 2026-09-29, venue not recorded; viewed by the owner, viewing dates not recorded; not viewed by the research process          | Owner's own look at the charts; recorded as diagnostic-only in the [HYP-038 intake](radar-pump-observation-2026-11-12.md); no v2 decision depends on it | outcomes (visual, those instruments)      |
| 2026-11-15 or shortly before | Radar cases AKE, FRONG and EVAA, dates after 2026-09-29, venue not recorded; named in the owner and colleague's direction discussion, viewer and viewing dates not recorded; not viewed by the research process | Recorded as diagnostic-only in the [HYP-038 intake](radar-pump-observation-2026-11-12.md); no v2 decision depends on it                                 | outcomes (visual, those instruments)      |
| 2026-11-12 or shortly before | Price charts of radar tokens, among them STAMP, PUSSY, POD and SPACEHOOD, dates after 2026-09-29, venue not recorded; viewed by the owner, not by the research process                                          | Owner's own look at the charts; recorded as [HYP-038 intake](radar-pump-observation-2026-11-12.md); no v2 decision depends on it                        | outcomes (visual, those instruments)      |
| 2026-11-10, about 14:00      | Bybit `/v5/market/tickers` last price of BTCUSDT, SOLUSDT and DOGEUSDT, one instant each; the rest of the responses was not printed and the saved files were deleted unread                                     | [Venue cost check](venue-cost-check-v1.md), by mistake                                                                                                  | features (three prices, one instant)      |
| 2026-11-10, about 13:35      | `analytics` log tail during the post-recreate health check: one `snapshot.recorded` line, one instrument, with a price and a percentage change at the +1h offset                                                | Maintenance health check, Redis recreate                                                                                                                | features (one value pair, one instrument) |

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

Source-quality v2 completed on clean `1f03acf`: all 12 pairs parsed, with no fills
or returns. The original exact-decimal notional diagnostic flagged 1,317,035 of
9,420,197 unmasked trades. Before a further source-only diagnostic, register a
breakdown of home-notional versus size and relative error of foreign-notional
versus price times size. Fixed reporting bins are exact, at most 1e-15 / 1e-12 /
1e-9 / 1e-6, and larger; these are diagnostics, not replacement quality gates.
Also count native BT/RPI label representations without inferring eligibility.
Read only the same 12 pinned tapes, skip reserved rows before numeric conversion,
emit no prices, sizes, monetary amounts or returns, and preserve all prior results.

The registered unit/flag diagnostic completed from clean `5e8f09d`. All 9,420,197
unmasked rows have home-notional equal to size. Every nonzero foreign-notional
relative error is at most 3.123e-16, consistent with floating-point serialization;
the original strict diagnostic is retained. Native BT is always absent; RPI is
represented by 0/1 or is absent. No unknown row is reclassified as ordinary.
Source quality v2 and this auxiliary read remain source-level evidence only;
there is no fill/return or new-window read. See
[source-pilot result](bybit-presettlement-b2-source-pilot-result-2026-11-13.md).

## B2 entry-pilot replay registration, 2026-11-13

Publish the [entry-only contract](b2-entry-pilot-contract-v1.md) in main before
running. Reuse only the frozen B1 events on the ten original pilot days and their
already downloaded native files; the two ob200 fixtures are separate. Emit model
counts and unresolved reasons for the eight original configurations, preserving
denominators. This is execution-model exposure, not strategy-return evidence.
Clock availability, 2-second gaps, price priority, partials, unknown flow and
unverified native sizing are fixed by that contract before fill statistics.
No GET/HEAD, masked tape values, exit, funding payment, test year, backward holdout
or HYP-012 v2 blind window is opened by this entry. B3's exact exits and a separate
main-published narrow mask amendment remain prerequisites for any exit read.

The power-budget note uses only the sealed HYP-037 MDEs and arithmetic, with no
new data exposure. HYP-037 A/B remain closed; no method or threshold was selected
from a new outcome.

The entry pilot completed on clean `d6f0ece` after #591 merged in main at
2026-11-13T15:53:52Z. **Level: outcomes (execution-model labels only)**, not strategy
returns. The ten original days produce 24 primary decisions and 184 overlapping
configuration instances across all eight settings. Strict primary has 11
unknown-class cases, 9 modeled post-only rejections and 4 no-qualifying-flow cases;
the all-unknown-eligible sensitivity gives 9 full modeled fills, not a real fill
rate. The 300s setting fails the fixed 95% source floor (21/24 complete).
Source-only quote diagnostics on all 12 frozen days split the old 911,480
discrepancies into 432,934 inside-spread and 478,546 at/beyond old opposite quotes;
neither category is automatically corrupt data. See [result](b2-entry-pilot-result-2026-11-13.md).
No new download, masked tape, exit, funding payment, economic return, independent
test or HYP-012 v2 blind-window read occurred. No configuration was selected.

## B3a fixed economic read registration, 2026-11-13 (not yet run)

Owner accepted the revised [design](b3-economic-screen-proposal-v1.md) after the
colleague review response with "ну значит го го го". Publish this registration,
contract, exact scope and implementation in main before the first native read.
Level: **strategy outcomes**, discovery only. This entry records planned
exposure, not a completed request or result.

Use B1's frozen d30 export SHA
`4f1554108d6da503630d24d5b123d8000532b7bb3decb33f033e57dab65711f7`,
1,637 events, 150 symbols, 718 forecast-settlement symbol-day clusters.
[Scope](evidence/b3-economic-screen-proposal-2026-11-13/requests.json.gz) SHA
`c199994e706ae956ed454682a757c867fab187581a233c7d3d5b3897bdb56213`
fixes 4,200 native linear-perpetual price/mark request identities, each 32
one-minute bars in [forecast t minus 30 minutes, t plus 2 minutes). Reuse
pinned discovery funding index `f48fe09f...`, with rates used only for cash-flow
accounting and proven page coverage. BTC is the only fetched benchmark; no
median-pool queries, native tape, order-book or new venue is opened.

Both fixed next-minute-entry policies are reported together, exits t minus
one minute and t plus one minute. Costs 30/50 bps, two-exit Bonferroni bound,
calendar-week dependence, complete-cluster accounting, funding valuation
sensitivity, mandatory non-gating portfolio and the limited 15 bps spending
stop are fixed before the read. Maker uses the same future frozen event list
and exits; B3a is not an outcome-blind maker validation. All existing discovery
selection history remains, including the choice of 4h after exploratory returns.

Budget: 256 MiB conservatively reserved response-body bytes including retries,
two attempts per key, 8,400 globally, three request starts per second. Resume
cannot reset code/scope/limits/ledger. Preserve source bodies and immutable
receipts. There have been no B3a market requests or return computations at
registration. Official API/help pages were read only for schema and funding
semantics; illustrative examples are not historical execution observations.
No HYP-031 test year, backward holdout or HYP-012 v2 blind window is released.
The narrow future maker tape policy is specified in the design but is not
activated by this kline registration.

### B3a targeted review before outcomes, 2026-11-13

The reviewer reported a source-level coverage check of the already pinned
discovery funding index: all 1,637 events covered, no paging gaps or recorded
fetch errors; no rate values emitted and no economic report run. This is a
reviewer's reported pre-main-publication check, not a claim that this planned
registration was already in main. It is not independently repeated here.

Before any native B3a fetch or economic read, correct the one-slot diagnostic
from fixed USD 45 plus a USD 5 reserve requiring cash at least 50 to
min(45, cash minus 5), minimum modeled notional USD 5. The minimum is an
accounting assumption, not native order evidence. Add known-prefix lengths
and explicit verdict reasons; retain the unknown tail and circular-week
bootstrap. Only synthetic fixtures are read for this correction. Event net,
nomination gates, frozen requests and the two exits are unchanged. #593 was
closed after premature publication; the correction remains local for targeted
review before a PR is reopened or created. No native requests or returns
have been read by this implementation.

### B3a native fetch and formal read completed, 2026-11-14

#593 merged as `45fc1a14d0d4ccc70175439505b7b36eb4662a0e` before native
requests. The owner separately authorized the fetch, then the formal read with
"Да, рассчитать оба выхода". Both actions used clean main and the unchanged
4,200-key scope `c199994e...`, run binding `c4563c48...`. All 4,200 windows are
complete; 4,206 attempts include six recovered retries. Sealed native snapshot
SHA `d952ecf2b22d3ede3b393347dda51f12a518b3601b5537bb9842a383be29740d`.

**Level: strategy outcomes, discovery only.** [Result](b3a-discovery-result-2026-11-14.md),
original JSON SHA `4d4f6f1adc18bda779c48aa78bf7ea997f34f47b79bf602e5f32e3cd87444b33`.
Both fixed taker exits fail: cluster mean net30 -26.03 / -20.43 bps, with
1,626 / 1,628 complete events and 708 / 710 complete clusters. Zero-volume
execution bars account for 11 / 9 unresolved events. Funding is an accounting
input, not a future-known signal feature. Both exits were read together, seed
3103, 20,000 whole-calendar-week draws; no parameter or code change followed.

The budget diagnostic is `maker_source_review_needed`: upper favorable
pre-cost price-plus-funding bounds 21.68 / 26.08 bps do not meet the fixed
both-below-15 pause rule. This is no maker pass, exam release or new archive
go. All taker nomination failures are retained. The one-slot known prefix
ends before its first unresolved selection on 2025-06-07; full ending cash
stays unknown, with no zero/skip substitution. Independent arithmetic checked
the registered report only, without extra cuts, seeds or data.

The earlier exploratory selection history remains. No test year, backward
holdout, HYP-012 v2 blind-window, native reserved tape, new venue, production
or order is opened. Both original outputs and all native receipts are retained;
publication of this completion record does not rewrite the prior registrations.

## HYP-036 bounded planning audit, registered locally 2026-11-14

Owner accepted the limited next step with "го го го" after the inventory review.
The [contract](hyp036-power-audit-v1.md), code and this row are committed locally
before calculation. They are not claimed to have been in main before the audit.
There is no network request or strategy outcome read in this step.

**Level: pre-decision feature summaries.** Existing discovery-only features
`a3b54d6c...` and the frozen pilot identities `338b6e38...`, original counts
`db24fde0...`. Decisions remain 2025-01-13 to 2025-01-26 and 2025-07-07 to
2025-07-20. Reproduce the original raw-price planning MDE and add the
BTC-adjusted event-price diagnostic; compute historical four-hour scales over
336 hourly ends at/before each t and pool correlations before January/July.
Earliest required price bar is 2024-12-01 00:00 UTC for January's monthly pool
and correlation; no event hold return, mean, PnL or funding outcome is output.
The existing feature container is parsed whole, but no holdout or test input is
loaded. Historical values can follow an earlier event; this is feature-level
exposure, not a claim of disjoint untouched price windows. Only fixed
dependence sensitivities and calendar projections follow. No new hypothesis,
exam assignment, full archive fetch or promotion is authorized.

Completed from clean `de499aa` on 2026-11-14, 12:30:02 to 12:30:22 UTC.
Raw original MDE reproduced: conditional discovery projection 19.88 bps;
BTC-adjusted event-price projection 14.61 bps. Actual 28-day sample MDE is
68.65 / 50.48 bps. [Result](hyp036-power-audit-result-2026-11-14.md), original
JSON SHA `5bf24449cb3c0455fae7dd329ea60a36c24cf8b710b66ed7e42ea2feef631f0f`.
Only the fixed summaries and sensitivities were computed; no event holding
return, funding outcome, mean or PnL. Exam power remains unknown. This was
locally registered before calculation, not retrospectively claimed as a
publication in main before it.

## HYP-036 economic pilot `hyp036-econ-v1`, registered 2026-11-14

[Contract](hyp036-economic-pilot-v1.md), reviewed by the colleague 2026-11-14.
This row, the code and the frozen request list must be in main before the first
request; the fetch and the read each need the owner's go. One spending decision on
the frozen 784 pilot events (`338b6e38...`), never a test, an exam or a strategy
registration. HYP-036 v1 stays stopped.

**Level: outcome-level native price paths, discovery year only.** Bybit V5 1-minute
perp klines for each event hold [t + 1 min, t + 4 h + 1 min), 1-minute mark-price
klines over the same window for the 546 events with a funding settlement inside it,
and 1-minute BTCUSDT klines per distinct decision hour (393). Request list
`docs/research/evidence/hyp036-economic-pilot-v1/requests.json.gz`, SHA-256
`1cc29544b3d7928e3a99c34c906801f1c0aae9153d34a7ef3a8d0c6ad3a785b0`: 784 + 546 + 393 =
1,723 requests, at most 3,446 attempts and 128 MiB of response bytes. Funding rates
at the settlements inside the holds come from the pinned discovery index
`f48fe09f...`. Windows: 2025-01-13T00:01Z to 2025-01-27T03:01Z and
2025-07-07T00:01Z to 2025-07-21T03:01Z, only the listed holds. After the read these
holds count as read at outcome level for this trigger. No holdout, HYP-031 test-year,
HYP-012 v2 blind-window, tape or control input is requested.

Completed on the owner's go, 2026-11-14:

- **Fetch** from clean `af25f70`: all 1,723 requests complete on the first attempt
  (30.6 MiB). Sealed receipts `f038532e...`, outside Git.
- **Read** at 22:49 UTC: result `6acc6544...`, verdict `stop_spending`.
  [Result](hyp036-economic-pilot-result-2026-11-14.md).
- **Now read at outcome level for this trigger:** only the listed hold windows of
  the two pilot blocks. No other window was opened.

## Catalyst feasibility `catalyst-feasibility-v1`, registered 2026-11-15

[Contract](catalyst-feasibility-v1.md), design discussed with the colleague on
2026-11-15. This row and the code must be in main before the first request; the
fetch and the report each need the owner's go. Counts and timestamps only, no HYP
id, no strategy registration.

**Level: coverage (event metadata) in both windows.** Requests:

- Bybit V5 announcements, types `delistings` and `product_updates`;
- Bybit V5 linear `instruments-info`, statuses `Trading`, `Delivering` and `Closed`;
- Binance's public announcement list, catalogs 48, 161 and 49;
- Upbit's public notice list.

At most 3,000 requests, 64 MiB of response bytes and one request per second. No
market-data endpoint is called.

**What is kept:**

- announcement items whose canonical machine time is in 2023-02-01 to 2024-09-15
  or 2024-10-01 to 2025-09-30;
- instruments launched before 2025-10-01, with symbol, launch time and a delivery
  time only if it is before 2025-10-01.

Every other item, including the HYP-031 test year and the v2 blind window, is dropped
after only its machine timestamp is read. No response body is persisted. The
backward holdout appears in Git only as aggregate counts; its event list is sealed
outside Git. The discovery year's event list (time, source, article id, base,
bucket) is published and holds no market value.

**Incidental exposure during design, 2026-11-15.** Two web searches for
documentation of the Binance and Upbit announcement endpoints found none. They
returned news headlines about Upbit listings, at least one in the discovery year
and some possibly dated in 2026, inside the v2 blind window. These were titles only,
with no market values. They are recorded conservatively, like the 2026-11-13
search, and are not used in any rule or count.

Completed on the owner's go, 2026-11-16:

- **Fetch** from clean `48676de` after four runs that stopped on the feeds' shape or on a
  rate limit and were fixed through #602 to #605 before any count was read. 287 requests
  in all (3,594,593 reserved bytes), 152 pages continued from earlier runs. Run binding
  `c477aebd...`, outside Git.
- **Report** at 14:00 UTC: outcome `stop_direction_a`, buckets `A1` and `A3`
  `insufficient_events`, `A2` and `A4d` `lab_only`, `A4f` and `A4r` `not_reconstructible`.
  [Result](catalyst-feasibility-v1-result-2026-11-16.md).
- **Now read at coverage level, as registered:** event counts and timestamps in both
  windows. This study read no returns. The backward holdout appears only as aggregate
  counts; its sealed event list has SHA-256 `52baf153...`. No price, trade, spread,
  depth, funding rate, open interest or return was read, and no holdout title, ticker or
  date was looked at. The backward holdout keeps its registered status. The discovery
  year was already open and is used for development; it is not a confirmation window.
- **Requested, not read:** Upbit's notice list. Its first page failed all three attempts
  (HTTP 429, 400, 429), so no page of it was read.
- **Not requested:** Bybit `product_updates` (no machine publication time on its first
  page) and Coinbase (no second-precision listing history is known).
- **Structural look at the A2 holdout titles, 2026-11-16, on the owner's approval
  ("Да, разрешаю").** One run of a read-only command over the kept receipts classified
  the 134 holdout A2 articles that gave no base into eight shape classes fixed in
  advance and printed only the total and the count per class: 134 total, 134 `other`, 0
  in every named class. No title text, ticker, article id, date or calendar distribution
  was printed or stored, and no return was read. `other` means the
  classes did not recognise the title format; it is not evidence that no events or no
  edge exist. The cause of the lost bases is not established and A2 stays `lab_only`.
  No second class selection on the holdout titles is allowed now. It is a disclosed look
  at the holdout titles of A2, not a confirmation read. The backward holdout keeps its
  registered status. [Diagnostic](catalyst-feasibility-a2-diagnostic-2026-11-16.md), section 6.

## Bybit against Hyperliquid cost-only `bybit-hl-cost-only-v1`, drafted 2026-11-17

[Contract draft](bybit-hl-cost-only-v1.md), Stage 2A of the
[consilium plan](../consilium/plan.md). **Not registered for any request yet.** This row,
the code and the colleague's design review must be in main before the first request.
There is no AWS credential in the program: the owner has not decided to provide one, and
every listing, request and download needs the owner's separate go. No request has been
made and no market value has been read.

Two steps, hard-separated:

- **Step A, listing only.** Signed requester-pays `ListObjectsV2` of the Hyperliquid
  archive bucket, `market_data/[date]/0/l2Book/` for 2023-02-01 to 2025-09-30, plus an
  hourly refinement for coins first seen in the discovery year. At most 12,000 attempts,
  256 MiB of response bytes, 3 requests per second. Object keys, dates and sizes only; no
  object body, no price, no order book. **Level: coverage.** Discovery year: per-coin
  results published. Backward holdout (2023-02-01 to 2024-09-15): aggregate counts only,
  coin list sealed outside Git. Gap week (2024-09-16 to 2024-09-30): lookback only, no
  per-date output.
- **Step B, cost only.** One instant per date and pair, 2024-10-01 to 2025-09-30 only:
  `T = D 00:00:30 UTC`, at most 12 dates and 108 pair observations, read from a `Range`
  prefix of one Bybit `ob200` (else `ob500`) day file and one Hyperliquid `l2Book` hour 0
  file. Stored: half spread and executable non-fee cost `x` in bps at USD 50, 250, 500 and 1,000,
  statuses, and a boolean price-ratio diagnostic flag. Never stored or published: a price
  level, a mid, a cross-venue ratio. At most
  512 MiB of response bytes. **Level: features (cost only), discovery year only.** No
  return, no price move between instants, no funding rate, no basis level, no open
  interest.

Not requested, in either step: the HYP-031 test year (2025-10-01 to 2026-09-28) and every
date on or after 2026-09-29. The request builder refuses them. Step B never touches the
backward holdout. The discovery year is already open at outcome level (the Bybit and
Hyperliquid rows above); Step B is laboratory exposure on it, not a confirmation window.
The test year is `reserved` here and unassigned in the program plan; the contract does
not use it and lists the disagreement as an open question.

## I25 new-perp short `new-perp-short-v1`, draft protocol, 2026-11-16 (design only)

[Draft protocol](new-perp-short-lab-protocol-v1.md), revision 10 after the colleague's
review, for discussion. **This row records no read.** The document fetches nothing,
reads no price, funding rate, spread, depth or return, and registers no request list. It
states in advance what each later stage would read, so that no window is spent by
accident. Each stage needs the owner's separate go and, before its first request, a
request list with a hash and an update of this row.

**Planned exposure, by stage (not yet read):**

- **Stage 1, coverage level, discovery year only (2024-10-01 to 2025-09-30).** Bybit
  trade-archive file listings and first daily files, catalog fields of `instruments-info`,
  machine-timed announcements, the Bybit spot catalog, and existence lists of trade
  bars, mark-price bars, funding history and order-book files. Identity is judged by
  evidence that is not a price. No price is read.
- **Stage 1b, coverage level, backward holdout (2023-02-01 to 2024-09-15).** The same
  checks, **sealed and aggregate only**, run only after stage 1 passes and on a further
  separate go of the owner.
- **Stage 3, features level.** Pre-entry return dispersion and week-block dependence of
  the cohort's first days, discovery year only. No mean, sign or outcome is emitted.
- **Stage 4, outcomes level, discovery year only.** 1-minute trade bars and mark-price
  bars, hourly mark bars, funding history, order-book snapshots and a liquid-pool
  benchmark, for launches whose whole path ends by 2025-09-30. A price-scale
  diagnostic, which gates nothing and excludes nothing, belongs here and is
  outcome-level. After that read those paths count as read at outcome level for this
  trigger.
- **Never read:** any price from 2025-10-01 (the HYP-031 test year, excluded and
  unassigned) or from 2026-09-29 (the HYP-012 v2 blind window). Launches whose path
  would cross 2025-09-30, or 2024-09-15 for the holdout, are counted and never priced.

**Holdout.** The backward holdout stays at coverage level until a frozen exam batch is
decided by the owner and the colleague (program plan, stage 6). I25 does not spend it
alone. Its launch list stays sealed outside Git, as in the catalyst study.

**What the existing count already read.** The `instrument_launches` aggregates of
[catalyst-feasibility-v1](catalyst-feasibility-v1-result-2026-11-16.md): 263 launches in
53 ISO weeks (discovery) and 254 launches (holdout), coverage level, no price. No new
exposure comes from this draft. The draft's only external reads were public Bybit
help-center and API documentation pages (the margin-change notice, the Insurance Fund
page, the Innovation Zone fee FAQ and the fee-group endpoint page), opened on
2026-11-16 in the browser pane and by web search. They hold no market value.

## I25 stage 1 registration `new-perp-short-v1`, request list v1 revision 2, 2026-11-17 (pending the owner's go)

[Request list and contract](new-perp-stage1-request-list-v1.md), frozen as
[`contract.json`](evidence/new-perp-stage1-v1/contract.json). This row, the code, the
contract JSON and the request list must be in main before the first request; **each of the two
gos needs the owner's go**. **This row records no read: nothing has been fetched.**

**Level: coverage, discovery year only (2024-10-01 to 2025-09-30).**

- **Go 1, metadata only** (526 planned requests, measured by the first read; at most 761, attempt cap 1,000, 300 MiB):
  Bybit `instruments-info` linear catalog fields, the trade-archive root listing and
  per-symbol file listings, and HEAD requests (status and size, no body) for the first daily
  file of each launch.
- **Go 2, after the owner reviews the go 1 manifest** (1,181 planned requests, at most 2,400,
  attempt cap 2,400, 8.5 GiB, one 512 MiB cap per first file; the combined byte ceiling of both gos is about 8.8 GiB): the first daily trade file of each launch (column 1, the trade
  timestamp, only), one entry minute bar, hourly trade and mark-price series (start times and
  runs of missing hours only) and funding history (timestamps and page boundaries only). Go 2
  also contains the mark-price, funding and bar coverage reads that protocol section 11 item 2
  names, and it cannot start without a finished go 1 and the manifest hash.

Every response also holds prices or rates. They are discarded unparsed and no price, rate,
volume or return is stored or reported. Hashes: `request_list_sha256`
`8a4758760af4889078f2034eba77c78a8f1885b8269fce6b2cde863259cf9c17`, go 1
`02b5ac65932302e377563cc9d8ce1a1448cd3c94b022cb797a214a4a77f359fe`, go 2
`5324b08114db77c5cc999aa0f7f79f994ff101968b2a9d04ebb67ff4291713bb` (request list revision 6, reviewed envelope handling after the Go 2 partial run below). The order-book archive and
the spot catalog are **not** part of this registration; each is a later decision.

**Cut.** Nothing dated from 2025-10-01 is read, recorded or counted: a catalog row launched from
the cut is skipped before any other field is read, listing names from the cut are dropped
uncounted, no total row count is stored, and an archive directory is kept only if it is absent
from the catalog and its modification date is proven inside the window (an unknown date is not
kept). No span of any series ends after 2025-09-30T23:59:59.999Z. This is how the HYP-031
test year (2025-10-01 to 2026-09-28) and the HYP-012 v2 blind window (from 2026-09-29) stay
unread and uncounted at metadata level too. A launch whose path would cross the cut is
`window_censored` and gets no bar, mark or funding request.

**Holdout.** Not requested. Catalog rows launched before 2024-10-01 are reduced to symbol, base
and, only if it falls inside the window, a delivery date, kept as closed service material
outside Git for `relist` and `multiplier_ambiguous` only. No date or count of them is
published. Stage 1b stays a separate go.

**Recorded after each go (not yet).** An aggregate report of counts, shares and stop codes (no
symbol, no price), the run's `request_list_sha256`, the go hash and the code revision. The
receipts stay sealed outside Git. After go 2 the discovery-year launches count as read at
coverage level for this trigger, and nothing at outcome level. Stage 3 and stage 4 reads need
their own request lists and rows. M5 at stage 1 proves the entry minute bar and hourly
continuity only; minute-level continuity is proven at stage 4 (protocol Amendment 1).

## Stage 2C paper screens `stage-2c-paper-screens-v1`, 2026-11-17

[Paper screens](stage-2c-paper-screens-v1.md) of I36, I35, I26, I27 and I45 at the
owner's USD 5,000. **No window was read, at any level.** No price, funding rate, open
interest, spread, depth, basis, volume or return was read from any venue, archive or
Schurfer capture, and no request was made to any exchange or data service. Inputs are
repository documents, published fee and funding conventions, and formulas. The
HYP-012 v2 blind window from 2026-09-29 was not approached.

**Level: none (no exposure).** The backward holdout (2023-02-01 to 2024-09-15), the
HYP-031 test year and the discovery year keep their registered status; none is spent or
shortened by this document.

**Planned reads, none authorized.** Any later measurement named there is limited to the
discovery year (2024-10-01 to 2025-09-30), needs its own contract and this registry
row updated before its first request, and needs the owner's separate go. Reusing the
local carry snapshot (2024-10-01 to 2026-09-28) for I36 would be a discovery-only read
at a window already seen at the outcomes level. The holdout is not requested.

## I25 stage 1 Go 1 read `new-perp-short-v1`, 2026-11-17 (coverage level, completed)

[Result record](new-perp-stage1-go1-result-2026-11-17.md). Go 1 of the
[stage 1 request list](new-perp-stage1-request-list-v1.md) was run on the owner's go from a clean
`f6eb5c3`, bound to `request_list_sha256`
`023b9863d1af73dbeaff5cc7cf64d0e77f3c0fdba295e4158f7c0e54982d6fe3`.

**Level: coverage, discovery year only (2024-10-01 to 2025-09-30).** 526 requests, no retry, no
stop: Bybit linear `instruments-info` catalog fields (4), the trade-archive root listing (1),
per-symbol file listings (263) and HEAD requests (status and size, no body) for the first daily
file of each launch (258). 11.4 MB of response bytes. **No price, rate, volume or return was read;
no first daily file was downloaded.** Nothing dated from 2025-10-01 was read, recorded or counted,
and no holdout, HYP-031 test-year or HYP-012 v2 blind-window input was requested.

**Now read at coverage level for this trigger:** the discovery-year launch catalog fields, the
archive file names and dates of those launches, and the sizes of their first daily files. Nothing
at outcome level.

**Findings recorded.** 22 of 258 first files exceed the 64 MiB per-file cap (a selection limit
against the most active launches); M2 (ii) archive-only share is `not_assessed` (the root page has
no modification dates). Manifest SHA-256
`72395099c6110427f4b841cfb6bf4a477ed142bc829b6c0905fed158105317a0`. The lifted per-file cap is the
reason for the contract amendment (PR 615); Go 1 is rerun in a new root under the new hashes, so
the discovery-year metadata will be read at coverage level a second time. Receipts and
`preflight.json` (per-symbol sizes and the 22 names) stay sealed outside Git.

## I25 stage 1 Go 1 rerun and Go 2 partial read `new-perp-short-v1`, 2026-11-18 (coverage level, stopped)

[Request list, section 6c](new-perp-stage1-request-list-v1.md). Run on the owner's go from a clean
`e70a1b4`, in a new root (outside Git), bound to `request_list_sha256`
`0a0c2ffc5371ce1b7e15ecc3c91419c5dda5edd3fd7e939229407a5b1b05e894`.

**Level: coverage, discovery year only (2024-10-01 to 2025-09-30).**

- **Go 1 rerun, complete:** 526 requests (catalog 4, archive root 1, listings 263, HEAD sizes 258),
  11.4 MB, 08:43:05 to 08:51:56 UTC, outcome `go1_complete`. Manifest SHA-256
  `759305ceaf5730ff0b7f23963a2c17292590ecb590d0336376cc139a0b5ca76d`: all 258 first files within the
  512 MiB cap. This is the second coverage-level read of the same metadata.
- **Go 2, partial, stopped:** 589 receipts, 6.28 GiB, 11:36:05 to 11:49:38 UTC. **D2:** the first daily
  trade file of all 258 launches (6,425.47 MiB downloaded; column 1, the trade timestamp, kept, every
  other column discarded unparsed). **D3:** the entry minute bar start time, 110 candidates. **D4:**
  hourly mark-price series, 110. **D5:** hourly trade series, 109 (start times and runs of missing
  hours only; the pages hold prices, discarded unparsed). **D6** (funding) not started. The run stopped
  fail-closed on `D5: schema mismatch (envelope)`: a Bybit error envelope with an unknown retCode, whose
  body is not kept. No verdict is computed from the partial run.

**Now read at coverage level for this trigger, in addition to the Go 1 read:** the first-trade
timestamps of 258 discovery-year launches, and the existence and continuity of the entry bar and hourly
series of 110 candidates. **Nothing at outcome level.** No holdout, HYP-031 test-year or blind-window
input was requested, and nothing dated from 2025-10-01 was read, recorded or counted.

**Next.** The fix (this change) records the retCode of an error envelope and retries transient codes. Both
gos are rerun in a new root under the new hashes, which reads the same discovery-year data a further
time; each rerun needs the owner's go. Receipts and `preflight.json` stay sealed outside Git.

**Diagnostic request, 2026-11-18.** On the owner's go, exactly one request outside any run budget:
the TUTUSDT hourly trade kline (`interval=60`, `start=1742638383361`, `end=1744193583361`,
`limit=1000`) that failed at seq 1115, sent once to `api.bybit.com` with no retry. **Coverage level,
discovery year only** (the window is 2025-03-22 to 2025-04-09). Only `retCode` and `retMsg` were
read, and the response was never stored. Result: **`retCode` 0, `retMsg` "OK"**: the stop at seq
1115 did not reproduce. Its original cause and retCode remain unestablished;
a later success is consistent with, but does not prove, a transient error.

## I25 Stage 1 completed rerun, 2026-11-19 (coverage only, incomplete)

On the owner's separate gos, the sealed `i25-stage1-v3-8748d9e` run executed
Go 1 and Go 2 on clean revision `8748d9ef664b4b0c65f885420692166dd8ad7a54`.
The approved discovery window is 2024-10-01 through 2025-09-30. Go 2 finished
at 2026-11-18T23:17:11Z (2026-11-19 00:17 Europe/Warsaw).

- Go 1: 526 attempts, 11,413,845 response-body bytes.
- Go 2: 1,200 attempts, 6,753,329,719 bytes, no retries or API errors.
- D2: all 258 first files, including the 22 above the former 64 MiB cap.
- The frozen catalog has 263 launches; primary selection is 228 episodes in
  48 ISO weeks. M4 is coarse, M5 minute continuity is deferred, M6 is static.
- Outcome: `stage1_incomplete`; only M2(ii) is unassessed because root dates
  were not extracted. This is not a trading outcome or a profitability test.

Access was coverage level: file timestamps, bar/funding timestamps and metadata.
Exchange bodies contained prices/rates, which were not extracted or retained.
No test-year/holdout/blind-window outcome or new order path was read or run.
The source remains sealed outside Git. Aggregate evidence is committed as
`evidence/new-perp-stage1-v3/{stage1-report,manifest,stage1-verification}.json.gz`;
decompression yields the original JSON, not private per-symbol inputs.

Report SHA-256: `400f635ea29ac52dc5999caa866b152fa14cea1b82381ce91270012821a853ca`.
Manifest SHA-256: `759305ceaf5730ff0b7f23963a2c17292590ecb590d0336376cc139a0b5ca76d`.

## I25 M2(ii) supplement registration, 2026-11-19 (proposed, not read)

The [separate contract](new-perp-m2ii-supplement-v1.md) binds that sealed report,
manifest and source input hashes. It uses the frozen 263 symbols, not a fresh
catalog. Only archive root/directory HTML may be requested, capped at 1,200
unknown directories, 1,500 attempts and 128 MiB, including retries/reserves.
Unknown names remain in memory until a pre-cut file admits them. Random opaque
journal identifiers and operational counters are permitted; post-cut identities,
dates and launch counts are not persisted. No CSV, price, bar, funding or book
request is part of this supplement. There is no resume or mutation of the source.

Supplement request-list SHA-256:
`4ca2124139eb28e4c38dc00e40b4a245eb7a5c357c5729028842f61221854a7c`.
This registration is a proposal, not evidence of a read or authorization for one.
The owner must approve this exact scope/hash separately after review and merge.

## I25 M2(ii) v1 actual read and link-format probe, 2026-11-19

Coverage-level metadata only. Approved supplement v1 on clean merged `84f2ba5`
made 81 attempts (one C3, 80 D1), read 4,421,748 bytes, and stopped on
`unsupported_file_anchor`. No complete M2(ii) result was produced. A separate
owner-authorized format probe made two GETs, read 174,885 bytes, and retained only
`foreign_prefix_dated_csv_gz`. Current ordinal 80 does not prove continuity with
the stopped directory. The probe's frozen caps were six attempts and 6 MiB;
no names, hrefs, dates, raw bodies, CSV values, prices or rates were retained.
These activities spent 83 attempts and 4,596,633 bytes; they did not modify the
original Stage 1 root or the stopped supplement root. The probe verified both.

Diagnostic report SHA-256:
`59c02213bd70a2d2d52966cf33b15aaedb407782601446a988da947ff8fd3cc4`.

## I25 M2(ii) v2 registration, 2026-11-19 (proposed, not read)

The amended [contract](new-perp-m2ii-supplement-v1.md) keeps the frozen source,
archive endpoints, 1,200-directory cap, 1,500-attempt cap and 128-MiB cap. It adds
opaque pre-cut filename identity issues: scan completion never clears such an
issue, and a positive M2(ii) verdict remains blocked. Matching directory/file
prefixes are not inferred to equal foreign prefixes. Valid post-cut-only files
leave no scientific identity, date or count. Exact share is null while identity
is unresolved. All previously spent attempts and bytes remain spent.

V2 request-list SHA-256: `88bebd47b124f1492f049a89ed428a78a2a9f1b03b22d285b92eba88736e6834`.
This is a proposal only, with no new archive access. Review, merge and separate
owner approval for this hash and a fresh root are required before execution.

## I25 M2(ii) v2 actual read, 2026-11-19

Approved coverage-only supplement on clean merged `7e958b8`, contract
`88bebd47b124f1492f049a89ed428a78a2a9f1b03b22d285b92eba88736e6834`.
The full archive HTML scan used 889 attempts and 42,631,655 response bytes:
one C3 root, 888 D1 directory listings, all HTTP 200, no retries or stops.
The 1,500-attempt and 128-MiB caps were respected. No CSV, price, bar, mark,
funding, order-book or AWS request was included.

One exact-prefix archive directory outside the frozen catalog has its first
archived day in the discovery window. Two directories have unresolved pre-cut
filename identity. Their names/dates were not retained; no identity is inferred
and neither directory is silently zeroed. The scan is complete, the identity
check is incomplete, and Stage 1 remains `stage1_incomplete`, with only
`m2_archive_filename_identity_unverified` unassessed. Its exact share is null.
The 228 primary episodes and existing partial-check limitations are unchanged.

The [aggregate evidence](evidence/new-perp-m2ii-v2-result/README.md) preserves
exact JSON bytes, hashes and an independent check of 2,233 captured inputs.
Report SHA-256:
`0590a619d8bc5ddac11e928a958038f360ad4616d3685b11c53dc11be31a1f23`.
The sealed completed source and stopped v1 were verified unchanged. Earlier v1
and probe spending (83 attempts, 4,596,633 bytes) remains spent separately.
No further stage or metadata request is authorized by this record.

## I25 targeted filename diagnostic registration, 2026-11-19 (proposed, not read)

The [proposed diagnostic](new-perp-m2ii-identity-probe-v1.md) binds the sealed v2
report/input map and the original Stage 1. It uses one fresh root HTML and two
current opaque positions (80, 245), with no search fallback. Maximum nine new
attempts and 20 MiB. It proposes private retention of prefixes evidenced by valid
pre-cut filenames and their pre-cut day/count summaries, not verified asset
identity. Directory names require their own matching pre-cut filename evidence.
Post-cut names/dates/counts and raw responses are not retained. Private evidence
is not published. Ordinal matching does not prove linkage to the old directories;
no old report or M2 verdict can be promoted by this diagnostic.

Proposed request-list SHA-256: `617cb488517081071727108945afe7634345fcf1baec22ff05fbfe500cd4ee21`.
No new archive request was made. Review, merged code and separate owner approval
of this exact new hash/caps in a fresh root are required before fetch.

## I25 targeted filename diagnostic actual read, 2026-11-19

Approved diagnostic on clean merged `3cf37ca`, contract
`617cb488517081071727108945afe7634345fcf1baec22ff05fbfe500cd4ee21`.
Three HTML GET attempts (one C3, two D1), all HTTP 200, 214,739 response bytes,
no retries/stops, within nine attempts and 20 MiB. Private pre-cut prefix
observations were captured; names/dates are not published. Each current folder
has one pre-cut prefix with its first archived day before 2024-10-01, and neither
folder has its own admitted pre-cut name. This is filename evidence only, not a
rename, instrument identity or proof of linkage to the two prior v2 directories.

Outcome `diagnostic_complete_unlinked`, continuity `not_proven`; sealed M2/Stage 1
remain `stage1_incomplete`. Both sealed sources and all ten captured inputs
were independently verified. No CSV, prices, funding, bars, books or AWS request
was included. The original 228 episodes and partial checks are unchanged.
Report SHA-256:
`4449921d31dc9efdb61925d8f83b33c5ca0a39885f80de910cd5344b96017b49`.
Further access or a revised M2 definition requires a separate reviewed decision.

## I25 M2(ii) v3 registration, 2026-11-19 (proposed, not read)

The [v3 contract](new-perp-m2ii-file-prefix-v3.md) proposes a fresh full archive
HTML scan by observed canonical pre-cut filename series, not historical asset
identity. Frozen catalog/source fingerprints and sealed prior v2/diagnostic
inputs are verified. Exactly one canonical prefix per directory, disjoint from
frozen_263 and unique across all unknown directories, is required for admission.
Mixed/noncanonical cases and all prefix collisions remain anonymous blockers;
canonical prefixes in mixed directories participate in collision detection too.
No cross-directory merge or inferred rename. Admitted file_prefix is private;
directory_name is retained only when equal to that admitted prefix.

Caps: 1200 unknown directories, 1500 new attempts, 128 MiB, plus a fail-closed
16384 pre-cut prefix-slot RAM cap. No provisional admission before full scan.
No old run is resumed or rewritten; no CSV, market value, funding/bar/book or
AWS request is included. All earlier spending remains spent. No v3 request
has been made. Review, clean merged code and a new owner go are required.

Proposed request-list SHA-256: `11e5275769540faa7d1b03b3e66cecec074f836c33ff36546afeb75dc1a25de0`.

## I25 M2(ii) v3 actual read, 2026-11-19

Owner-approved fresh HTML metadata scan on clean merged `49edd5d`,
contract `11e5275769540faa7d1b03b3e66cecec074f836c33ff36546afeb75dc1a25de0`. The reviewed tree matched the merge tree; CI 6/6 passed
on the reviewed head before merge. This run made 889 HTTP attempts,
read 42631655 response bytes, and used 0 retries.
Status counts: `{"200": 889}`.
Caps of 1500 new attempts and 128 MiB were respected. No CSV, price, funding,
trade/mark bar, book or AWS request was included; old allowances were not reused.

Outcome `stage1_pass_partial`, HTTP scan complete `true`,
file-prefix check complete `true`.
Globally admitted pre-cut filename series: 453;
first file before the discovery window: 452;
first file inside the discovery window: 1.
Unverified directories: 0; collision
directories: 0. Exact M2(ii) share:
`0.003787878787878788`. This is the approved observed-prefix partial cohort, not historical
asset identity or a census of real launches. Known catalog-directory contents
were not rescanned. Private prefixes and dates are not published.

The 228 primary episodes, 48 ISO weeks and inherited
M3/M4/M5/M6 limitations are unchanged. Three sealed source roots and all
2233 captured v3 inputs were verified. Old reports remain
unchanged and incomplete/unlinked as originally recorded. Report SHA-256:
`38b569aa5dc713179ded3db70f7cdf788cea560db7c0c99c65d9332a879070b8`. Evidence-map SHA-256:
`532d101483c2691c422e1ddce2187423dfce5d460c4de1b719d626b66645ad03`. No profitability claim or further data
access follows from this record.

## I25 Stage 1b metadata registration, 2026-11-19 (proposed; executed 2026-11-20, see below)

The [metadata code/contract proposal](new-perp-stage1b-metadata-v1.md) binds the
completed discovery v3 report and all four sealed source roots. It proposes
coverage-level C1/C3/D1/D2h only for the backward holdout
`[2023-02-01, 2024-09-16)`, with private opaque cohort/manifest and aggregate
output. No holdout provider request or read has occurred in this work. Tests use
synthetic responses and a loopback server only. Architecture approval is not
fetch approval. No CSV GET, bars, funding, books, AWS, returns or exam is included.

Proposed new caps: 2400 attempts, 469762048 response bytes (448 MiB), at most
300 catalog rows and 1200 outside-cohort folders. A fresh 0700 sibling root is
required; no old allowances or source facts are repurposed as holdout data.
Early filenames/catalog rows contribute only the agreed RAM predicates, never
exact pre-window dates or unadmitted names on disk. Unknown/colliding labels
cannot disappear from the frozen catalog denominator. Metadata cannot return
`stage1b_pass_partial`; a body-phase manifest review and separate go remain.

Proposed request-list SHA-256:
`31ae1a57970e7af98d2e52fd75534cf2604cd4a79bb889f25cd2f6a90fbada96`.

Revision 2 supersedes the unexecuted metadata-v1 hash `e4513b79bfc4e2f8b31d1e691a3879565ad3f0e7372ab20ffd9793c69f772c9f`.
It retains the in-window catalog calendar for M2(iii), preserves date-proven M1
ambiguity/reasons and known M2(i) lower-bound failures after later HTTP stops.
Rules are `new_perp_stage1b_metadata_v2`; caps and endpoints are unchanged. No
holdout request was made under the superseded proposal or this revision.
Code/contract review, clean merge, green CI and the owner's go for this exact
hash/caps are required before any holdout access. The shared exam stays sealed;
no parameter tuning on holdout facts is permitted.

### Executed 2026-11-20: Stage 1b metadata read, coverage level, backward holdout

**Level: coverage (metadata) only.** Authorized by the owner, confirmed in chat to the
coordinating session on 2026-11-20; the run itself happened before the recording session, in
another working session. Run `i25-stage1b-metadata-v2-e01d67c`, 00:15 to 00:53 Europe/Warsaw
(`finished_at_utc` 2026-11-19T23:53:05Z), clean revision
`e01d67ce32cb46826cb570102db76b193055fa80`, rules `new_perp_stage1b_metadata_v2`, request-list
SHA-256 `31ae1a57970e7af98d2e52fd75534cf2604cd4a79bb889f25cd2f6a90fbada96`, window
`[2023-02-01, 2024-09-16)` (the backward holdout).

- **Requests:** 1,402 attempts (C1 4, C3 1, D1 1,150, D2h 247), all `ok`, no retry, no stop;
  53,956,397 response bytes of the 448 MiB cap; attempt ceiling 2,400.
- **Outcome:** `metadata_complete_coverage_pending`, complete, binding valid, no stop code;
  `body_phase_authorized` false.
- **What was read:** the Bybit instruments catalog (254 launches in the window), the archive
  root listing, per-folder listings, and the HEAD (status and size, no body) of the first
  in-window daily file of 247 launches. Aggregate counts only are published.
- **Not read:** no CSV GET, no trade or mark bars, no funding, no order books, no AWS object, no
  price, no return, no exam. M1, M3 identity, M4, M5 and M6 are `not_assessed`.
- **Record:** [result note](new-perp-stage1b-metadata-result-2026-11-20.md) and
  [aggregate evidence](evidence/new-perp-stage1b-metadata-v2-result-2026-11-20/); the run
  manifest (SHA-256 `f3098e0840be489df640fbdab66c464257bac3d6aa5315d58e70d86d594d1341`) and the
  per-launch folders stay sealed outside Git.

**Holdout status is unchanged: one shared sealed exam batch.** This is a coverage-level look
(counts of launches, file dates and sizes), not an outcome read. No holdout fact may tune Stage 3
parameters, and the body phase for the holdout needs its own design, review and owner go.

## I25 Stage 1b body registration, 2026-11-20 (proposed, not read)

The [body-phase contract](new-perp-stage1b-body-v1.md) binds the finished Stage 1b metadata run
(manifest SHA-256 `f3098e0840be489df640fbdab66c464257bac3d6aa5315d58e70d86d594d1341`, report
SHA-256 `173347ae6785dd2fe5a5bbbce7e6fdfc10b82db37d98908e7ba0fc6f64458352`, metadata request-list
SHA-256 `31ae1a57970e7af98d2e52fd75534cf2604cd4a79bb889f25cd2f6a90fbada96`). No request was made and
no sealed file was read in this work; the tests use a synthetic sealed root and a mock server.

**Level: coverage only.** Window: the backward holdout `[2023-02-01, 2024-09-16)`. If run, it
reads: the first daily trade file of each of 247 launches (column 1 timestamps and counts only),
then for the launches that stay entry candidates the entry-minute bar start, hourly mark-price
and trade bar start times, and funding timestamps (days 3 to 21 after `T0`, clipped to the
window). Never read or stored: any price, rate, volume, return, order book, announcement, spot
catalog or per-launch result; the discovery year, the HYP-031 test year, the HYP-012 v2 blind
window and anything from 2024-09-16 on. Public output is aggregate counts only.

Two separate owner gos. Go A: 247 requests, 494 attempts, 5,185,208,320 response bytes (4,945
MiB). Go B: at most 1,729 first attempts, 2,162 attempts, 670,040,064 bytes (639 MiB), asked for
only if Go A leaves no stop code, on the SHA-256 of `queue_b.json`. Proposed request-list SHA-256:
`4fa65c35b75b587d55b520833c7c0e84c404c2eadcb56040c3d063f7ab9c4bf2`; go hashes
`1811a7559ec6e1be0aa40821eb6cd586145c0b0343a2d1a2621e6e6363d7af69` (A) and
`b9bc4e6ab07a0964dbbd4b1955ccfd6a222562748a51855097035b8bfbcc77f5` (B). A fresh 0700 root is
required; no earlier allowance or root is reused. Code and contract review, a clean merge with
green CI and the owner's go for each exact hash are required before any request.

**Holdout status is unchanged: one shared sealed exam batch.** `stage1b_pass_partial` would be a
coverage statement, never an economic result, and no holdout fact may tune a Stage 3 parameter.

## I25 Stage 3 registration `new-perp-short-v1`, 2026-11-20 (proposed; executed 2026-11-21, see below)

The [Stage 3 contract](new-perp-stage3-power-calibration-v1.md), frozen as
[`contract.json`](evidence/new-perp-stage3-v1/contract.json), with its code and synthetic tests,
proposes the power calibration of protocol 7.4 (dispersion and dependence only). **This row records
no read: nothing has been fetched, no price was read, and the code ran only against a mock
transport.** The owner's separate go for the exact hash below is required after review and merge.

**Level: features, discovery year only (2024-10-01 to 2025-09-30).** One hourly trade-bar page
(`/v5/market/kline`, `interval=60`) for each of the 228 primary V1 episodes of the completed
Stage 1 run, covering the whole-hour window `[A, B]` inside days 1 to 7 after the first trade
(`A = ceil_hour(T0 + 1 d)`, `B = floor_hour(E)`), so that every bar ends at or before the entry
minute. Of each page only the bar start times and two closes are parsed; the closes become one
log return per episode, kept in a sealed run root outside Git. Published: aggregates only
(dispersion, week-block dependence, MDE, counts). 228 requests, at most 684 attempts, 48 MiB
(about 3 MB expected), about 4 minutes at one request per second.

**Not read:** any bar at or after any entry minute, the entry bar, any exit or hold-week bar,
anything from 2025-10-01 (HYP-031 test year) or from 2026-09-29 (HYP-012 v2 blind window), the
backward holdout, mark prices, funding, order books, minute bars. No holdout fact tunes a
parameter. The discovery ledger count is unchanged at stage 3 (no variant outcome is read).

**Overlap, stated:** the hourly trade klines of live and delisted USDT perpetuals of this year
were read at features level by HYP-032 step 1 and the 2026-11-11 exploratory series touched the
same year at outcome level; whether the days 1 to 7 of each of these 228 launches were among
those reads is not verified. A laboratory result on this year is a hypothesis (protocol 9).

Hashes: `request_list_sha256`
`3392f75267e684e46093cbc0fb55e0714036df585a98d220b416dae4594a08d9`, `go_sha256["1"]`
`701fcfd51b9d10f0e4093ef9eeb585b8a97a95e66bba7685dcfd9666dead7daf`. Prerequisites pinned: Stage 1
report `400f635ea29ac52dc5999caa866b152fa14cea1b82381ce91270012821a853ca`, manifest
`759305ceaf5730ff0b7f23963a2c17292590ecb590d0336376cc139a0b5ca76d`. After a read this row is
updated with the root's report and the outcome; until then, nothing at features level is spent.
The owner also approves the cohort SHA-256 that the `cohort` command prints for the sealed Stage 1
root (it fixes the actual input files by fingerprint), and the stored report of a root is immutable.

## I25 Stage 3 actual read and stop, 2026-11-21

The Stage 3 contract above was run once, on the owner's go for its exact hashes (request-list
SHA-256 `3392f75267e684e46093cbc0fb55e0714036df585a98d220b416dae4594a08d9`, go
`701fcfd51b9d10f0e4093ef9eeb585b8a97a95e66bba7685dcfd9666dead7daf`, cohort SHA-256
`60aa139bf33bf129ee9ff5c530c47a5151fa99b9a712c59e90e82a8285ebaa4c`), at revision
`67ba33f854fb51ae5e47359493909a6f28620010`, rules `new_perp_stage3_v1`.

- **Requests:** 228 attempts, all `ok`, no retry, no stop; 2,667,639 response bytes of the 48 MiB
  cap. A first launch failed before any request (wrong working directory, empty root) and is not
  counted.
- **Outcome:** `insufficient_power`. Calibrated V1 MDE 1,158.0 bps (`2.80 * SE`, SE 413.6 bps,
  effective sample about 123.9, design effect 1.84) against the 400 bps limit; V2 and V3 1,163.1 and
  1,176.2 bps (extrapolated, conditional). The registered sensitivity check (878.0 and 1,028.5 bps)
  fails too.
- **What was read:** hourly trade-bar closes of the pre-entry days 1 to 7 of 228 discovery-year
  episodes (dispersion and dependence only). **Not read:** any bar at or after an entry minute, any
  exit, mark price, funding, order book, anything from 2025-10-01 or 2026-09-29, the backward
  holdout. `returns_read` is false: no return of the short trade exists.
- **Record:** [result note](new-perp-stage3-result-2026-11-21.md) and
  [aggregate evidence](evidence/new-perp-stage3-v1-result-2026-11-21/); the run root
  `i25-stage3-67ba33f` stays sealed outside Git.

**Consequence: I25 `new-perp-short-v1` stops at protocol stop 6.** Stage 1b Go A and Go B are
withdrawn (the body-phase contract stays merged and unrun) and the backward holdout
`[2023-02-01, 2024-09-16)` stays unread as one shared sealed exam batch. The discovery ledger count
is unchanged (no variant outcome was read). The result says the cohort cannot show an edge of any
realistic size; it is not evidence that no edge exists.
