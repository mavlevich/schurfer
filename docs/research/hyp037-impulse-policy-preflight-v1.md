# HYP-037 impulse policies, preflight registration v1

Registered 2026-11-11 before this study's first historical invocation. The owner
selected this route and instructed continuation after the plan and synthetic code
slice. This registration covers counts and planning dispersion only. Historical
trade-return reading requires the successor's outcome contract and review first.

## Question and lineage

Compare two Bybit USDT linear-perpetual candidates on the reused discovery window.
A is the first-impulse rule. B applies HYP-033's negative-funding and growing-OI
filter to A. Both are selectable candidates. Their absolute net will have a
two-candidate correction. The incremental mean B minus A will be diagnostic.

The definitions and staged delivery are in the
[implementation plan](hyp033-policy-comparison-plan-v1.md). HYP-033 v1 remains
stopped on its original matched-comparison floor. A is also related to the closed
impulse/momentum lines. This successor has a new ID and a changed estimand. The
owner's continuation authorizes this specific preparation and outcome-blind
preflight exception to the 2026-11-09 program stop and the untouched-window rule.
The reused year is discovery-only and already features-exposed. Its known v1
counts and power are part of the selection history and multiplicity record.

## Fixed preflight scope

- Hypothesis ID `HYP-037`, two registered variants `A` and `B`.
- Source is the existing HYP-032 snapshot `features-8522f0c.jsonl.gz`.
- Required SHA-256 is
  `a3b54d6ce092a73afbd4911e8ea72862a707846b45fbb23d05ec1d91f71ebab6`.
- Source window is 2024-10-01T00:00:00Z to 2025-10-01T00:00:00Z, exclusive.
- Decisions use v1's first decision 2024-10-16T00:00:00Z and its existing last
  admissible 4-hour decision, including the entry delay and exit boundary.
- Native instrument identity, live/delisted universe, listing/delivery caps and
  event definitions reuse `bybit_squeeze_continuation.py` and the features loader.
- A includes all liquid first impulses with a valid waiting bar. Undefined F or G
  remains in A. B requires both F and G to be determinably true.
- The source's 1-hour closes, funding and OI are read to compute pre-entry
  features. Pre-impulse 4-hour return dispersion is allowed for planning MDE.
  An earlier event's path can occur inside a later event's pre-entry scale.
  Output contains scales summarized as MDE, never a setup mean or net return.
- Planning power retains v1's inflation 1.5 and design effect 1.2, with alpha
  0.025 per candidate and target power 0.80. These are assumptions, not measured
  post-event power. The 50 bps limit is reported for each candidate.
- Unrestricted simultaneous holds use the registered entry delay and H=4 hours.
  Report their peak before proposing a capital-constrained implementation.

No new requests are required. The source hash is verified before parsing and again
before sealing the result. The registry entry precedes the local snapshot read.
The HYP-031 reserved year, backward holdout, HYP-012 v2 blind window and production
data are outside this invocation.

## Binding and outputs

The adjacent [machine registration](hyp037-impulse-policy-preflight-v1.json)
contains the computed contract constants and this document's SHA-256. Invocation
requires its SHA-256 and verifies that its contract equals the running code's
preflight contract. The protocol document is verified before any feature parse.
Altered input hash, contract, stage, ID or decision boundary refuses the run.

The CLI records actual Git HEAD, actual dirty-tree state and content hashes of its
code dependencies. A source change during calculation refuses sealing. A local
dirty-tree run is identified as such, with exact source fingerprints. It does not
claim a clean merged-code evidence run.

An output directory is created exclusively for one invocation. `run.json` binds
the registration, protocol, features and sources before calculation. `events.json`
contains frozen native IDs, decision timestamps and B membership, linked to the
run hash. `counts.json` is published last and references both preceding hashes.
Publication reuses the repository's atomic `publish_once`. An interrupted run
without `counts.json` is unsealed. Use a new directory to retry. Existing output
directories are refused, including empty ones, and sealed files are never replaced.

The report contains the funnel, intended counts, failed instruments, F/G
determinability, symbol/week concentration, pre-entry planning MDE and peak
simultaneous holds. It has stage `preflight_only` and makes no profitability or
promotion decision. If counts reproduce the old filtered set, record that check.
Different counts are investigated before moving to any outcome read.

## Next decision

Review the resulting A/B evidence size, planning assumptions, input integrity and
required minute-data scope. Freeze and register the empirical-read contract before
fetching entry/exit minute bars or funding marks inside the holds. The backward
holdout is one frozen confirmation batch. HYP-031's reserved year remains separate.
