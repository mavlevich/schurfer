# 2026-10-31 direction decision: preparation brief

Status: **preparation only, written 2026-10-07.** It decides nothing, reads no cohort and
no outcome, and changes no registered rule. It lists what the decision of 2026-10-31
chooses between, which inputs will exist by then, and what each answer starts, so the
day itself needs no improvised data gathering.

## What is decided

The [priority queue](../../ROADMAP.md) (item 4, and the reassessment rule of the
pump and early-flow domain) leaves one choice for 2026-10-31:

- **A.** start at most one bounded prospective collector (PR 6 onward: Gate pre-move
  data, the source PR 3 selected), or
- **B.** start no collector, stop strategy-specific expansion in the pump and early-flow
  domain, and move to the spot and perpetual carry line.

The queue keeps one primary and one support implementation PR at a time, so A and B are
not run side by side. Neither answer stops HYP-012 v2, reads HYP-015, or opens any
sealed data: those keep their own registered rules and dates.

## Inputs, and when each exists

| Input                                    | Where from                                                                      | Ready by    | Reads an outcome?  |
| ---------------------------------------- | ------------------------------------------------------------------------------- | ----------- | ------------------ |
| HYP-012 v2 checkpoint (12 qualified)     | `make prod-hyp012-v2-administrative-stop`, scheduled                            | 2026-10-31  | no (counts)        |
| Disk growth and Gate budget (PR 2b)      | the daily readings since 2026-10-04, scheduled analysis                         | ~2026-10-12 | no                 |
| Fresh disk reading and recomputed budget | the daily timer on 2026-10-31, recomputed for A's universe                      | 2026-10-31  | no                 |
| Gate collection budget (PR 5 + erratum)  | `docs/research/gate-collection-budget-v1.md`                                    | now         | no                 |
| Carry execution canary (#476)            | `bybit_carry_feasibility`, runs on or after 2026-10-31                          | 2026-10-31  | no (books, limits) |
| Carry account preflight, stage A (#504)  | the owner's Bybit account                                                       | partly now  | no                 |
| Domain results so far                    | discovery ledger: HYP-012b, 012c, 028, 029, 030 (cost and exits), abnormal-flow | now         | already read       |
| HYP-015 hold12h verdict                  | registered read                                                                 | 2026-11-04  | yes, after 10-31   |

Notes:

- **Bybit perpetuals are tradable** on the owner's account (confirmed 2026-10-07), so
  the execution venue for either answer is Bybit.
- **HYP-015 is read after this decision.** Its result does not change A or B; a positive
  verdict authorizes only its own next gate.
- **The burst probe's sealed costs** stay sealed until v2 is terminal and a separate
  readout is registered; they are not an input here.

## What the domain evidence says so far (already read, nothing new)

Every candidate in the pump and early-flow domain that reached an after-cost read has
failed or stayed unproven: HYP-012b (all venues negative), HYP-012c and HYP-029 (fail),
HYP-028 (not registered after exploration), the abnormal-flow screen (negative at every
threshold), and HYP-030 (costs below the scenario, but the typical trade near zero, 94%
of the net from five instruments, and no exit rule that survives the test half). The
open forward cohorts are HYP-012 v2 (weak prior) and HYP-015 (read 2026-11-04).

## Decision rule proposed for the day

1. **The disk is checked again on the day.** PR 2b (about 2026-10-12) is nearly three
   weeks old by then. A is possible only with, on 2026-10-31:
   - a fresh disk reading from the daily timer, its health green;
   - the growth rate over the readings since PR 2b, not PR 2b's figure;
   - the budget recomputed for the exact universe A would collect, with the reserve.

   If any of these is missing or failed, A is not allowed and the A-or-B decision is
   postponed until they exist (B's canary may still run, it needs no disk). If they
   show the collection does **not** fit, A is unavailable: choose B.

2. If it fits, A still needs a stated reason to expect a survivor that the failures above
   do not already contradict. Without one, choose B; a collector with a weak prior
   costs months of disk and attention.
3. B proceeds only through its own gates: the canary (#476) runs; if at least 10 exact
   pairs pass in two of three rounds, stage A of #504 is completed on the owner's
   account; stage B is registered only after HYP-012 v2 is terminal.
4. If the canary fails as well, both lines stop, and the next step is a written review of
   the whole research program before any new line is opened.

## On the day

1. Read the v2 checkpoint report (scheduled task).
2. Take a fresh disk reading: `make prod-disk-growth-run` on the host (it starts the
   reading service, then runs the health check). `prod-disk-growth-health` alone is not
   enough: it accepts a reading up to 36 hours old, so on 2026-10-31 a file from
   2026-10-30 would pass. Then confirm that the newest
   `runtime/research/disk-growth/reading-<UTC>.json` is dated 2026-10-31 UTC and matches
   its `.sha256` sidecar, and record that file name and hash in the decision.
3. Recompute the Gate budget from that exact file, as in rule 1.
4. Apply the rule above and record the choice and its reason in the ROADMAP.
5. If B: run the canary from a clean `main` and publish its artifact; then stage A.
6. Keep the 2026-11-04 HYP-015 read and the 2026-11-04 burst-probe end check as
   scheduled.
