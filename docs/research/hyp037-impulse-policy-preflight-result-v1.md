# HYP-037 preflight result v1

Completed 2026-11-11 from the registered
[preflight contract](hyp037-impulse-policy-preflight-v1.md). Only the existing
historical features snapshot was read. No minute outcome bars, setup means, net
returns, test-year data, backward holdout or production data were read.

## Counts and planning power

| Measure                                             |    Plain impulse A | Funding/OI-filtered B |
| --------------------------------------------------- | -----------------: | --------------------: |
| Events and symbol-day clusters                      |             12,278 |                   986 |
| Symbols                                             |                629 |                   361 |
| ISO weeks                                           |                 51 |                    51 |
| Top-five symbol share                               |              2.18% |                 5.17% |
| First-half clusters                                 |              6,309 |                   383 |
| Peak simultaneous unrestricted 4-hour holds         |                334 |                    17 |
| Planning MDE with the two-candidate correction, bps | 12.791905864541443 |     49.22851894642323 |
| Registered 50 bps planning limit                    |             within |                within |

The loader produced 671 instrument series, with no failed instruments. F and G
were determinable at 12,277 of 12,278 baseline events. Both candidates pass the
preflight count, symbol, week and top-symbol checks; B also passes determinability.

**Identity reproduction.** Verified the original HYP-033 `counts.json` against its
registered SHA-256 `343512373d5bb31696876995c1dfef37dd12338ae0c10b54d5df5c040a10e31f`,
then verified its `pairs.json` using that report's binding. All 986 B event IDs,
including native symbols and decision timestamps, equal the old frozen event set.

The MDE values retain the registered planning inflation 1.5 and assumed design
effect 1.2. B is close to the 50 bps limit. These are planning estimates under
those assumptions, not observed power after the setup or uncertainty about a
measured profit. A's greater event count can still contain synchronized market
moves. The simultaneous-hold counts describe capacity pressure, not an instruction
to use 334 or 17 live slots.

## Provenance and sealed evidence

This was a **local dirty-tree run**, based on main
`25207f4fc210c24a34a9f07fc8fa9f8a8c09f279`. Its new code was uncommitted. The
report records the actual dirty state and content fingerprints of all seven
declared code dependencies. In particular, `bybit_squeeze_policy.py` was
`f38d84a570df5456ac8c9e6249f66c908e3bbf0c4a98a847f7cefe1aaa3d5bdf`.
It is a reviewable local preflight and does not claim clean merged-code provenance.

The invocation binding was created at 2026-11-11T21:29:59.716972Z. The features
snapshot was checked before parsing and before result publication. The original
local output is `runtime/research/hyp037/preflight-local-v1/` in the study worktree.
The following copies preserve the emitted bytes for review.

| Artifact                                                              | SHA-256                                                            |
| --------------------------------------------------------------------- | ------------------------------------------------------------------ |
| [Registration](hyp037-impulse-policy-preflight-v1.json)               | `4ca9ed7117951b140aa64ef318cd47d9e24c2c3defc06093d5a840beba0511dc` |
| Registered protocol document                                          | `22e04e98d37576441bf158dfbee531f56d2e7d15b574ae94ae33b85cd3fefb1a` |
| Features snapshot                                                     | `a3b54d6ce092a73afbd4911e8ea72862a707846b45fbb23d05ec1d91f71ebab6` |
| [Run binding](evidence/hyp037-preflight-v1/run.json)                  | `8b324bd312f991bcb35d85cef2c4765496ee7885d74aa1939b96f7cc0d715f48` |
| [Counts](evidence/hyp037-preflight-v1/counts.json)                    | `a9ef438e49c8bbf64a2c1c634b0f213f129ae3fc439bd9f3d591e562e5e16169` |
| [Frozen IDs, compressed](evidence/hyp037-preflight-v1/events.json.gz) | `0e9ba16f5131a9afb332481c7bcc29f6361d165972b505d5ffea91515207850b` |

The compressed event file uses gzip mtime 0. Its decompressed bytes match the
original `events.json`, whose SHA-256 is recorded in `counts.json`. The run hashes,
event uniqueness and A/B counts were independently checked against these files.
An initial CLI attempt used the registration hash from before formatting and was
refused before feature loading. The one completed invocation used the registered
hash above.

## Verification and next step

The code slice passed 91 focused tests across the policy, original scanner,
cluster inference and portfolio modules. Ruff and focused mypy passed. The tests
include loading a generated snapshot through the real CLI, hash/contract
rejection, source changes, output exclusivity, missing outcomes and paired-week
inference. Full `make verify` remains a pre-PR delivery check.

The next step is review and freeze of the empirical-read contract and minute-data
scope for HYP-037. Both A and B remain candidates for that read. Their net signs
are unknown. A discovery survivor still needs the registered capital-constrained
replay and independent confirmation before forward or live stages.
