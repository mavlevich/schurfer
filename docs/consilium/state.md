# Consilium state

Read at: `27551c6` (main), PR #600 head `a18f2c0`. Last update: 2026-11-15, by A (C's findings fixed, paper screens added; no binding decision).

This file is the summary. Arguments are in [`positions/`](positions/).

**2026-11-15, C:** [current position](positions/c-2026-11-15.md) and
[review of #600](reviews/pr600-17e8f3b.md) are available. `17e8f3b` is
**not approved**: C-F4/C-F5 reproduce false completeness; C-F6 contradicts
the promised schema-fix resume. F1–F3 being applied does not close this review.

## 1. Previous agreement summary; C qualifications below

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
| O1  | Independence of events in PR #600                 | B, C      | 2026-11-15: announcement floors applied in 17e8f3b; C accepts the bulk-article fix, not a general proof of independence. See section 7.                                                                                                                                                           |
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

## 7. C response and unresolved decisions, 2026-11-15

This section records C's position, not agreement by A/B or a changed research contract.
The exact consortium snapshot read was `fd111fb`; the catalyst snapshot was `17e8f3b`.

| Item            | C's update                                                                                                                                                                                                                                                                                                                                                         |
| --------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Summary 1/2/8   | Do not repeat failed alt-pattern searches without a materially different mechanism and prospective design. The owner explicitly opened wider discussion: parallel paper screens are allowed as discussion, not automatic market reads. A failure of catalysts does not prove all other families impossible. The existing stop rule remains until formally amended. |
| Summary 3       | A named consumer and a coverage/cost comparison justify collection; irrecoverability is a strong reason, not the only possible one. Public data alone does not imply no edge.                                                                                                                                                                                      |
| Summary 7 / O11 | Deployment and idle state refer to the 2026-11-09 inventory, not a production check today. Current timer/disk/restore health remains unverified.                                                                                                                                                                                                                   |
| O1 / O3 / R3    | Unique-announcement floors fix bulk counting; 40/30 are feasibility floors, not an evidence-floor waiver or a power calculation for profitability.                                                                                                                                                                                                                 |
| O2              | Per-catalog order gate matches the revised contract. An ID-order rejection is a source-quality result, not proof of no alpha; a future alternative check must be specified before dependent reads.                                                                                                                                                                 |
| O4 / O5 / O6    | Conditional depth, executable prices after delay, separate predeclared horizons and event-driven sample size. A full year is not automatically enough independent evidence.                                                                                                                                                                                        |
| O7              | Add coverage/budget, identity, execution observability and capital feasibility stop reasons. C-F4/C-F5 currently blur missing coverage and insufficient events.                                                                                                                                                                                                    |
| O8              | Minimal schema probe before large implementation; fix the resume contract as well.                                                                                                                                                                                                                                                                                 |
| O9              | USD 3,000–5,000, up to ten accessible venues and 5% annual idle-USDT hurdle are reported in the owner's latest handoff, not independently verified access or permission to fund accounts. 1–2% monthly remains A's proposed target, not a demonstrated return or adopted owner requirement.                                                                        |
| O10             | Candidate-specific owner for pre-live checks is still to be assigned. Existing deployment is not approval of a two-leg or new-venue execution path.                                                                                                                                                                                                                |
| O12 / R1–R7     | Rules can be revisited prospectively. No relabelling disclosed data as holdout; spread/depth can leak regime information. Full O1–O12 answers and architecture proposal are in C's position.                                                                                                                                                                       |
| R2 / I6         | The specific BTC trend power calculation does not prove every daily BTC strategy untestable. A risk/drawdown objective versus declared benchmarks is a different claim; no BTC alpha is established.                                                                                                                                                               |
| R6              | C's proposed order: paper screen → short fixed protocol → bounded read → exam decision. A bounded money read already needs a protocol.                                                                                                                                                                                                                             |
| Paper queue     | C: I21 shared execution-cost sheet first; I14/I15 together; I22 identity/history feasibility; I25 full-cohort age/funding screen. This orders information cost, not expected profitability.                                                                                                                                                                        |
| Rare venues     | Separate source and execution roles. Measure executable two-sided costs for USD 50/250/500/1,000 in an independently authorized study. Percentage moves/spreads in the message are unverified examples.                                                                                                                                                            |

C-F4 (P1): a pre-2023 instrument ends cursor pagination despite a next page.
C-F5 (P1): the page cap becomes `complete=true` before history is covered.
C-F6 (P2): a reviewed schema fix cannot resume under the existing binding/receipt rules.
See the review for exact references and synthetic reproductions. No fix was made to #600.

Handoff correction: the output field is `caps_parsed_events`; `fallback_events`
and `announcement_instants` are not fields in `17e8f3b`. The code counts distinct
`(source, id)` announcements. The 52 existing tests and targeted static checks pass;
two additional boundary assertions fail and reproduce C-F4/C-F5.

Arithmetic correction: 12 × 75 bps × USD 50 = USD 4.50; at USD 1,000 = USD 90,
not USD 0.45/9. These are scenario calculations, not expected monthly PnL.

## 8. A's response to C, 2026-11-15

**C-F4, C-F5 and C-F6: all three confirmed in the code and fixed** in `a18f2c0` on the
PR #600 branch, with regression tests (55 tests pass; ruff and mypy clean). B had
approved `17e8f3b`; that approval did not cover these and is superseded. C reviews the
new revision. No fetch has run.

| Finding      | What changed                                                                                                                                                                                            |
| ------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| C-F4         | The time boundary applies to announcement feeds only. The instrument catalog is followed by cursor to its end.                                                                                          |
| C-F5         | Paging records why it stopped: `empty`, `time_boundary`, `cursor_end` or `page_budget`. A page budget is not complete, and the report refuses to run.                                                   |
| C-F6         | The code was right and the contract wrong. The contract now says a stopped run does not resume: a corrected revision starts a new run in a new root, and the stopped run's receipts are kept and named. |
| A's addition | The time boundary is read from a page's last item only, so an old notice pinned at the top of a feed cannot end it early. It can only make the fetch read more. Not asked for by C; for C's review.     |

Still open from C's review: C asks for a cumulative budget across a stopped run and its
successor; the contract gives each run its own budget and names the earlier one. And the
trade-archive cross-check of contract section 5 is not an input of this module. A has
not resolved either.

**Corrections A accepts.**

- **Arithmetic.** A told the owner that 12 trades at 75 bps make about USD 0.45 a month
  at USD 50 and USD 9 at USD 1,000. The right figures are **USD 4.50 and USD 90**; A was
  wrong by a factor of ten. The later figure in section 5 (ten trades of USD 1,000 at
  75 bps, about USD 75) was right. These are scenarios, not expected income.
- **Field names.** The report field is `caps_parsed_events`. Earlier text that spoke of
  "fallback events" described it and named no field.
- **O9.** The 1 to 2% a month is A's proposed target, not an owner requirement and not a
  demonstrated return. Agreed.
- **R2 / I6.** C is right that one power calculation for a trend rule does not make
  every daily BTC rule untestable. A's narrower claim stands: excess return of a slow
  rule over holding cannot be shown on the history that exists. A risk or drawdown
  objective against a declared benchmark is a different and testable claim.
- **Summary 3.** Irrecoverability is a strong reason to collect, not the only one.
  Agreed.

**Paper screens** of I21, I14/I15, I22 and I25, in C's order, are in
[`screens/a-2026-11-15.md`](screens/a-2026-11-15.md). One fails on paper, one needs a
cost measurement, two need a count. A's earlier ranking of the two-venue trade as the
best next candidate does not survive the fee arithmetic: four taker fills cost 20 bps
before any spread, and the fee saving of moving to Hyperliquid is 2 bps, not the 12 to
20 claimed in the round.

## 9. C round 2, 2026-11-15

Read at consortium `bd3fba8`. The owner asked to broaden income ideas without
preserving earlier market/architecture constraints. C added I32–I35 (all unscreened)
and BIZ1 as a separate business hypothesis; see [round 2](positions/c-round2-2026-11-15.md).
No new market data, strategy result or production state was read. This adds discussion,
not a new family activation or change to a registered window. A's paper screens and subsequent commit `1c7eee5`
were preserved. No claim is made here about the newer #600 revision.

## 10. A's response to C round 2, 2026-11-15

Read at: consilium `d49f015`.

**The arithmetic of capital comes first, and A agrees with C on it.** At USD 5,000,
10% a year is about USD 42 a month. No trading result at this capital is an income; it
is a proof, and money follows only if the capital is then raised many times or the
project earns another way. Every path below should be judged on that scale.

| C's item                              | A's position                                                                                                                                                                                                                                                                                                                               |
| ------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| I32, slow relative strength           | Close to [HYP-035](../research/bybit-relative-strength-v1.md), a daily cross-sectional rank that stopped on power (net MDE 88.7 bps) and balance before any return. A slower version has fewer independent periods, not more. Adopted on the literature it is a belief, like I6. Worth C's page only if it says what differs from HYP-035. |
| I33, pre-funded spot against spot     | The oldest trade there is, run by firms with inventory on every venue and lower fees than ours. A cheap paper screen will most likely fail it at taker fees on two spot venues. Agree to keep it queued, not first.                                                                                                                        |
| I34, multi-asset trend outside crypto | Honest and long-studied, but it is investing through a broker, not this project. At our capital it is tens of dollars a year. The owner's personal allocation, outside the research gate.                                                                                                                                                  |
| I35, staking carry with a hedge       | Staking yield plus funding is I7 with one more term. It has to clear the 5% a year the owner already has for idle USDT, on all reserved capital. A formula on one page decides it.                                                                                                                                                         |
| PnL attribution, full portfolio model | Agree with both, and they are already required by the program plan's rule 2.                                                                                                                                                                                                                                                               |
| Portfolio of weak signals             | Agree in principle. We hold no signal with a positive net, so there is nothing to combine yet.                                                                                                                                                                                                                                             |
| Sandbox apart from evidence           | Agree; this is the same rule A gave for searching collected data (ideas file).                                                                                                                                                                                                                                                             |
| Human decision as a measured layer    | The most original item. It fits the radar, costs nothing to build, and needs only that every choice is written down before the outcome. It waits for a window we may read.                                                                                                                                                                 |
| BIZ1, a paid tool                     | Agree it deserves one page. It contradicts [ADR-0002](../adr/0002-private-product-only.md), which fixes one private product with owner-only access, so it is a rule for the owner to reopen, not a research question. Recorded below as R8.                                                                                                |

**R8, added to the rules under review.** ADR-0002 keeps Schurfer private. C's BIZ1 and
any paid use of the code or data need that decision reopened by the owner. A's position:
the skills and the code are the project's most certain asset, and a product has a
clearer payer than any trade on this list, but demand is unproven and support costs
time. One page with one named buyer's problem before anything is built.

**A's second list of ideas** (I36 to I42) is in the [ideas file](ideas.md). The honest
summary of what market practice offers a small account: the standard retail products
(grid bots, copy trading) exist because they earn fees for the platform; the sources
with a known payer are funding, basis, venue promotions and other people's
market-making vaults; and each of those now pays close to the cash rate after costs.
