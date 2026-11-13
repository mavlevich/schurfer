# B2 entry replay pilot result, 2026-11-13

**Pilot complete; real-world execution remains unidentified.** The registered
ten-day entry replay ran from clean `d6f0ece`, after its contract and registry
were published by #591. It introduces no new source request and reads no exit,
strategy return, funding payment, test year or blind-window market data.

The primary has 24 decisions on ten original symbol-days. Across the eight
configurations there are 184 overlapping model instances, not 184 independent
trials. The two extra ob200 format fixtures are excluded from this denominator.
No configuration is selected from these results.

## Execution counts with original denominators

The all-unknown-eligible sensitivity assumes otherwise unclassified prints can
reach an ordinary API maker; explicit special prints remain excluded. Counts
below are conditional model outputs, not observed exchange executions.

| Configuration                             | Signals | Post-only rejected | Incomplete source | Strict primary: unknown class | Sensitivity full | Sensitivity partial | Sensitivity none |
| ----------------------------------------- | ------: | -----------------: | ----------------: | ----------------------------: | ---------------: | ------------------: | ---------------: |
| Primary: d30, USD50, 1s arrival, 60s wait |      24 |                  9 |                 0 |                            11 |                9 |                   0 |                6 |
| d15                                       |      20 |                  9 |                 0 |                            10 |               10 |                   0 |                1 |
| d10                                       |      20 |                  7 |                 1 |                            11 |                9 |                   0 |                3 |
| 300s wait                                 |      24 |                  9 |                 3 |                            11 |               11 |                   0 |                1 |
| Zero visible queue ahead                  |      24 |                  9 |                 0 |                            11 |               10 |                   1 |                4 |
| USD25                                     |      24 |                  9 |                 0 |                            11 |                9 |                   0 |                6 |
| USD100                                    |      24 |                  9 |                 0 |                            11 |                9 |                   0 |                6 |
| 5s arrival                                |      24 |                 13 |                 0 |                             4 |                4 |                   0 |                7 |

For every row, rejected + incomplete + sensitivity full/partial/none equals the
original signal denominator. The strict primary's remaining cases have no
qualifying observed sell flow under its fixed counterfactual order-flow model.
They are not actual observed orders. In particular, the excluded-unknown case's
zero queue progress is not an estimate that real fill probability was zero.

The 300s case has only 21/24 = 87.5% source completeness and fails the existing
95% source floor. Its apparently larger full-fill count is not an interpretable
improvement. d10 reaches 19/20 = 95%; other cases have complete book/tape paths
under the fixed checks. None thereby passes native sizing or ordinary-flow
identification: BT is missing, and lot/minimum constraints remain unverified.

The primary sensitivity gives 9/24 = 37.5% full modeled fills across all signals,
not 9/15 after dropping rejected orders. At 5s arrival it gives 4/24 = 16.7%, with
13 post-only rejections. This is descriptive sensitivity on a small selected
source pilot, not a statistically confirmed latency effect or an account PnL.
The equality of USD25/50/100 counts does not justify scaling or prove native
order admissibility. The d15/d10 signal populations also differ from d30.

## Quote-discrepancy split

The separate source-only v3 audit inspected all 12 original/format-fixture pairs.
All core source counts and prior-clock alignment counters reproduce v2 exactly;
only the requested diagnostic split is added.

| Earlier-engine-clock quote diagnostic |    Trades |
| ------------------------------------- | --------: |
| Consistent with preceding quote       | 8,508,452 |
| Inside preceding spread               |   432,934 |
| At/beyond old opposite quote          |   478,546 |
| Total discrepancies                   |   911,480 |
| Prior quote older than diagnostic 5s  |       265 |

Inside-spread trades are 47.5% of discrepancies and old-opposite-quote trades
52.5%. The latter are not automatically corrupt: the book can move between
publications. This split alone establishes neither batching as the cause of
every discrepancy nor eligibility for our queue. Generation `ts` controls
decision availability in the model; engine `cts` is not substituted for receipt
time. Historical feed receive timestamps remain unavailable.

The entry decoder normalizes explicit 0/1 and boolean flags, rejecting any known
special flag even if the other is absent. The source-audit flag-completeness
counter is retained for comparison; it must not be read as proof that all prints
are ordinary. Unknown flow is never silently changed to ordinary volume.

## Provenance and publication

#591 merged at **2026-11-13T15:53:52Z**, main revision
`92b9f94aaa9ba7fa2ffa4bc56db63ca8b37320da`. The retained invocation and publication
artifact establish that the main-published contract preceded both native reads.
This corrects the next-stage order; it does not retroactively change the earlier
local-only B1/B2 publication history.

| Artifact                 | SHA-256                                                            |
| ------------------------ | ------------------------------------------------------------------ |
| Published entry contract | `32f342e040434f2dc03636d296be68a6997e98cbd96f9436d2d33060f9526be6` |
| Native source manifest   | `527b7446887d1f6c466c9891d9f1ef67134dc4b86aaf7a30d3f83c8ddc9bb0a0` |
| Entry replay result      | `941978473831f538c23c39beb2bd28bcd51d949be16929cc778a5a7c477d6ff8` |
| Quote diagnostic result  | `c8008f26069effa7b6adcdac7cdadb34bb612713f3e38906120e7f013c14e366` |
| Count-only summary       | `c3a3c8dff4bd401cfeb5fdfe2e75c4e4d67fe103b94c7434f008eae059879d8e` |

[Evidence](evidence/b2-archive-scope-2026-11-13/entry-replay/) retains the invocation,
main-publication witness, per-ID outcomes, summary, quote diagnostics and code
fingerprints. The entry run streamed 3,816,682,730 inflated bytes in 520.745s;
the full source audit streamed the frozen corpus in 883.178s. They ran concurrently
with local verification, so these times are not standalone performance benchmarks.
No numeric prices or native quantities are emitted by the model report.

The same 24 native bodies were also retained outside disposable worktrees at
project-root `runtime/research/b2/native-source-pilot-527b7446/`. Bodies are hard
links to the immutable capture, with independently copied/fsynced metadata and
all native hashes rechecked. This is retention of the same capture, not another
download or independent sample. The archive-location witness is retained with
the result, so worktree cleanup need not remove the only source copy.

The branch was merged with published main after all native reads. All implementation
files were verified byte-identical to `d6f0ece` after resolving squash-related
add/add conflicts. No original snapshot, failed source result or code revision
was replaced or deleted.

## Verification and decision

Thirty-seven focused tests passed, along with Ruff and strict mypy. Full
`make verify` passed on the normal isolated PostgreSQL at the frozen implementation.
Coverage includes actual ZIP/CSV input, time availability, ties, same-ID conflicts,
resets, gaps, stale/future engine clocks, partials, price priority, eligibility,
publication gating and portable immutable receipts. Post-merge source hashes
match the at-read sources; native v3 core diagnostics equal v2.

This completes the bounded B2 pilot, not the whole strategy. Do not download the
75.8 GB corpus on these counts alone. Before proceeding, decide whether a cheap
fixed all-signal taker economic baseline is informative and what evidence would
justify a maker assumption. B3 would need its own frozen exits, full costs and
actual funding, portfolio/skip accounting, market benchmarks and owner read go.
The adverse-selection comparison of modeled filled/unfilled groups is still
unread. Preserve the full signal denominator and all variant history.

The reserved exit tape is still closed. Its exact necessary discovery windows
must be published before an exit read, with next-day coverage and a separate
budget. The source plan still needs at least 46 more symbol-days to cover the
forecast settlement days; no next-day data was fetched here. HYP-037 remains
closed. The separate power-method note is planning, not a new strategy read.
