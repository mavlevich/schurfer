# Resume readiness: what a new data window can restart (planning note v1)

Status: **planning note, 2026-11-10; decides nothing and registers nothing.** Asked by
the owner after the [venue cost check](venue-cost-check-v1.md) found no cost change. The
[2026-11-09 program review](program-review-2026-11-09.md) resumes the program only on a
new independent window, a measurable cost change or a data source that removes a stop's
cause. This note asks two questions about the first one: when can a new window open, and
which stopped line would it actually restart?

It reads nothing new. Every number comes from a recorded result: the HYP-012 v2 rule and
its 2026-10-31 checkpoint, and the stop records of the lines below. No count of v2
episodes after 2026-10-31, no price and no return was read for it.

## 1. When the HYP-012 v2 blind window can end

The window ends only at a terminal state of v2: its formal read or a recorded
administrative stop ([rule](source-lead-forward-cohort-v2-administrative-stop.md)). The
cohort started on 2026-09-29; the capture deadline is 2027-03-31.

The only recorded count is the 2026-10-31 checkpoint: **20** matured tradable qualified
episodes in 32 days, about 0.63 a day. Held constant, that rate gives:

| Checkpoint | Day | Minimum to continue | At 0.63 a day | Outcome at that rate |
| ---------- | --: | ------------------: | ------------: | -------------------- |
| 2026-11-30 |  62 |                  28 |            39 | continue             |
| 2026-12-31 |  93 |                  45 |            58 | continue             |
| 2027-01-31 | 124 |                  64 |            78 | continue             |
| 2027-02-28 | 152 |                  81 |            95 | continue             |

**This is a scenario at the October rate, not a forecast.** At that rate the 100th
_matured_ episode arrives around **2027-03-08** (day 160). The formal read needs 100
_resolved_ episodes over at least 4 UTC weeks, and a matured episode is not yet a
resolved one, so under this scenario the read runs in March at the earliest. If accrual
speeds up, the read can come earlier; if it slows, a checkpoint stops the cohort. Without
100 resolved episodes, the reader records `checkpoint_unreached_at_deadline` 24 hours
after 2027-03-31.

