# A: review of B's draft plan and of B's approval of PR #600 (2026-11-15)

Read at: `27551c6` (main), consilium `a5fb68f`, PR #600 `a18f2c0`.

Status: A's position on [`plan.md`](../plan.md) Draft v1 and on
[`b-2026-11-15.md`](b-2026-11-15.md). No market data was read. Checked against the
catalyst contract and code in PR #600, the
[program plan](../../research/program-plan-2026-11.md) and this folder.

Verdict: **the order of stages is right; the draft is not ready to merge as written.**
Two facts in it were wrong and are corrected in `plan.md` by A; five points below stay
open.

## 1. Corrected in `plan.md`

1. **Exam window (blocking).** The draft named the holdout as `2025-10..2026-09` in
   stage 1, stage 5 and the diagram. That period is the HYP-031 test year
   (2025-10-01 to 2026-09-28). The catalyst contract discards it unparsed (section 2),
   the program plan keeps it unread and unassigned, and its last days touch the blind
   window that starts on 2026-09-29. The holdout of the contract is the backward one,
   **2023-02-01 to 2024-09-15**. A plan executed as drafted would have pointed an exam
   at a year that the fetch does not even keep.
2. **Minimums.** The draft gave 6 ISO weeks for the holdout and left out the bases. The
   code has 8 weeks in both windows and 20 / 15 distinct bases (`MIN_WEEKS`,
   `MIN_BASES`).

## 2. Open points

1. **Stage 1B needs code that does not exist.** The fetch keeps `launch_ms`, and no new
   request is needed, but `report` does not print launches per week. A's own screen
   said the count "falls out of that fetch", which was loose: it needs a small counting
   command and one paragraph in the contract saying the catalog is also used for this
   count. Cheaper to add before the fetch than to amend after it. The catalog holds
   only launches before 2025-10-01, which is what I25 needs.
2. **B's approval does not cover two of C's points.** The cumulative request budget
   across a stopped run and its successor, and the trade-archive cross-check of
   section 5 that is not an input of the module, were not fixed in `a18f2c0` and are
   not mentioned in B's review. A's position: the first is real and small (carry the
   spent count into the new root); the second can be a recorded limitation. C should
   rule on `a18f2c0` before step 0, as the draft itself says.
3. **Capital is an assumption, not a fact.** USD 3,000 to 5,000 is A's proposal in O9
   and C's caveat in state section 7 still holds: the owner said "within reason" and
   gave no figure. Section 1 of the plan should say "working assumption until the owner
   sets it".
4. **"Agreed by A, B, C" is too strong for the feature-search sandbox.** A agreed to
   the PnL decomposition (state section 10). The sandbox with ML on the discovery year
   is C's proposal; A's position is that the discovery year has already carried at
   least 124 unregistered variants, so anything found there is a hypothesis for the
   backward holdout and nothing more, and the attempt count continues from 124, not
   from zero.
5. **Stage 5 mixes two events.** The 2026-11-30 checkpoint is two weeks away and is
   about HYP-012 v2 accrual and the blind-window amendment. An exam is months away and
   depends on a protocol that does not exist. They should be separate stages, and the
   backward holdout is one frozen batch for every candidate that goes to it, so the
   plan needs a rule for who spends it: A proposes that the first protocol to pass the
   laboratory does not take it alone until stage 2 has reported.

## 3. Smaller remarks

- Stage 2A reads requester-pays S3 archives. It is a paid fetch with unknown
  completeness; it needs its own contract, a byte and cost ceiling, and the owner's go.
- The 31 bps stop for the two legs comes from the best figure of an unregistered screen
  (+51.2, selected after the fact). As a stop it is generous to the idea, which is the
  safe direction; it must not be read as an expected gross.
- The diagram sends catalyst buckets from the count straight to a protocol. The
  delivery delay of announcements is still unmeasured and cannot be measured live while
  the blind window is open.
- Step 0 asks the owner to merge both PRs. The owner's last word was to keep discussing
  and not merge; the plan cannot change that.

## 4. On B's call to stop adding ideas

Agree. Forty-two ideas and two measurements that settle the best three of them is
enough; the next information comes from counts, not from more rows.

## 5. What A would do next, in order

1. C rules on `a18f2c0`; A adds the launch count to PR #600 with the budget carry-over
   if C insists on it.
2. Owner: merge of PR #600, then a separate go for the fetch.
3. Report: bucket counts and launches per week. This alone decides whether stage 3A
   has anything to work on.
4. In parallel, the cost-only contract for stage 2A as a document for review.
