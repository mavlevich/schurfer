# HYP-012 v2 identity and accrual audit, 2026-10-01

Status: outcome-blind operational diagnostic, **not** a formal cohort read,
economic verdict, registry amendment, or approval to requalify old captures.
No price, book value, return, PnL, funding or exit value was read. The v2
qualification version, identity registry and cohort remain unchanged.

## Scope and provenance

- Cohort capture window: `[2026-09-29T00:00:00Z, 2026-10-01T11:00:00Z)`.
  The upper bound is fixed for this report; the live cohort continues.
- Production reads: read-only, repeatable-read PostgreSQL transactions on
  2026-10-01 between 11:20 and 11:25 UTC. The separate queries did not share
  one database snapshot, so this is an operational observation rather than an
  immutable formal input bundle.
- Code checked out: clean `2b39857e58a75159a7fd4de47a20f21ba98dd808` before
  this document was added. Qualification version:
  `source_lead_qualified_capture_v4`. Registered v4 fingerprint:
  `7d5f635a4ed02013ad3bd5fb7bd118f5b80979427bf059a130279fa2c3bee189`.
- Frozen candidate snapshot: `source_lead_identity_v4_candidates.json`,
  internal candidate SHA-256
  `0a7eeb501a1b5694d64f3cc46db6789fd6d706a96340833fccb2d58fb73e2348`.
  The independently registered route decisions have internal SHA-256
  `c7f782ec2871fd2a19365441cdc54a38c57974b510fcccc17a2d32bade9d8502`.
- Classification joins production `source_identity_key` to the **committed**
  Gate links and their canonical asset IDs, then checks whether that asset has
  a committed Bybit link. For rejected bases, candidate membership and
  `no_target_perp` come from the committed candidate and decision snapshots.
  Current exchange catalog membership was not used to rewrite historical
  eligibility.

Only `id`, source identity/base, timestamps, statuses, eligibility reasons,
target exchange, and target error **class** were selected from production.
The core funnel joins `app.source_lead_captures` to
`app.source_lead_qualifications` by `capture_id` and exact qualification
version, using `source_first_observed_at` for the window. Target failure
classification joins `app.source_lead_target_observations` by `capture_id`.
The historical comparison uses the same capture status and committed Gate to
Bybit identity links; it does not use qualification or economic outcomes.

## Reconciled cohort funnel

| Stage                         | Captures | Explanation                                       |
| ----------------------------- | -------: | ------------------------------------------------- |
| Captured                      |      156 | Gate source-lead capture rows in the fixed window |
| Excluded before qualification |       49 | `gate_not_unique_first_source`                    |
| Qualified-version rows        |      107 | `156 - 49`; all have `status=excluded`            |
| Source identity unapproved    |      101 | 28 distinct native identity keys, 27 bases        |
| No approved executable target |        6 | Three bases; details below                        |
| Qualified Bybit episodes      |    **0** | Consequently no eligible v2 shadow attempt yet    |

Every one of the 101 `source_identity_unapproved` rows has a non-null source
key. An exact join against all 85 registered Gate keys found **zero**
unexpectedly registered keys: the runtime reason agrees with the frozen
registry. This rules out a simple lookup false negative for these rows; it
does not establish that the registry covers the changing source universe.

### Why the 101 source keys are absent

| Frozen v4 candidate relation                                                     | Cohort rejections |  Bases | What the stored evidence establishes                                                                                                                                                 |
| -------------------------------------------------------------------------------- | ----------------: | -----: | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| In the candidate snapshot                                                        |                62 |     14 | Both Bybit and Binance routes were rejected as `no_target_perp` in the v4 evidence snapshot. These are **snapshot-time** decisions, not a statement about market availability today. |
| Outside the candidate snapshot; first capture after its 2026-09-25T20:00Z cutoff |                29 |      9 | No v4 route decision was made.                                                                                                                                                       |
| Outside the candidate snapshot; first capture before its 2026-09-03 start        |                10 |      4 | No capture fell in the fixed candidate window and these assets were not v3 carry-overs. No v4 route decision was made.                                                               |
| **Total**                                                                        |           **101** | **27** | One base has two source identity keys.                                                                                                                                               |

The 101 rows' two target observations each say
`source_identity_unregistered`. The capture path returns that reason before
opening a target exchange client. Their current Bybit tradability therefore
cannot be inferred from these observations, and none can be retroactively
counted in v2.

### The six registered-source target failures

- Four episodes share one Gate source identity for which v4 approved a
  **Binance-only** route. The Bybit observation is `no_registered_target`;
  the Binance observation is `sampled` but Binance is descriptive, not a
  tradable venue in this cohort.
- The other two episodes have approved Gate to Bybit routes. Their Bybit
  observations are `fetch_failed:target_exchange_unavailable`, error class
  `TimeoutError`, with recorded target latencies of 5,666 and 6,151 ms.
  Both have `identity_verified=false`: the batch failed before an exact
  target market was resolved. The worker wraps its public `load_markets`
  call in the configured 5-second timeout. A single bounded,
  read-only public Bybit catalog load from the production analytics image on
  2026-10-01 completed in 1,525 ms for 3,787 markets. That later check does
  not explain the earlier timeouts or prove that the failure has ended.

## Accrual denominator and decision

The frozen registry has 44 Gate identities with a registered Bybit target.
Their capture counts, computed with exact native source identity, were:

| Window                                            | All captures | Source-eligible captures | Registered Gate to Bybit captures | Source-eligible among those |
| ------------------------------------------------- | -----------: | -----------------------: | --------------------------------: | --------------------------: |
| Candidate window, 2026-09-03 to 2026-09-25 20:00Z |        1,728 |                      952 |                               133 |                          42 |
| Warm-up, 2026-09-25 20:00Z to 2026-09-29          |          181 |                      110 |                                 9 |                           6 |
| V2 cohort through 2026-10-01 11:00Z               |          156 |                      107 |                                 9 |                           2 |

The candidate-window 42 source-eligible captures span about 22.8 days, or
about 1.8 per day **before** target sampling, book and qualification checks.
At that historical rate, 28 days would supply about 52 possible episodes,
below v2's 100-resolved floor; reaching 100 would take about 54 days even
if every such capture qualified and resolved. This is an illustrative
capacity calculation, not a forecast: the candidate window selected assets
with leads, event rates vary, and the cohort's first 59 hours produced only
two source-eligible captures on registered Bybit routes.

**Conclusion:** the immediate zero is explained by the registered universe
and two observed target timeouts, not by a stopped scanner or an incorrect
source-key lookup. The data do not justify altering v2's registry, retrospectively
qualifying captures, or reading its economic outcomes. Continue an
outcome-blind funnel check; distinguish a recurring Bybit timeout from an
isolated failure if more registered-route signals arrive. At the planned
2026-10-31 reassessment, use only accrued counts and coverage to decide
whether an administrative stop/amendment is warranted before any formal
outcome read. A broader identity universe requires its own evidence,
registration and prospective cohort.
