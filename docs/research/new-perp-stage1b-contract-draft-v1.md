# I25 Stage 1b: sealed holdout coverage contract draft

Status: **design only, not executable and not a fetch approval**. This document
prepares the next coverage check after discovery's `stage1_pass_partial`. It does
not register an exam, read a holdout response, freeze unimplemented code, or grant
access to any price/outcome. The companion JSON is a design specification, not
an executable request-list hash. No Stage 1b request has been made in this task.

## Prerequisite and permitted decision

The completed discovery v3 companion is bound by these SHA-256 values:

| Artifact        | SHA-256                                                            |
| --------------- | ------------------------------------------------------------------ |
| v3 contract     | `11e5275769540faa7d1b03b3e66cecec074f836c33ff36546afeb75dc1a25de0` |
| v3 report       | `38b569aa5dc713179ded3db70f7cdf788cea560db7c0c99c65d9332a879070b8` |
| v3 evidence map | `532d101483c2691c422e1ddce2187423dfce5d460c4de1b719d626b66645ad03` |

Its run revision is `49edd5d6f8ab07c1d79f6b801b9f9a2b5a5a00e5`. It completed
with no unassessed check or stop code, under the approved partial archive method.
The original discovery, v2 and diagnostic reports remain unchanged. The new
implementation must validate this prerequisite and its sealed inputs locally
before and after any later HTTP run.

Stage 1b may answer only: **is the registered holdout sufficiently recoverable
for a future frozen examination?** A coverage pass is not confirmation, power,
mean return, net profitability, or permission to trade. A negative coverage gate
stops that route; another window cannot silently replace it.

## Window and cohort

- Venue and market: Bybit linear USDT perpetuals. Native catalog labels and
  archive filename labels retain their distinct meanings and provenance.
- Holdout interval: `[2023-02-01T00:00:00Z, 2024-09-16T00:00:00Z)`.
- The cohort is the current catalog's rows with `launchTime` inside this interval,
  from Trading, Delivering and Closed. Current status is not historical eligibility.
  Discovery's 263 symbols are not this cohort or its denominator. The protocol's
  earlier count of 254 is context, not a measurement of this future run.
- Keep the registered ages 3/7/14 days and seven-day hold; V1 decides. For V1,
  `E = ceil_to_minute(T0 + 7 days)` and `X = E + 7 days`. The full path and the
  `E - 30 days` lookback must remain inside the holdout. Compare exact timestamps,
  not an approximate launch-day cutoff. X is the end of the exit bar `[X - 1 minute, X)`: `X == cut` is
  eligible, and only `X > cut` is window-censored. Bar/trade/settlement starts
  remain strictly below cut; funding for an open position uses `E < stamp < X`.
- Never query an out-of-window bar to rescue an entry, lookback, path or sample
  size. Funding/trade/mark coverage requests must be clipped to this window too.
  An internal data gap is not a delisting; no nearest-bar substitution is allowed.
- The 2024-09-16 through 2024-09-30 gap, discovery year, HYP-031 test year and
  HYP-012 v2 blind window are excluded from scientific retention and counting.
  Current catalog/root responses physically span those dates. They are processed
  in RAM with explicit filters, not called historically window-limited responses.

## Two separate authorizations

**Metadata go.** Catalog C1, root C3, directory HTML D1 and first-file HEAD D2h
only. No CSV GET, candles, funding, books, announcements, or AWS. It produces a
private frozen cohort/manifest and an aggregate preflight report, never an M1-M6
pass. Catalog pagination must finish before selecting rows; repeated cursors,
unproven truncation, cap breaches and unavailable required sources fail closed.

Proposed metadata ceilings are intentionally finite and do not promise completion:

| Class |       Logical request ceiling | Per-response cap |  Class byte ceiling |
| ----- | ----------------------------: | ---------------: | ------------------: |
| C1    | 9 pages across three statuses |            3 MiB |              28 MiB |
| C3    |                        1 root |            4 MiB |               9 MiB |
| D1    |      1,500 unique directories |            1 MiB |             384 MiB |
| D2h   |          300 first-file HEADs |     0 body bytes | shared stage budget |

At most 300 in-window catalog rows and 1,200 directories outside that frozen
cohort are admitted to the plan. D1 is the unique union, at most 1,500; it is not
two untracked scans. More rows/directories stops before selecting a smaller set.
Maximum first attempts: `9 + 1 + 1500 + 300 = 1810`. Proposed stage ceilings:
**2,400 actual HTTP attempts and 448 MiB (469,762,048 response bytes)**, including
retries and the 64 KiB read reserve. All caps are simultaneous; unused class budget
is not permission to exceed the stage cap. Each logical request has at most three
sends. These figures are a proposal requiring code, synthetic checks and review,
not an owner-approved budget.

