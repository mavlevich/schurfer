# Research program review for 2026-10-31 (draft)

Status: **draft for the owner, written 2026-10-25, facts refreshed 2026-10-28.** It prepares rule 4 of the
[direction brief](direction-decision-2026-10-31-brief.md) ("both lines fail: a written
review of the whole program before any new line"). It decides nothing, reads no cohort
and changes no registered rule. The decision is the owner's, on 2026-10-31.

## Where the program stands

- **Pump and early-flow domain: no survivor.** Every candidate that reached an
  after-cost read failed or stayed unproven: executable pump-short, early_momentum_v4,
  HYP-012b, HYP-012c, HYP-029, abnormal-flow v1, HYP-030 (typical trade near zero, 94%
  of net from five instruments). HYP-022 is inconclusive; HYP-026 was withdrawn.
- **Branch A (Gate collector)** fitted the disk conservatively only without the closed
  pump-short writer (about 43 MiB a day); that writer is off in production since #528
  (2026-10-26), and the fresh reading on 2026-10-31 decides the fit. A still lacks a
  stated reason to expect a survivor that the failures above do not contradict.
- **Branch B (carry) failed its economics screen** for the tested configuration, USD 50
  a leg with isolated +300% collateral ([reading](bybit-carry-economics-screen-v1.md),
  v1.1 fixed before the read, v1.2 fixes, merged as a closed line in #522). No threshold passes: after crossing costs and priced liquidations every
  one loses, from -4.9% to -36% a year on the USD 210 a pair needs. Isolated +300% collateral
  turns base funding (about 9% a year on notional) into 2 to 3% on capital before costs.
- **Open:** HYP-012 v2 (weak prior, administrative stop rule, checkpoint 2026-10-31) and
  HYP-015 (registered read 2026-11-04).
- **Prepared, not opened:** HYP-031, post-funding reaction on Bybit public history
  ([protocol](bybit-post-funding-minute-v1.md) registered in #523, fetcher and report
  merged in #526; nothing fetched).

## What the record teaches

1. **Small edges die on crossing costs.** At USD 50 a real 50 bps edge is about USD 10 a
   month ([cost and power planning](cost-power-planning-v1.md)); most candidates did
   not reach 50 bps gross.
2. **Own-collector cohorts are slow.** One read takes months of capture and blind
   windows (HYP-012 accrued about 1.8 events a day historically). A public-history
   screen answered carry in a day.
3. **Pre-registration worked.** Every failure above was found before money moved.
   Late corrections mostly removed apparent edge (abnormal-flow went from +0.61% to
   about -0.06%; carry worsened once crossing costs and liquidations were priced).

## Proposed decision for 2026-10-31

- **A: no.** Without a stated reason it remains a collector with a weak prior.
- **B: carry closed at this size.** The canary may still run (it is free and recorded),
  but stage A and stage B are not started.
- **Rule 4: this review, then at most one new line: HYP-031.** Its fetcher and report are
  merged (#526); on the owner's go, fetch and read the discovery year; stop on failure.

This departs from the brief, whose rule 4 starts only after the canary fails too. The
brief predates the economics screen, which already closes B at this size, so the review
proposes applying rule 4 now. Until the owner accepts this review, the brief and the
ROADMAP queue (item 3: a Gate collector or the carry path) stand unchanged.

## Lines allowed until the stop date

| Line                        | Next step                                                                                                                                   | Ends when                                                   |
| --------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------- |
| HYP-015                     | registered read 2026-11-04; on a pass, its confirmation stage                                                                               | its own rules                                               |
| HYP-012 v2                  | runs unchanged under the administrative stop                                                                                                | its own rules                                               |
| HYP-031                     | fetcher and report, discovery read, then test read; the seconds-level variant only under its own rule (first minute >= 30 bps on discovery) | a failed read with no seconds-level opening, or a test pass |
| Relative strength (reserve) | opens only if HYP-031 stops before 2026-12-15                                                                                               | same shape as HYP-031                                       |

Nothing else opens before the stop date, whatever a result suggests. The one
conditional opening is HYP-031's seconds-level variant, under the rule already fixed in
its protocol.

## Stop rule (proposed; the owner sets the numbers)

- **Date:** 2027-01-31.
- **Budget:** about 4 hours a week of the owner's attention (reviews, decisions,
  deploys) and no new paid infrastructure.
- **Success** means at least one line, by the date, meets both:
  1. it passes its own registered out-of-sample read, with that read's criteria
     unchanged (for HYP-031: all five conditions on the cluster mean);
  2. in the same read's one-position-at-a-time portfolio at USD 50 (for HYP-031 the
     protocol's one-position diagnostic, chosen before the outcome), the portfolio
     itself has at least 10 trades a month, that is 10 distinct entry times, **and** a
     mean net of at least +20 bps per trade after the registered costs, so it earns
     at least about USD 1 a month. The frequency and the money come from the same
     portfolio: a pass on the cluster mean with a losing portfolio is not a success
     (colleague review: a synthetic example passed both stages at +42.5 bps and 52.5
     trades a month while its portfolio lost USD 10.5 a month).

  Condition 2 is the program's bar for continuing, not a change to any line's verdict:
  the line's registered result stays as its own reading gave it. A line whose
  registered read has no such portfolio fixes one, with its selection rule, before
  that read.

- **On success:** that line alone continues, and only through its own registered next
  stages and their entry conditions: for HYP-031 a live measurement of
  settlement-time spreads and then paper, which wait until HYP-012 v2 is terminal
  (its capture deadline is 2027-03-31). Live trading follows only the existing
  live-probe design. Every production step (a deploy, a capture, a key) still needs
  the owner's separate go. Success does not open new lines.
- **On no success by the date:** no new research line or stage is opened; collectors
  that no registered cohort needs move to maintenance (backups and health kept,
  closed-line writers stopped); the program resumes only for a new external reason the
  owner states in writing.
- **Registered cohorts are not interrupted.** Neither the date nor an early stop ends
  HYP-012 v2 (it runs to its administrative stop or its 2027-03-31 deadline) or a
  registered HYP-015 stage: each finishes under its own rules, and its captures keep
  running until it is terminal. The stop rule ends new development, not a registered
  read.
- **Early stop:** if HYP-015 fails, HYP-031 fails discovery and the reserve fails too,
  the rule applies at once, before the date.

## Housekeeping with a money or time effect

Both items proposed here on 2026-10-25 are done: the closed pump-short decision writer
is off in production (#528, 2026-10-26), and the carry result is in `main` as a closed
line (#522).

## Owner decisions needed on 2026-10-31

1. A or B per the brief, and whether to accept this review.
2. Open HYP-031 or not.
3. The stop rule's date, budget and success bar.