A checkpoint stop ends the blind window **when the `admin_stopped` row is recorded**
([terminal state](source-lead-forward-cohort-v2-administrative-stop.md#the-terminal-state)),
not automatically on the checkpoint date, and the closed windows of the cohort's
episodes stay closed for good. A stop at 11-30 needs fewer than 8 new matured episodes
since 10-31, under a third of the October rate. The 11-30 checkpoint is evaluated with
the registered command on the owner's go. One month of accrual is a thin base; clustered
pumps or outages can move every date above either way.

## 2. Which windows exist

| Window                                                 | Length      | Status                                                           | Usable for                                                                                                                |
| ------------------------------------------------------ | ----------- | ---------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| Forward, 2026-09-29 to v2's terminal state             | ~5-6 months | blind until v2 is terminal                                       | a study registered after the terminal state; historical for it, never prospective; minus v2's closed windows after a stop |
| HYP-031's reserved test year, 2025-10-01 to 2026-09-28 | 363 days    | untouched for HYP-032 to HYP-036 ([ledger](discovery-ledger.md)) | reserved for HYP-031's seconds protocol ([minute protocol](bybit-post-funding-minute-v1.md)); its release is a decision   |
| Backward holdout, 2023-02-01 to 2024-09-15             | ~19 months  | unread beyond coverage                                           | one frozen confirmation batch only ([strategy](research-strategy-v2.md)); not discovery                                   |

HYP-032 to HYP-035 used a 350-day window (decisions 2024-10-16 to 2025-09-30).

## 3. Does more data restart a stopped line?

An MDE scales with the inverse square root of the number of independent days, if the
event rate and the dependence per day stay the same. Against the 350-day window, adding
the reserved year multiplies an MDE by **0.70**. A forward window of about 160 days used
alone multiplies it by **1.48**; pooled with the old window, by **0.83**. The table uses
each line's recorded stopping items.

| Line                                                             | Stopped on                                                                                                       | Kind               |                   MDE now, bps | + reserved year | Forward alone | Restarted by more data?                                                                                                        |
| ---------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------- | ------------------ | -----------------------------: | --------------: | ------------: | ------------------------------------------------------------------------------------------------------------------------------ |
| [HYP-032](bybit-compressed-spring-v1.md) compressed spring       | leg (b): the 5 weeks with most losses hold 38.4% of lost clusters but 21.3% of event clusters (cap 30%)          | coverage           | (a) 44.7 passed (not the stop) |        (a) 31.3 |      (a) 66.1 | no grounds to release the year: more weeks can lower the top-5 share while the transplant keeps losing the same kind of events |
| [HYP-033](bybit-squeeze-continuation-v1.md) squeeze continuation | controls for 61.2% of events (floor 80%); (b) MDE 61.5                                                           | structural + power |                       (b) 61.5 |        (b) 43.1 |      (b) 91.0 | no: the control share is a ratio and does not grow with days                                                                   |
| [HYP-034](bybit-premium-dislocation-v1.md) premium dislocation   | controls for 70.9% (floor 80%); (b) MDE 50.13                                                                    | structural + power |                       (b) 50.1 |        (b) 35.1 |      (b) 74.1 | no: same reason                                                                                                                |
| [HYP-035](bybit-relative-strength-v1.md) relative strength       | balance 0.132 (gate 0.10); MDE 88.7 and 66.7                                                                     | structural + power |                       (a) 88.7 |        (a) 62.1 |     (a) 131.2 | no: (a) stays above 50 and the balance does not change                                                                         |
| [HYP-036](bybit-absorbed-buying-v1.md) absorbed buying           | controls 56.4% (floor 85%); z1 balance 0.175; [matching check](bybit-absorbed-buying-v2-matching-check.md) 73.2% | structural         |                   power passed |                 |               | no                                                                                                                             |
| [Family 5](next-candidate-feasibility-v1.md) against pump coins  | MDE 110.8 and 75.3                                                                                               | power              |                      (a) 110.8 |        (a) 77.6 |     (a) 163.9 | no                                                                                                                             |
| Liquid top-10 pilot (same note)                                  | coverage 80%, z1 0.145; MDE 48 passed                                                                            | structural         |                      48 passed |                 |               | no                                                                                                                             |
| [BTC trend](btc-trend-feasibility-v1.md)                         | needs about 1,280 weeks (25 years)                                                                               | power              |                                |                 |               | no                                                                                                                             |
| HYP-031 minute, HYP-015                                          | `fail`: the effect is smaller than the costs                                                                     | result             |                                |                 |               | no (closed)                                                                                                                    |
| HYP-031 seconds (reserve)                                        | source gate: trade classes in the public archive; publication delay to be measured live                          | data source        |                                |                 |               | not a window question                                                                                                          |

**No stopped line is restarted by data alone.** Most stops are structural. A control
share, a balance or a coverage rate is a property of the design, not of the sample size.
The lines that stopped on power need far more than any window can give. HYP-032 is no
exception: its power leg (a) already passed, and its stop on leg (b) is a systematic
loss in the transplant. More weeks could lower the share of losses in the top 5 weeks
without removing that loss, so passing the item that way would not remove the cause. A
new window helps only a design that would pass on its own terms with more days, and the
record holds none.

## 4. Position

Mine; the colleague reviews before merge.

1. **HYP-012 v2 is the only line that can still give a result**, and it most likely
   resolves in March 2027. Its accrual must not be broken by the maintenance work:
   before each group 2 stop, check whether v2's capture uses that source (decision 5 of
   the program review).
2. **The 11-30 checkpoint** is the first new information: it either keeps v2 open
   (the October-rate scenario) or stops it, and then the blind window ends when the
   stop is recorded.
3. **Do not release HYP-031's reserved year now.** No stopped line has grounds to expect
   it to remove its cause, and it is the only clean year for the one line with a gross signal (the first-minute
   diagnostic, +68.6 bps gross against a +30 bar).
4. **Before 11-30, only HYP-031 seconds' archive schema.** Check the public trade
   archive's schema and the meaning of its RPI and block-trade flags against Bybit's
   documentation and the discovery materials already allowed. A flag the archive does
   not carry stays `unknown`. This reads no return. Even if the flags check out, a
   resume still needs an observed publication delay of the rate and an executable price
   after it; the forward capture for that on `research/hyp031-seconds-tape` stays gated
   on v2's terminal state.

Alternatives, not recommended:

- Release the reserved year for a HYP-032 v2 power step. It costs HYP-031 seconds its
  confirmation year with no grounds to expect the stop's cause to go away.
- Redesign the control rule of HYP-033 or HYP-034 under the third route ("controls a
  stopped design lacked"). It needs a new id and a reason why it differs, and its power
  on one year is still near the 50 bps limit.
- Stay idle until the 11-30 checkpoint.