**Body/coverage go.** Only after the metadata manifest is reviewed and sealed.
Prepare a new hash and exact D2-D6 queue with data-dependent counts and limits.
No body-phase budget is assigned by this draft. Reuse neither discovery's 8.5 GiB
nor the metadata allowance. Files larger than a proposed cap are not silently
skipped: stop preflight and resolve the cap/cohort consequence before the first
body read. A fresh approval is required for the resulting manifest and byte cap.

CSV bodies physically contain prices and volumes, and API bodies may contain
rates/OHLC. The later approval must explicitly cover physical receipt while the
reducers retain only timestamps/existence and permitted static catalog fields.
No price/rate/volume is parsed into a scientific value or written to an artifact.

## M2(ii) and historical labels

Apply the reviewed filename-series method at the holdout's own exclusive cutoff,
not discovery's hard-coded `2025-10-01`. Subtract only this frozen holdout catalog
in RAM from the root's lexically valid directory names. Outside-cohort directories
require a complete fresh HTML scan, even if their current names are familiar.
Known catalog-directory contents are fetched for their catalog checks, but this
does not silently broaden M2(ii)'s unknown-directory unit.

- Exactly one canonical pre-cut filename prefix in a directory is required.
- Any mixed/noncanonical pre-cut prefix blocks positive M2(ii).
- Any canonical prefix found in multiple unknown directories or in the frozen
  holdout catalog blocks all its owners, including a prefix in a mixed directory.
- Do not normalize multipliers, infer aliases, infer renames or merge folders.
- Globally admit only after the entire snapshot and integrity checks finish.
  Incomplete scans admit no provisional series. Keep the 16,384 pre-cut prefix-slot
  RAM cap; exceeding it stops the complete scan rather than truncating candidates.
- Later filenames are discarded in RAM before names, scientific counts, collision
  tracking or facts are persisted. Current directory names require their own
  matching pre-cut file before they can be retained as historical labels.

Let N be the frozen holdout catalog count and a the globally admitted unknown
series whose first archive day falls inside the holdout. The unchanged gate is
`a / (N + a) <= 0.05`; do not hard-code discovery's 13/14 threshold. A zero catalog
is insufficient evidence, never a zero-share pass. A known breach outranks other
uncertainty. Any unresolved identity/collision issue makes the exact share null
and blocks a positive result. `archive_only_proven` remains false.

To tell an old series from one beginning in the holdout, the reducer may inspect
pre-window filename dates in RAM. Persist only `has_pre_window_file`, not those
exact earlier dates or a label evidenced solely outside the holdout. A series
with any pre-window file is outside a, even if it continues in the holdout. This
metadata-only exception needs explicit inclusion in the final contract; it grants
no pre-window price, funding, bar or outcome read.

## Sealed facts and allowed public output

The output root is fresh, mode 0700, outside every prior sealed root and outside
Git. No resume, root reset, overwrite, allowance refund or transfer of phase costs
is permitted. The raw discovery cohort is not repurposed as holdout input.

Plans, catalog labels, exact entry/exit timestamps, accepted filename facts,
request targets, cursor tokens and per-symbol receipts are private. Opaque random
request keys replace labels in the public-facing ledger. No request URL, native
symbol, schema tree, raw body hash, retMsg, redirect target or exception detail
may appear in public output/logging. Raw HTML/CSV/API bodies are not retained.
The implementation must explicitly handle C1/D2h privacy; the existing opaque
C3/D1 request class cannot simply be reused for those endpoints unchanged.

Current C1 labels are not asserted to be historical labels. Until a label has
matching in-window filename evidence, keep it only in RAM and persist opaque
uncertainty instead. Do not rescue identity by retaining a later directory name.
Private accepted facts require explicit allowlists per class, with all unrelated
fields discarded. Public reports contain only whole-window aggregate counts,
failure/partial reasons, actual attempts/bytes and provenance hashes. No ticker
list, per-symbol/date result, ranking, monthly return, performance proxy or outcome.

The sealed source must include a complete input hash map. A public companion can
refer to this map's hash, but it must not export its private paths or labels.
Sealed facts are reserved for the frozen coverage/exam workflow and must not be
read to choose parameters or candidates. Metadata receipts containing labels are
not safe to attach to review chats; the aggregate report is the handoff.

