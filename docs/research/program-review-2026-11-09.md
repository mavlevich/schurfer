# Research program review, 2026-11-09 (draft)

Status: **draft for the owner and the colleague, 2026-11-09.** It follows the
[2026-10-31 review](program-review-2026-10-31.md) and decides nothing; it reads nothing
new. Its purpose: after the strategy v2 families and HYP-015 all stopped, decide whether
the program opens anything more, and what.

## What ran since 2026-10-31

| Line                                    | Result                                                                                                                                 | Cause                                 |
| --------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------- |
| HYP-032 compressed spring               | closed at `insufficient_power` (2026-11-07)                                                                                            | power                                 |
| HYP-033 squeeze continuation (family 1) | stopped at counts, `insufficient_data`                                                                                                 | no comparable controls                |
| HYP-034 premium dislocation (family 4)  | stopped after Amendment 1, `insufficient_data`                                                                                         | balance on the last hour              |
| HYP-035 relative strength (family 6)    | stopped at counts (PR #565 still open)                                                                                                 | balance and power of daily books      |
| HYP-036 absorbed buying (family 2)      | stopped at the pilot gate; a changed matching rule failed; a liquid top-10 pilot on new blocks failed (#567 to #569)                   | coverage and z1 balance, three ways   |
| Family 5, post-pump OI turn             | paused on paper (#569)                                                                                                                 | MDE 75 to 111 bps                     |
| BTC trend, long or cash                 | no protocol (#570)                                                                                                                     | MDE 31 to 50% a year on clean windows |
| Execution edge, limit against taker     | paused (#571)                                                                                                                          | powered, but no signal on record pays |
| **HYP-031 post-funding, minute**        | **fail** at discovery, read 2026-11-05: 4,928 events with full data, mean net -13.0 bps at 30 bps costs, -33.0 at 50; test year unread | the effect is smaller than the costs  |
| **HYP-015 hold12h**                     | **reject_hold12h** on Gate B, read 2026-11-05: 3,575 pairs, mean net -0.64% after costs and funding                                    | the signal                            |

No return of any strategy v2 family was read: every one stopped before its reading.
Family 3 (liquidation cascades with fills) was not feasible on Bybit as counted.

## What the record adds

The 10-31 review's three lessons stand (small edges die on crossing costs; own-collector
cohorts are slow; pre-registration worked). Three more:

4. **A treatment defined by an extreme price state has no comparable control.** HYP-033,
   HYP-034 and three HYP-036 designs failed on coverage or on z1 balance, whatever the
   pool: net buying and impulses come with up hours, against other coins and against
   the same coin's own hours alike.
5. **On this data and at this size, only large effects are testable.** A 4-hour alt
   trade needs about 1,000 to 1,500 independent events for an MDE near 50 bps; daily
   books and whole-strategy BTC tests cannot get there on the clean windows.
6. **Cheaper execution is worth something only for a signal that pays.** The execution
   test is the first one that is powered, and it has nothing to apply to.

## Records not yet in `main`

- **HYP-031's discovery read** is in local commit fa13ae5 (report SHA-256 matched); the
  registry in `main` still says "registered, not opened".
- **HYP-015's evidence** is in local commit 2d1c582 on `codex/hyp015-formal-evidence`.
- **HYP-012 v2:** its 2026-10-31 administrative-stop checkpoint result is not recorded in
  the repository.
- **The 10-31 stop rule** (date 2027-01-31, about 4 owner hours a week, a success bar,
  and an early stop if HYP-015 fails, HYP-031 fails discovery and the reserve fails) was
  proposed; whether the owner adopted it is not recorded.

## Where that leaves the program

**All three conditions of the 10-31 early stop now hold.** Two lines gave negative
results on their own reads: HYP-015 and HYP-031's minute protocol (no second read
needed). The reserve, HYP-035, and several other candidates stopped at counts before any
return was read, so their effect sizes stay unknown. That is enough for a program
decision; it is not a finding about those effects.

What the stop means, as proposed on 10-31: no new research line or stage opens;
collectors that no registered cohort needs move to maintenance (backups and health
kept, closed-line writers stopped); the program resumes only on the criterion below.

- **HYP-012 v2** runs under its registered rules: first its 2026-10-31 checkpoint status
  is restored, then it continues to its deadline, 2027-03-31, or to its own
  administrative stop. Before any closed-line writer is turned off, the sources this
  cohort still needs are checked.
- **HYP-031's seconds-level variant** stays in reserve. The minute failure does not answer
  the question of the first seconds; the variant opens when the rate's publication delay
  and the executable price right after it can be measured. Today the spread at that
  moment is only an assumption.
- **Records into `main`:** HYP-015's formal result (2d1c582), HYP-031's minute discovery
  (fa13ae5), the HYP-035 record (#565) and the execution inventory (#571); then the
  owner's decision is written into this review.

## Resuming

The program resumes only when one of these appears, and the resuming record names
which gate it now has grounds to pass:

- a **new independent window** of data (for example forward data after HYP-012 v2 is
  terminal) for a line whose stop was power or data;
- a **measurable change in execution costs** (fees, spreads or fill quality at the
  program's size) that moves a stopped line's cost assumption;
- a **data source that removes the cause** of an earlier stop (for example a measurable
  publication delay and executable price for HYP-031's seconds-level variant, or
  controls a stopped design lacked).

## Position

Mine, and the colleague's: **apply the stop now** and open no other family or idea.
The record since 10-31 is consistent: the cheap public-history screens work and fail
fast, and what they keep finding is that the edges within reach at USD 50 are smaller
than the noise or the costs.

## Owner decisions needed

1. Adopt the stop now.
2. Keep HYP-031's seconds-level variant in reserve under the condition above.
3. Restore HYP-012 v2's 10-31 checkpoint status.
4. Bring the four records into `main`.
