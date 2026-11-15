# Consilium state

Read at: `27551c6` (main), PR #600 head `17e8f3b`. Last update: 2026-11-15, by A.

This file is the summary. Arguments are in [`positions/`](positions/).

## 1. Agreed by A, B and C

| #   | Point                                                                                                                                                      |
| --- | ---------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | Short-horizon candle and order-flow patterns on alt perpetuals get no more budget after [HYP-036](../research/hyp036-economic-pilot-result-2026-11-14.md). |
| 2   | The next step is the bounded catalyst feasibility study (PR #600). No second research class runs beside it.                                                |
| 3   | New data is collected only for a named check, and only if a later archive fetch cannot rebuild it.                                                         |
| 4   | The delivery-time collector for announcements is a separate contract, after the count report.                                                              |
| 5   | A catalyst entry is costed as a taker entry. A passive limit order on directional news is not a base assumption.                                           |
| 6   | A paper screen and a cheap bounded read come before any large fetch.                                                                                       |
| 7   | LIVE_PROBE is already deployed and idle. No authorization is issued before a candidate has passed its laboratory and exam.                                 |
| 8   | If catalysts fail, no new family starts automatically; registered obligations (HYP-012 v2, the HYP-038 intake after v2) run to their own rules.            |
| 9   | The improvement roadmap v3 runs on its own time budget and promises no trading edge.                                                                       |

Agreeing to investigate catalysts is not agreeing that an edge is there (C). A rates the
chance of a candidate as low to moderate.

## 2. Open points

| #   | Point                                             | Raised by | Positions                                                                                                                                                                                                                                                                                         |
| --- | ------------------------------------------------- | --------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| O1  | Independence of events in PR #600                 | B, C      | Confirmed, see section 3. A proposes a fix before any count is read.                                                                                                                                                                                                                              |
| O2  | The id-order check may fail Binance for no reason | B         | Real risk, cause unproven, see section 3.                                                                                                                                                                                                                                                         |
| O3  | "Tens of events are enough"                       | C         | A agrees it is unproven. Feasibility shows the material exists; the laboratory protocol must show it is enough for its own conclusion, on independent announcements, not on asset-days.                                                                                                           |
| O4  | Depth at the moment of a catalyst                 | B, C      | A withdraws "only the spread matters at our size". Both spread and depth are measured from the order book archive at the realistic entry second, before the exam is spent, and at the sizes we intend to scale to.                                                                                |
| O5  | Horizon per bucket                                | B         | A agrees fast listings (A1, A2) and delistings or parameter changes (A3, A4) are different mechanisms. One horizon per bucket, fixed before any price; one long variant for A3 and A4d at most, no grid. Multi-day drift is a belief from an ideas note, not a measured fact.                     |
| O6  | The 4-week pilot rule does not fit rare events    | B         | A agrees. For catalysts the cheap screen is the count plus one laboratory read on all events of the year.                                                                                                                                                                                         |
| O7  | Stop reasons                                      | B, C      | A agrees: a failed bucket records one of `not_reconstructible`, `insufficient_events`, `move_gone_before_entry`, `negative_net_after_costs`. A stop is never written as "no edge exists in the market".                                                                                           |
| O8  | Schema probe before a large module                | B         | A agrees as a working rule: one bounded probe of an undocumented endpoint before the code that depends on its shape.                                                                                                                                                                              |
| O9  | What result by March justifies continuing         | A, C      | C: do not ask the owner for a bps number alone. The owner sets the budget of the next attempt, the risk allowed, and the requirements on frequency, capacity and evidence. A agrees and adds the size up to which the effect must hold.                                                           |
| O10 | Pre-live checks on the deployed LIVE_PROBE path   | A, C      | Which checks and adaptations a new candidate needs (unknown positions at restart, stop-loss placement failure), and who confirms them.                                                                                                                                                            |
| O11 | State of production today                         | B, C      | Group 1 stopped and about 28 GiB free is A's last recorded state (2026-11-10), not checked today. B's 77% figure is from the 2026-11-09 inventory. One read-only check settles it.                                                                                                                |
| O12 | Did our own rules cause the dead end              | B         | B: the rules together exclude almost every working idea. A disagrees in part: every line read on money lost on money, not on a rule. Two things were stopped by rules or priority and not by a result: the two-venue idea (R4) and slow majors (R2, where the limit is statistics, not our rule). |

## 3. Findings on PR #600 (catalyst feasibility)

Checked in `apps/analytics/schurfer_analytics/catalyst_feasibility.py` at `f224c79`.
The fetch has not run and no count has been read, so both can be amended cleanly.

**F1. The cluster rule does not do what the contract says. Confirmed.** The contract
(section 3) says a multi-base article counts once per base and "the cluster rule below
keeps them from counting as independent". The rule is one cluster per base and UTC day,
and the code follows it: `clusters = {(base, day)}`. That merges one base on two venues,
but one article about 25 perps still gives 25 clusters. The minimum of 8 ISO weeks stops
two articles from passing alone, so the claim "two service articles pass" is too strong,
but eight bulk articles over eight weeks would pass with eight independent moments.

- A's proposed fix: count distinct announcements (`source`, `id`) and distinct
  announcement instants per bucket, report both, and put the minimums on distinct
  announcements as well as on clusters. Correct the sentence in section 3.
- Open: the number. A proposes the same floor as clusters (40 discovery, 30 holdout);
  B suggested 30 to 40.

**F2. The id-order check may reject a source for the wrong reason. Plausible, cause
unproven.** `discordant_share` compares id order with publication order over all of a
source's articles and fails provenance above 5%. If ids are assigned when a draft is
created, drafts prepared days ahead would disagree with publication order without any
backdating. Nobody here has evidence of how the publisher assigns ids.

- A's position: keep a gate, because it is the only evidence on edits for a source with
  one machine time. Compute it within one catalog, report the pooled figure beside it,
  and record a failure as `order_check_failed`, not as proof of edits.
- Alternative (B): lower it to a diagnostic. A is against without another check on
  edits to replace it.
- Open: whether a failed order check may be followed by a second, pre-registered check
  (for example archived copies of a fixed sample of articles) before the source is
  dropped.

**F2, added after B's review.** Confirmed in the code: `report()` pools all three
Binance catalogs into one list per window before the order check, so drafts of
different catalogs are compared with each other. Computing the check per catalog is
agreed by A and B. Gate or diagnostic stays open.

**F3. The title parser can read an ordinary all-caps token as a ticker. Real, narrower
than reported.** For Binance catalog 49 (monitoring tag) `_caps` runs on the whole
title, and for catalog 48 it is the fallback when no ticker is in parentheses. Any
all-caps token of 2 to 15 characters outside a short stop list becomes a base, and it
becomes an event if Bybit has a live perp with that name. B lists THE, NOT, ONE and
similar. Those match only when the title writes them in capitals, and publisher titles
are in title case, so the common words are not the risk; short all-caps tokens that are
both a word and a ticker (AI, ID, IO, GAS) are.

- A's proposed fix: in catalog 49 read tickers only from the part of the title after the
  monitoring tag phrase, keep the parenthesis form as primary in catalog 48, extend the
  stop list, and report per bucket how many events came from the fallback parser so a
  reader can see the exposure. Add tests with adversarial titles.

Status of all three, 2026-11-15: **applied** in commit `17e8f3b` on the PR #600 branch,
with regression tests, after B accepted A's fixes for F1 and F2. Announcement floors are
40 (discovery) and 30 (holdout). The order check is per catalog and stays a gate; a
second check that could readmit a source is left as a separate amendment. The parser
reads only after the key phrase and its events are counted in the report. B reviews
the updated PR.

## 4. Rules and constraints the owner put up for review

The owner, 2026-11-15: nothing is fixed. Rules written earlier, the architecture, the
capital and the whole tactic may be revised if they stand in the way. Each row is a
question for discussion, with A's position; none is a decision.

| #   | Constraint                                                                                              | What it costs                                                                                                       | A's position                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| --- | ------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| R1  | HYP-012 v2 blind window: nothing from 2026-09-29, any venue, until v2 is terminal (deadline 2027-03-31) | No live measurement of spread, depth or delivery; no forward exam until then; every new study works on history only | The most expensive rule we have, held for a cohort whose prior is weak. Do not break it. At the 2026-11-30 checkpoint, discuss with the count in hand whether a formal amendment may narrow it to the instruments and venues v2 uses.                                                                                                                                                                                                                |
| R2  | Clean windows only                                                                                      | A slow strategy on majors cannot be tested on about two clean years                                                 | B: admit the full BTC and ETH history. A: the [BTC trend note](../research/btc-trend-feasibility-v1.md) shows more history does not make it testable either: telling 10% a year apart takes about 1,280 weeks raw (25 years), and adding 2020 to 2022 only triples 137 weeks. So this is not a rule to relax but a choice for the owner: adopt a pre-declared rule as a belief-sized position with no edge claim (the note's option 1), or leave it. |
| R3  | Evidence floor of 100 resolved episodes                                                                 | Rare events never reach it                                                                                          | Already relaxed for catalysts (40 clusters). Keep the relaxation tied to effect size: fewer events only where the expected effect is several times the cost.                                                                                                                                                                                                                                                                                         |
| R4  | Bybit USDT perpetuals as the only execution venue                                                       | No spot, no options, no second leg, no other venue's fee schedule                                                   | B: this rule is what froze the Bybit against Hyperliquid idea. A agrees it is the constraint most worth reopening, see I14 in the [ideas file](ideas.md). First the owner checks which venues and instruments are actually open to them; the program plan records Hyperliquid access from Poland as `unknown`.                                                                                                                                       |
| R5  | USD 50 per trade                                                                                        | Any edge earns cents; a two-venue trade cannot be funded at all                                                     | A and B agree on rare large trades: capital multiplies a proven edge. B adds a real second case: a two-leg trade needs margin on both venues, so capital is a precondition there. A holds that capital does not make a negative single-leg edge positive, and that it goes in after an exam, except for the minimum a two-leg probe needs.                                                                                                           |
| R6  | Hypothesis-first, exam-gated process                                                                    | Days per stopped line; few lines per month                                                                          | Keep the exam. Shorten the path to it: paper screen, then bounded read, then contract.                                                                                                                                                                                                                                                                                                                                                               |
| R7  | Architecture: many venues, many services                                                                | Maintenance time, disk, attention                                                                                   | Not what stopped any line. Research now runs offline on archives. Simplify production only where it saves real hours; no rewrite.                                                                                                                                                                                                                                                                                                                    |

## 5. Owner's answers, 2026-11-15

| Point                      | Answer                                                                                                                                                        | What follows                                                                                                                                                                                                                                                                                                                                                                                        |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| R4, I14: second venue      | Hyperliquid is open to the owner, and money can be held on both venues, or on more if that brings something. Stated by the owner; not checked by anyone else. | I14 is no longer blocked by access. Its one-hour paper screen reads no data and can be written while the catalyst count runs. A second paper screen: the same known ideas costed on a venue with a lower fee.                                                                                                                                                                                       |
| R4, I11: Bybit instruments | "Everything seemed available recently." Not confirmed for options.                                                                                            | The owner checks options in the account when convenient. Not urgent.                                                                                                                                                                                                                                                                                                                                |
| R2, I6: BTC as a belief    | No preference, as long as it makes money.                                                                                                                     | A: then not adopted. Whether it makes money cannot be shown, so it is a bet and not a strategy. Stays parked.                                                                                                                                                                                                                                                                                       |
| O9: success by March       | Asked A for a proposal.                                                                                                                                       | A proposes three conditions together: one candidate through an untouched window with the lower bound of net above zero; an expected 1 to 2% a month on a working capital of USD 3,000 to 5,000; the effect measured on the book at the size the owner is ready to reach. Plus a fixed loss budget for the live probe. The numbers are the owner's to set.                                           |
| R1: blind window           | Asked A for a recommendation.                                                                                                                                 | A: neither break nor lift it; narrow it by meaning. It protects returns and prices that a later exam uses. Measurements of cost (spread, depth, delivery time of announcements) hold no return and are not joined to signals. Propose at the 2026-11-30 checkpoint an amendment that admits such measurements under their own contract. To be discussed with the colleague who registered the rule. |
| O5: horizons               | Asked whether it can be decided now.                                                                                                                          | Only the principle: one horizon per bucket, taker entry, fixed before any price. The numbers wait for the counts.                                                                                                                                                                                                                                                                                   |
| New ideas                  | Asked how to make everyone look for new ones.                                                                                                                 | Proposed: a blind round. Each participant writes ten ideas from an assigned angle without reading the repository or each other; every idea names who loses the money. The repository is used only afterwards, to screen.                                                                                                                                                                            |

## 6. Corrections log

| Date       | Who  | Correction                                                                                                           |
| ---------- | ---- | -------------------------------------------------------------------------------------------------------------------- |
| 2026-11-15 | B, C | Proposed running the HYP-036 pilot first; it had been read on 2026-11-14 (`stop_spending`). Both withdrew it.        |
| 2026-11-15 | A    | Wrote that LIVE_PROBE was undeployed. The 2026-11-09 inventory records it deployed and idle. Fixed in A's position.  |
| 2026-11-15 | A    | Left the order-rules capture unchecked. It is a host timer, outside groups 1 and 2 (B).                              |
| 2026-11-15 | A    | Called the restore drill "the only action relevant today". Too narrow (C): reviewing PR #600 also moves the project. |
| 2026-11-15 | A    | Claimed only the spread matters at our size. Withdrawn; see O4.                                                      |
