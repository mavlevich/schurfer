# B1 prior-only 4h discovery result, 2026-11-13

**Verdict: `pass` for 4-hour contracts at d30, d15 and d10.** The pooled S1 formula
gate and the 4h group's own S1 and S3 gates pass. This releases B2's data/fill-model
work for these offsets under the program plan. It does **not** establish positive
gross/net returns, reliable maker fills, or permission for live trading. B3 outcome
reading and the reserved test-year examination remain separate stages.

## Reproduction and inputs

Calculated on clean local revision `81758783a7454ff592ea899f952a23a0f2aa9cad`,
after the completed supplement was sealed and its SHA pinned in that commit.
The registered report version is
`bybit_presettlement_feasibility_prior_only_4h_v2`. Only the supplemental pin/comment
changed in the calculation module since the exhaustive classifier-containment
audit; no filter, formula or threshold changed for this read.

[Evidence](evidence/b1-prior-only-4h-2026-11-13/read/) retains the byte-exact result,
execution manifest and derived S3 identities. Result SHA-256:
`96ed96d58514815203aa221a35bce5ed19a37b9b5316947148375a033170311e`.
The actual report call, source fingerprints, input digests, clean-tree state and
timing are retained in `run.json`. Runtime: 149.504 seconds; maximum RSS on macOS:
2,317,598,720 bytes. This is a local result, not a deployment or a main-branch claim.

| Input                            | SHA-256                                                            |
| -------------------------------- | ------------------------------------------------------------------ |
| HYP-031 discovery index          | `f48fe09f7023a476e524cf70c6ec1b629eee30a9a07cff41c9d149dfb59e0cf5` |
| Original minute premium          | `5d638d68d40a13e2ad10573ae7d6446a193a66b0db8dc4d1d12e20c0e9a58b15` |
| Prior-only 4h supplement         | `3fd3205c7f6f2ab129db6d77b4b3e514a7416d1aa6fbb010b3ee577bc9a6dfc8` |
| Hourly premium, diagnostics only | `8c3d81a765e993cfbc67444f5ac39f10be79e11567346b66e08c38e861e93919` |

The supplemental transport ran from clean `1cc0949`. The fixed pilot passed 64/64;
the full run completed 11,660/11,660 with exactly 11,660 attempts, no retries, and
167,326,720 conservatively reserved body bytes including the pilot. The immutable
[fetch inventory](evidence/b1-prior-only-4h-2026-11-13/fetch/inventory.json), original
binding and per-receipt digests are retained. Native receipts, SQLite ledger and
the 21,144,541-byte supplemental gzip remain in the worktree's runtime directory.

## S1: both formula gates pass

The prior-only 4h candidates number 17,892. Their union with the unchanged original
2,000-item validation sample is 19,845, with 47 overlapping IDs. Validation sample
membership selects no S3 event. The S1 4h subset also includes the sample's other
4h settlements, so its pool is larger than the S3 candidate set.

| Check                               |       Pooled S1 |           4h S1 | Fixed requirement |
| ----------------------------------- | --------------: | --------------: | ----------------: |
| Pool intervals                      |          19,845 |          18,789 |       fixed union |
| Schedule held / complete            | 19,844 / 19,844 | 18,788 / 18,788 |     coverage ≥95% |
| Scored relevant settlements         |           3,534 |           3,514 |              ≥300 |
| At/below / above the −0.30% trigger |   1,463 / 2,071 |   1,458 / 2,056 |          ≥50 each |
| Formula within 2 bps                |        96.7459% |        96.7274% |              ≥95% |
| Trigger agreement overall           |        99.6038% |        99.6016% |              ≥98% |
| Sensitivity                         |        99.5899% |        99.5885% |              ≥95% |
| Specificity                         |        99.6137% |        99.6109% |              ≥95% |
| Verdict                             |          `pass` |          `pass` |     both required |

