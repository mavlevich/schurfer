# Research program review, 2026-11-09 (draft)

Status: **draft for the owner and the colleague, 2026-11-09.** It follows the
[2026-10-31 review](program-review-2026-10-31.md) and decides nothing; it reads nothing
new. Its purpose: after the strategy v2 families and HYP-015 all stopped, decide whether
the program opens anything more, and what.

## What ran since 2026-10-31

| Line                                    | Result                                                                                                               | Cause                                 |
| --------------------------------------- | -------------------------------------------------------------------------------------------------------------------- | ------------------------------------- |
| HYP-032 compressed spring               | closed at `insufficient_power` (2026-11-07)                                                                          | power                                 |
| HYP-033 squeeze continuation (family 1) | stopped at counts, `insufficient_data`                                                                               | no comparable controls                |
| HYP-034 premium dislocation (family 4)  | stopped after Amendment 1, `insufficient_data`                                                                       | balance on the last hour              |
| HYP-035 relative strength (family 6)    | stopped at counts (PR #565 still open)                                                                               | balance and power of daily books      |
| HYP-036 absorbed buying (family 2)      | stopped at the pilot gate; a changed matching rule failed; a liquid top-10 pilot on new blocks failed (#567 to #569) | coverage and z1 balance, three ways   |
| Family 5, post-pump OI turn             | paused on paper (#569)                                                                                               | MDE 75 to 111 bps                     |
| BTC trend, long or cash                 | no protocol (#570)                                                                                                   | MDE 31 to 50% a year on clean windows |
| Execution edge, limit against taker     | paused (#571)                                                                                                        | powered, but no signal on record pays |
| **HYP-015 hold12h**                     | **reject_hold12h** on Gate B, read 2026-11-05: 3,575 pairs, mean net -0.64% after costs and funding                  | the signal                            |

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

## Facts this review could not confirm

- **HYP-012 v2:** its 2026-10-31 administrative-stop checkpoint result is not recorded
  in the repository.
- **HYP-031:** the registry still says "registered, not opened" for both its years, but
  the HYP-031 worktree holds index, windows and report directories from d87b3a2. I did
  not open them; whether its discovery read ran, and its verdict, is for the owner to
  state.
- **HYP-015's evidence** is in local commit 2d1c582 on `codex/hyp015-formal-evidence`,
  not in `main`.
- **The 10-31 stop rule** (date 2027-01-31, about 4 owner hours a week, a success bar,
  and an early stop if HYP-015 fails, HYP-031 fails discovery and the reserve fails) was
  proposed; whether the owner adopted it is not recorded.

## Where that leaves the program

Under the 10-31 proposal, two of the early stop's three conditions now hold: HYP-015
failed, and the reserve (relative strength, HYP-035) stopped. **The third is HYP-031.**

- **If HYP-031's discovery has not been read:** it is the one prepared line left
  (protocol registered, fetcher and report merged, data plan fixed), so read it, under
  its own rules, as the last line.
- **If it has been read and failed:** the early stop applies now.

What the stop means, as proposed on 10-31: no new research line or stage opens;
collectors that no registered cohort needs move to maintenance (backups and health
kept, closed-line writers stopped); HYP-012 v2 runs to its own terminal (deadline
2027-03-31); the program resumes only for a new external reason the owner states in
writing.

## Position

Mine: do not open another strategy v2 family or a new idea now. Settle HYP-031 (read it
if unread, as the last line), then apply the stop rule if it fails. The record since
10-31 is consistent: the cheap public-history screens work and fail fast, and what they
keep finding is that the edges within reach at USD 50 are smaller than the noise or the
costs. Continuing to open families would spend owner time against that evidence.

Alternatives: (a) adopt the stop now and leave HYP-031 unread, keeping its reserved test
year untouched for a later reason; (b) keep researching under a new kind of edge, which
the strategy's own rule requires to be named and to pass the three-item gate on paper
before any code; none is named here.

## Owner decisions needed

1. State HYP-012 v2's 10-31 checkpoint result and HYP-031's status.
2. Adopt the 10-31 stop rule now, or set another.
3. Open HYP-031's discovery read, if unread, as the last line.
4. Bring the pending records into `main`: HYP-015's evidence, #565, #571.
