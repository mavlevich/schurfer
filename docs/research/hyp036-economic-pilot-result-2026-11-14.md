# HYP-036 economic pilot result, 2026-11-14

**Verdict: `stop_spending`.** HYP-036's short at this trigger and horizon gets no
further budget. The rest of the discovery tape (about 520 GB) is not bought. This
was a spending decision under the fixed [contract](hyp036-economic-pilot-v1.md),
`hyp036-econ-v1`. It was not a test, an exam or a strategy registration.

## Chronology and evidence

- **Contract, code and registry row.** Reviewed by the colleague and merged as
  #596 (`af25f70`) on 2026-11-14.
  - The review requested one change. Its fix `95cd3b4` was not re-reviewed; the
    owner chose to merge on green.
  - The registry row and the request list (`1cc29544...`) were in main before the
    first request.
- **Fetch.**
  - Ran on the owner's go from a clean detached worktree at `af25f70`.
  - All **1,723 of 1,723** requests came back complete on the first attempt:
    1,723 attempts of 3,446, and 30.6 MiB of reserved response bytes of 128 MiB.
  - Run binding `8bb4a9d8...`, sealed receipts `f038532e...`. Both are outside Git,
    under `runtime/research/hyp036-econ-v1/`.
  - The 26 instruments that are `Closed` today also returned their 1-minute bars.
- **Read.**
  - Ran on the owner's go at 2026-11-14 22:49 UTC from the same revision.
  - It rebuilt the sealed artifact from its receipts before computing anything.
  - Result SHA-256 `6acc6544fbb6c3e8b082d5cfe323237258be538c93a65e4c5cb7223d9f684baf`.
  - Kept as a deterministic [gzip](evidence/hyp036-economic-pilot-result-2026-11-14/result.json.gz)
    with a [manifest](evidence/hyp036-economic-pilot-result-2026-11-14/manifest.json)
    and the [fetch summary](evidence/hyp036-economic-pilot-result-2026-11-14/fetch-summary.json).
  - No corrective recomputation was needed.

## Decision inputs

| Item                                                          |                            Value |
| ------------------------------------------------------------- | -------------------------------: |
| Complete events for net                                       |               777 of 784 (99.1%) |
| Complete clusters                                             |               743 of 750 (99.1%) |
| Incomplete reason                                             | zero-volume entry or exit bar: 7 |
| **Cluster mean net at 30 bps, funding included**              |                   **-42.80 bps** |
| Without the top 5 symbols (DOG, UXLINK, VET, BLUR, 1000TURBO) |                       -51.19 bps |

Both conditions for `continue_to_discovery_protocol` fail. Completeness is above
95%, so the result is not `not_assessable`.

## Fixed report

| Item                                        |        Combined |         January |            July |
| ------------------------------------------- | --------------: | --------------: | --------------: |
| Clusters                                    |             743 |             393 |             350 |
| Price return of the short before costs, bps |          -13.14 |           -1.39 |          -26.33 |
| Funding received by the short, bps          |           +0.33 |           +0.34 |           +0.33 |
| Before costs (break-even round trip), bps   |          -12.80 |           -1.05 |          -26.00 |
| Net at 30 bps, bps                          |          -42.80 |          -31.05 |          -56.00 |
| Net at 30 bps without funding, bps          |          -43.14 |          -31.39 |          -56.33 |
| Net at 50 bps, bps                          |          -62.80 |          -51.05 |          -76.00 |
| Net at 30 bps, mark low/high valuation, bps | -42.81 / -42.80 | -31.05 / -31.05 | -56.00 / -56.00 |

Further items from the fixed report:

- **Concentration:** 193 symbols over four ISO weeks. The top 5 symbols hold 6.9% of
  complete clusters.
- **Funding:** its share of net is -0.8%, so funding is immaterial.
- **BTC-adjusted net (diagnostic, not a tradable hedge):** -33.11 bps over 743
  clusters. Every event had its BTCUSDT bars.
- **Uncertainty:**
  - the cluster standard error is 10.09 bps, which understates same-hour and
    same-week dependence;
  - the audit's planning standard error is 27.60 bps.
- **USD 50 one-slot portfolio (diagnostic):**
  - 140 trades, 644 signals skipped while the slot was busy;
  - final cash USD 29.83, realized drawdown USD 27.33;
  - longest losing streak 7.

## Reading

- **The negative result is not marginal.** Net is negative in both blocks, without
  the top five symbols, after the BTC adjustment and at either cost level. The price
  part is negative before any cost: after heavy taker buying with a contained
  price, the price did not on average fall over the next four hours.
- **The mirror concern no longer applies.** Abnormal-flow v1's long on a similar
  pattern also lost, on 2026-07 to 2026-09. So neither side of this flow pattern
  has shown an edge on its own window.
- **What this does not show.**
  - It does not prove that no absorbed-flow strategy can work.
  - Four weeks cannot rule out an effect in the whole family, and other horizons or
    executions were not tested.
  - By the contract, no variant, horizon, filter, mirror, subgroup or cost level is
    read on these events. HYP-036 v1's declared variants V1 to V3 stay unread.

**Disclosure.** While printing the fixed report, the reviewer also displayed two
numbers outside it:

- the event-level mean net at 30 bps, -41.47 bps;
- the share of complete events with positive net, 43%.

They decide nothing and are listed here only so the exposure is complete.

## What stays

- **Windows.** The two pilot blocks' hold windows (2025-01-13T00:01Z to
  2025-01-27T03:01Z and 2025-07-07T00:01Z to 2025-07-21T03:01Z, the listed holds
  only) are now read at outcome level for this trigger. The backward holdout and
  the HYP-031 test year stay unread and unassigned.
- **Data**, all kept outside Git:
  - the sealed receipts and the run ledger;
  - HYP-036's pilot tape parts (80 GB).
- **Selection history** for any later absorbed-flow design:
  - HYP-036 v1, stopped twice on controls;
  - abnormal-flow v1's long loss;
  - this negative short;
  - the 2026-11-11 exploratory series.

## Program consequence

No candidate is active. What remains scheduled:

- the HYP-012 v2 checkpoint on 2026-11-30 (the registered command on the owner's go);
- the HYP-038 radar intake after v2 is terminal.

Under the [program plan](program-plan-2026-11.md) stop rule, without a candidate
past an untouched window by 2027-02-11, the program goes to maintenance until the
March read. Whether to start another idea before then is the owner's decision.
