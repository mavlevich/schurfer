# HYP-037 dependence calibration result v1

Result: **both A and B stop at `insufficient_power` under the registered 50 bps
planning gate. Their trade returns remain unread.** This is a power result under
the registered proxy assumptions. The sign or size of their trading edge is unknown.

The [amendment](hyp037-power-calibration-amendment-v1.md) fixed eight proxies, the
maximum-design-effect rule and each candidate's failure consequence before this
read. All proxies were estimable: 12,278/12,278 A and 986/986 B events defined in
every proxy, every original event week represented, 10,000 valid draws each.

## Dependence and planning result

| Proxy                                                   | A design effect | B design effect |
| ------------------------------------------------------- | --------------: | --------------: |
| Four hours ending immediately before the impulse starts |          111.00 |            2.66 |
| Ending 24 hours earlier                                 |           19.44 |            1.83 |
| Ending 48 hours earlier                                 |           21.67 |            0.70 |
| Ending 72 hours earlier                                 |           17.72 |            1.46 |
| Ending 96 hours earlier                                 |           25.28 |            2.24 |
| Ending 120 hours earlier                                |           14.95 |            1.65 |
| Ending 144 hours earlier                                |           13.47 |            1.36 |
| One-hour waiting bar, before entry                      |            7.45 |            0.77 |

The maximum is the immediately pre-impulse four-hour proxy for both candidates.
The waiting-bar ratio participates in the maximum, with no substitution of its
one-hour variance for the four-hour planning scale. The report includes every
proxy ratio and its coverage. It does not retain the two absolute variance
components separately; they can be reproduced from the committed calculation
and pinned features. This is a reporting limitation, with no change to the ratio
or the registered decision.

| Measure                                        |                  A |                  B |
| ---------------------------------------------- | -----------------: | -----------------: |
| Old MDE with assumed design effect 1.2, bps    |              12.79 |              49.23 |
| Calibrated design effect                       |             111.00 |               2.66 |
| Calibrated MDE, bps                            |             123.03 |              73.23 |
| Registered MDE limit, bps                      |                 50 |                 50 |
| Normal-approximation power at true +30 bps net |              10.1% |              20.8% |
| Normal-approximation power at true +50 bps net |              20.6% |              48.1% |
| Verdict                                        | insufficient_power | insufficient_power |

The table's power figures also equal the joint net-condition approximation in
this run, since the bound-above-zero condition is the tighter one for both.
The earlier 12.79/49.23 comparison was conditional on a design effect that the
calibration does not support. Historical pre-entry dependence can still differ
from dependence after entry, and the 51-week estimate is noisy. These limits are
carried with both machine verdicts.

## Sensitivity across the registered proxies

For each proxy separately, retain the original design-effect floor 1.2 and
four-hour planning scale. The following MDE values are derived from the sealed
report, with no new market-data read.

| Proxy       | A MDE, bps | B MDE, bps |
| ----------- | ---------: | ---------: |
| pre_0h      |      123.0 |       73.2 |
| pre_24h     |       51.5 |       60.8 |
| pre_48h     |       54.4 |       49.2 |
| pre_72h     |       49.1 |       54.2 |
| pre_96h     |       58.7 |       67.2 |
| pre_120h    |       45.2 |       57.7 |
| pre_144h    |       42.8 |       52.3 |
| waiting_bar |       31.9 |       49.2 |

B exceeds 50 bps under six of the eight proxies. A meets 50 bps under four of
eight, and stops under the registered conservative maximum rule, whose largest
effect comes from pre_0h. This does not establish that A is underpowered under
every reasonable proxy. The maximum rule was committed before the calibration
and remains the rule for this result.

A common market factor is a plausible explanation for A's strong dependence;
this calibration does not identify its cause. A tradable market hedge would
change the strategy, costs and variance and requires its own registration and
selection history. No hedged A outcome or net was evaluated here.

## Provenance

Run binding created 2026-11-11T22:21:19.989253Z, from clean local commit
`538abdc00f9de06cc9973d55141a37a163cfddd1`. That commit contains the protocol,
machine registration and tested code and precedes this calibration invocation.
Git porcelain was empty before and after the calculation. Eight code dependencies
were fingerprinted, including the calibration module. The features snapshot,
original counts and event identities were hash-verified before and after the read.

| Artifact                                                              | SHA-256                                                            |
| --------------------------------------------------------------------- | ------------------------------------------------------------------ |
| [Machine registration](hyp037-power-calibration-v1.json)              | `f91ef6f3f98b54eaeb5aeeec37cae924c2707b9352b1ad23b6c6605006ddcc0a` |
| Amendment document                                                    | `442e55814bb941b9314464570b7c6d0b1ba90ae6f9ad12bcadf3f97f4f671c04` |
| Features                                                              | `a3b54d6ce092a73afbd4911e8ea72862a707846b45fbb23d05ec1d91f71ebab6` |
| Frozen original counts                                                | `a9ef438e49c8bbf64a2c1c634b0f213f129ae3fc439bd9f3d591e562e5e16169` |
| [Run binding](evidence/hyp037-calibration-v1/run.json)                | `5d4b2c4c210c3c54bf491b9b18f1c46b8202701d1447b0568b988edab608b188` |
| [Calibration report](evidence/hyp037-calibration-v1/calibration.json) | `65197f39aef5bb2164be67bfc9360bfdce81cc552b66639937efca0960900775` |

The first snapshot, registration and evidence in `b1ef6c7` and the evaluator fix
in `91abd1d` remain preserved. This additional read used hourly return proxies
before each entry, which can overlap earlier events' paths as registered. It
emitted dispersion/dependence and planning statistics only. No A/B setup means,
net returns, minute entry/exit data, holdout, HYP-031 reserved year or HYP-012 v2
blind-window data were read.

## Decision and verification

Apply the preregistered action separately to both candidates: stop without outcome
reading. The threshold, lag list and maximum rule remain fixed after this result.
Any new candidate, hourly execution rule or revised economic target requires its
own declared decision and registration, carrying these method attempts into the
selection and confirmation record.

The code passed 109 focused tests, Ruff, focused mypy and the normal pre-commit
checks before registration commit. Tests cover waiting-bar dependence, exact
50/50.1 bps boundaries, one-candidate failure, missing weeks, degenerate proxies,
future-price exclusion, joint economic power and the real registered snapshot
reader.

Full `make verify` passed on 2026-11-11 after merging `origin/main` at `263ad23`
into branch revision `235f36e`, with the documentation-only proxy-sensitivity
addition in the working tree. The command used the normal disposable PostgreSQL
database and completed all-files lint, lock validation, Python type checks and
tests, Go tests/vet/deadcode, web lint/typecheck/tests/build and Compose validation.
This verification did not rerun the calibration or read setup outcomes.
