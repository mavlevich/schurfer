# Consilium: one place for ideas, positions and open points

Status: **working folder for discussion; decides nothing, registers nothing and reads
no data.** A decision becomes binding only when it is written into its own authority
(the [program plan](../research/program-plan-2026-11.md), a contract, the
[decision register](../research/decision-register.md)) through a normal PR.

The owner opened this on 2026-11-15: every participant brings their own questions and
ideas, and nothing is off the table. Rules, architecture, capital and tactics may all
be questioned here.

## Files

| File                       | What it holds                                                   | Who edits                                |
| -------------------------- | --------------------------------------------------------------- | ---------------------------------------- |
| [`state.md`](state.md)     | What is agreed, what is open, corrections, rules under question | anyone; one line per change, with a date |
| [`ideas.md`](ideas.md)     | Every trading idea and lever, with its screen status            | anyone; add a row, never delete one      |
| [`positions/`](positions/) | One file per participant and round: their own argument          | only its author                          |

## How to take part

1. **Start from current `main`.** Put the commit you read at the top of your position
   (`Read at: <short sha>`). Two rounds of this discussion were spent on advice written
   against a stale checkout.
2. **Write your own file** in `positions/` named `<letter>-<date>.md`. Do not edit
   another participant's file; answer it in yours.
3. **A fact needs a reference**: a file, a line, a PR, or "from memory, not checked".
   A claim about production needs the date it was observed.
4. **Per point: agree, agree with a different fix, or disagree**, each with a reason.
   "Accepted" without a reason is not an answer.
5. **Move the result into `state.md`** when a point is settled or stays open, and into
   `ideas.md` when it is an idea. The position files are the argument; the two shared
   files are the summary everyone reads first.

## Participants

| Letter | Who                            | Files                                                    |
| ------ | ------------------------------ | -------------------------------------------------------- |
| A      | author of the first position   | [`positions/a-2026-11-15.md`](positions/a-2026-11-15.md) |
| B      | participant with four reports  | to add                                                   |
| C      | participant with three reviews | [`positions/c-2026-11-15.md`](positions/c-2026-11-15.md) |
| Owner  | decides; answers in `state.md` |                                                          |

## What this folder may not do

- Read prices, returns or spreads on any window, or anything inside a blind window.
- Change a frozen contract or a registered rule. It can only propose the change.
- Authorize a fetch, a deploy or a live order.

## Latest review

2026-11-15, C: [PR #600 at 17e8f3b](reviews/pr600-17e8f3b.md): request changes;
two reproduced completeness blockers. This review does not authorize a fetch.
