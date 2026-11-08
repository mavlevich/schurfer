# Discovery power budget after HYP-033 to HYP-035 (planning note v1)

Status: **planning note, 2026-11-08; decides nothing and registers nothing.** It reads
no new data and no return: the budget comes from the published counts reports of
[HYP-033](bybit-squeeze-continuation-v1.md), HYP-034 (PR #564) and HYP-035 (PR #565), and the
archive size from the features snapshot's turnover and HYP-032's archive-size record.
It is for the team discussion before the next family is chosen.

## Three stops, three causes

| Line    | Stopped at                               | Cause                                                                                                                                                                 |
| ------- | ---------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| HYP-033 | counts, `insufficient_data`              | Not enough comparable controls: two calipers on an impulse left 61% of events with a control; (b) also lost power                                                     |
| HYP-034 | recount, `insufficient_data`             | Balance: the original control left the last hour far apart (SMD 0.36); the caliper that fixed it dropped the extreme fifth of events (no ordinary hour has a z1 of 7) |
| HYP-035 | counts, `insufficient_balance` and power | Power: a daily book of about 27 leaders has a pre-formation spread of about 3.2% a day; one year of days cannot reach 50 bps                                          |

Two of the three are the same lesson in different forms: **when the treatment is an
extreme price state, ordinary hours with the same price path do not exist**, so either
coverage or balance fails. The third is a plain sample-size limit of daily books.

## The budget

Under the floor shared by HYP-033 to HYP-035 (inflation 1.5, design effect 1.2, MDE =
2.487 x the root of the variance), a leg of n independent units with spread sigma has
MDE = 4.09 x sigma / root(n). The counts reports imply these spreads per event (from their
MDE and n, leg (a)):

| Source               | Horizon | Implied sigma per event | Events needed for MDE 50 bps |
| -------------------- | ------- | ----------------------- | ---------------------------- |
| HYP-033 events       | 4 h     | about 340 bps           | about 800                    |
| HYP-034 events       | 4 h     | about 420 bps           | about 1,200                  |
| HYP-034 V1 (premium) | 4 h     | about 580 bps           | about 2,300                  |

And for a daily book, with about 350 decision days in the window, the day's spread must
be at most about **229 bps** with no weekly dependence (F = 1), and about **212 bps** at
F of about 1.16, as HYP-035's leg (a) had (HYP-035 had 318 for (a), 245 for (b)).

The matched leg (b) needs more: in HYP-034 its MDE was about 1.1 x (a), so about 1.2 x
the events of (a). That ratio comes from the two actual MDEs at 91.6% coverage, so it
already contains that coverage loss and must not be divided by the share with a control
again. It is a guide for a similar design only; the decision is made on the frozen pairs.

**Rules of thumb for a 4-hour alt trade on this window:** about 1,000 to 1,500
independent events (3 to 4 a day), spread over at least 40 symbols, with at least 80%
of them able to find a control. Rarity does not help by itself: it removes events, so a
rare idea passes only with a much larger expected effect than its spread (the
colleague's point).

## What to estimate before any code (the gate the colleague proposed)

For each candidate, from the definition alone and from features already seen:

1. **Independent episodes** after the independence rule, by month and symbol.
2. **Reachable control coverage:** the share of events whose pre-t state (the price path
   the control must share) also occurs in ordinary hours. An event defined by an extreme
   move fails here, whatever the caliper.
3. **MDE** for (a) and (b) with the budget above, and for a daily book the day spread.

A candidate that fails any of the three on paper is not built.

## The remaining families against it

| Family (strategy v2)                    | Episodes                                                 | Coverage                                                                                                   | Data                                                                                                                                      | Position                                                                                                                            |
| --------------------------------------- | -------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| 2 Pressure against price response       | Likely many: a flow rank fires on ordinary price paths   | Good in principle: the treatment is the flow, not the price, so a control can share the event's price path | Taker split is not in Bybit klines; in the Bybit trade archive HYP-032's ablation scope alone (113,826 symbol-days) measured about 706 GB | The only one that fits the budget on paper; must state its difference from the closed abnormal-flow v1 (buy ratio with containment) |
| 3 Liquidation-cascade reversion (maker) | Bybit scopes miss the floor (233 and 217 episodes, #332) | Cascades are extreme moves: the HYP-034 problem                                                            | Needs the order book fill model                                                                                                           | Not feasible on Bybit as counted                                                                                                    |
| 5 Post-pump short on open-interest turn | Unknown; pumps are rarer than the HYP-034 events         | Unknown: a control can come from the pumps themselves (with against without an open-interest turn)         | Features snapshot has open interest                                                                                                       | Episodes are the binding question; close to the closed pump-short lines, so it would need its own stated difference                 |

## Proposal for the discussion

My position: **family 2 next, with the three estimates first and nothing built until
they pass.** The data path is the open question:

- **Bybit trade archive, a liquid subset** (for example the top 150 by turnover): the
  tradable venue's own flow. A whole-year, whole-universe fetch is larger than HYP-032's
  706 GB ablation scope and out of proportion.
- **Binance hourly klines with taker buy volume:** cheap like the features fetch. Only
  as a separate study measured wholly on Binance USD-M (flow and trade outcome): Binance
  flow against Bybit returns tests the transfer of a signal between venues, and the
  source-lead failures make that a known risk.

### Bybit archive size for a point-in-time pool (2026-11-08, no new request)

The pool for signals and controls must be fixed before any count. A top-K pool is set
each calendar month by USD turnover over the 30 days before the month (BTC excluded;
the first month, 2024-10, by its own first week, because the snapshot starts there; a
protocol would need its own rule). Sizes reuse HYP-032's archive-size record
(`archive-sizes-1fedaff`, listings for 630 symbols and 6,003 HEAD samples) and its rule:
each symbol's mean sampled size times its pool days with a file. That rule estimated
HYP-032's primary scope at 160.9 GB, and 168.3 GB was fetched.

| Pool (monthly, point in time) | Symbols over the year | Symbol-days with a file | Estimated compressed bytes |
| ----------------------------- | --------------------- | ----------------------- | -------------------------- |
| Top 50                        | 161                   | 18,148                  | about 295 GB               |
| Top 100                       | 286                   | 36,117                  | about 423 GB               |
| Top 150                       | 394                   | 54,211                  | about 513 GB               |

The bytes sit in the most liquid names (BTC alone is about 73 MB a day), so halving the
pool saves far less than half. At HYP-032's measured 11 MB/s, top 150 is about 13 hours
of download and parsing; the kept per-minute parts are small (about 33 KB a day).

### First step on data already held

HYP-032's tape (rules `tape-v3`, 20,661 symbol-days on 370 symbols) already has per-minute
taker buy and sell USD. It can show whether the family 2 feature can be built and give a
funnel without reading any return. Its days were chosen for HYP-032's decisions, so a
frequency found there does not carry over to the year. A full fetch follows only if the
pilot shows a plausible number of independent episodes, control coverage and MDE within
the budget.

Alternatives to family 2: majors only (much lower spread per event, so far fewer events
needed, but smaller effects and different costs), or the strategy's own fallback when
the list runs out (change the kind of edge, not the next idea).