## Verdicts and remaining partial checks

Keep the discovery protocol's gates and stop precedence. N, eligible episodes,
reason totals, exposure weeks and every denominator are window-specific. Any
adopted 100-episode/26-week floor must be evaluated from the actual holdout cohort,
not copied from discovery's 228/48. The metadata go cannot evaluate that floor.

The final body/coverage contract must state what it actually proves:

- M1 compares archive-first trade time with catalog launchTime; a bar at E does
  not prove the first nonzero trading-open bar or historical regular-launch time.
- M3's current catalog does not prove independent identity information known at E.
- Coarse M4 gaps over eight hours do not prove all hourly/two-hour/four-hour
  funding settlements. Unknown or clipped pages cannot be labelled complete.
- M5's entry/hour coverage does not prove minute continuity; keep it explicitly
  deferred unless separately contracted. End of archive remains a gap without
  independent halt evidence.
- M6 static catalog fields do not prove executable quantity, depth or a live order.

Incomplete HTTP/source integrity is distinct from a known coverage failure and
from completed partial checks. A failure is not erased by missing evidence. At
most `stage1b_pass_partial` may be proposed after all contracted checks finish;
metadata alone must return `metadata_complete_coverage_pending` or an explicit
stop/incomplete result, never a stage pass.

## Four review clarifications, implemented in the metadata proposal

1. X is an exit-bar end: censor only `X > cut`. A bar ending exactly at cut is
   wholly within the window; no start timestamp at cut is admitted.
2. C1 class bytes are 28 MiB, including room to reserve 3 MiB plus 64 KiB on
   the ninth full response. D2h's shared-budget sentinel is integer zero.
3. Unknown M2(ii) folders retain only a pre-window boolean. Catalog folders
   compute near-launch, day class and ticker-reuse predicates in RAM from all
   pre-cut dates, without persisting dates before the window. A day-proven
   ambiguous class wins first; only an otherwise undecidable pre-window first
   file gives `t0_pre_window_unreadable`. No exact T0 is invented and neither
   HEAD nor CSV GET outside the window is allowed. An early launch's lookback exclusion does
   not silently make its M1 clock known.
4. Both directory classes use the same strict HTML parser. Canonical prefixes
   from catalog folders, including foreign/mixed prefixes, protect the M2(ii)
   frozen collision set. C1 rows lacking an admitted label remain opaque catalog
   slots in N and the M1/M2(i)/M3/reason counters. Prior C1 labels/bases are RAM
   only for relist/multiplier predicates.

Safe catalog labels retain the in-window day calendar for the later M2(iii)
check. A known M2(i) lower-bound failure remains a failure after a later HTTP stop,
provided the catalog denominator and source integrity remain valid.

The [metadata executable proposal](new-perp-stage1b-metadata-v1.md) is separate
from this architectural draft. It implements no body/coverage fetch.

## Implementation and review gates before any request

1. Introduce explicit immutable window configuration for reducers/planners/verdicts.
   Do not monkeypatch discovery globals or silently duplicate the full runner.
   Preserve discovery's existing contract hash, behavior and sealed evidence.
2. Implement private C1/D2h handling, opaque request binding, sealed plans and an
   aggregate-only report. Verify no schema/log/crash path leaks excluded identities.
3. Add synthetic boundary tests at both window ends, lookback/E/X rounding,
   catalog pagination, canonical/mixed/duplicate prefixes, N=0 and exact 5% gates.
   Compare discovery behavior before/after the reuse change.
4. Exercise the real bounded transport against a local fake server for read-reserve
   limits, missing/oversized Content-Length, crash between receipt and stop,
   Retry-After across restarts, and independent metadata/body-phase accounting.
   No external network test is part of this design work.
5. Generate the executable metadata JSON request list, transitive code/publication
   binding and canonical hash; check JSON/doc/code equality. Add a proposed
   coverage-level seen-windows registration. The present draft is not that hash.
6. Obtain code/contract review, merge clean code with green CI, then ask the owner
   for the exact metadata hash and caps. After metadata, review the sealed manifest
   and obtain a separate body/coverage authorization. An exam remains a third,
   later decision after a frozen candidate batch.

Reviewer decisions now: accept this two-go sealed workflow and proposed metadata
caps; confirm the aggregate-only retention and pre-window filename boolean; keep
the explicitly partial M3-M6 claims; confirm that no Stage 3 tuning on holdout
facts is permitted. These are design decisions, not approval of unimplemented
fetch code. AWS, Hyperliquid registration, deployment and orders are out of scope.