The one forecast whose schedule did not hold is outside S1's registered scoring
denominator, not missing minute data. The possible-cap exclusions remain explicit:
101 pooled and 100 in 4h. Another 48 scored observations equal standard cap values
but do not meet the repeated-minimum exclusion rule; they are not removed after
seeing the result.

An aggregate pass is not exact reconstruction for every instrument: 115 scored
4h observations exceed 2 bps, with a worst absolute gap of **2,027.93 bps**. The
strict 0.5-bps diagnostic passes only 83.18% in 4h. Caps are not reconstructed and
the computed signal is not claimed to equal the rate Bybit displayed before t.
These limitations remain in force for B2/B3; no new outlier exclusion is introduced.

Hourly/minute inconsistency also remains: 53.17% of the 4h pool's compared minutes
fall outside hourly high/low. The hourly guard was explicitly retired in the
registered amendment after its earlier failure because hourly premium no longer
selects the universe. This result does not repair or validate that hourly envelope.

## S3: all registered offsets pass the 4h floor

Every offset has 17,892/17,892 complete eligible prefixes. No event was selected
from the validation sample alone or removed using the eventual settlement.

| Decision before forecast settlement | Events | Symbol-day clusters | Symbols | ISO weeks | Top-5 symbol share | Floor  |
| ----------------------------------- | -----: | ------------------: | ------: | --------: | -----------------: | ------ |
| 30 min                              |  1,637 |                 718 |     150 |        46 |             16.16% | `pass` |
| 15 min                              |  1,594 |                 701 |     150 |        46 |             16.26% | `pass` |
| 10 min                              |  1,574 |                 693 |     147 |        45 |             16.31% | `pass` |

The floor remains 150 clusters, 40 symbols, 26 weeks and top-5 share at most 30%.
All three offsets survive; none is selected by an observed trade return. Pooled S3
and every non-4h S3/class verdict remain `not_fetched`. The 75 qualifying off-class
validation rows do not enter an event list.

The derivative `events.json.gz` freezes native symbol, interval start, forecast
settlement, decision time and the premium observation cutoff for each offset.
It uses the exact same `classify` and verifies every status and event count against
this accepted result. It contains no eventual settlement label, price or return.
Its SHA-256 is `4f1554108d6da503630d24d5b123d8000532b7bb3decb33f033e57dab65711f7`.
The export records the later documentation/evidence-only dirty state explicitly;
all calculation-source hashes still match the accepted clean report. An initial
export comparison stopped before publishing IDs because in-memory integer object
keys differ from JSON's string keys. The successful comparison normalizes only
that serialization, without numerical tolerance or a selection change.
Across offsets, the identity inventory contains 4,805 decision points on 1,685
distinct forecasts and 151 instruments. They touch 724 decision symbol-days;
this is archival-scoping metadata, not an inference cluster count or the final
B2 download scope, which still depends on its placement/wait/remainder policy.

## Exposure and next step

This read uses discovery features only. Premium can overlap a hypothetical holding
path; that exposure was registered before fetching. No perpetual price, strategy
return, reserved HYP-031 test year, backward holdout or HYP-012 v2 blind-window
data was read. Earlier exploratory returns and the post-hoc choice of 4h recorded
in #582 remain part of the selection history; this pass does not erase them.

Proceed to B2: define the maker placement/wait/remainder policy and estimate the
bounded historical book/tape scope for the frozen event IDs before downloading it.
Retain all missed/partial signals in the denominator. No inference about earnings
can be made until execution assumptions and the subsequent economic read pass.

## Verification

- 101 targeted B1 transport/calculation/scope tests passed after the supplemental pin; calculation
  module Ruff and commit hooks passed.
- The transport previously passed 22 tests and full `make verify`. The full gate
  was not repeated for this pin-only change and evidence report.
- The actual report checked every input digest and conflict-safe merge from a
  clean revision. No threshold or classifier change was made after this read.
