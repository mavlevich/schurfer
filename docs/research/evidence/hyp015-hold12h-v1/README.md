# HYP-015 formal result

The single registered read returned `reject_hold12h` at Gate B on
2026-11-05 08:41:09 UTC. For the decision window `[2026-10-05, 2026-11-02)`
UTC, all 3,575 pairs were analyzable. The 720-minute policy averaged
−0.643699% net after costs, including captured funding. Gate B rejects a
mature non-positive standalone mean.

The registered $300 portfolio replay lost $131.53 at 720 minutes and $228.99
at 240 minutes. The longer policy took 354 positions versus 924 for the
shorter policy and occupied 95.6% of available slot-hours versus 88.7%. These
are portfolio diagnostics for the same WATCH stream. The registered
confirmation stage required `candidate`, so this contract closes here.

The production claim was `completed` with complete coverage and no override.
The published [verdict](hold12h_verdict.json) matches the claim and the
[SHA-256 sidecar](hold12h_verdict.sha256). The pinned reader inputs stay in the
production evidence archive. [Registration](../../momentum-flow-hold12h-verdict-v1.md).

| Evidence                  | Identifier                                                         |
| ------------------------- | ------------------------------------------------------------------ |
| Read code revision        | `d87b3a23c9aa1b4d7e00879f67723b15025a54b2`                         |
| Registered contract hash  | `b7878c673867fa7bcb32a7a8ab53ef9a801a9daa598cb8b4fcdd84d090bdf406` |
| Pinned read inputs digest | `b77984273efd553e232b40d33f4ecfdd5edb29036f267764dcc89a181b74ecd3` |
| Result fingerprint        | `adcb3243b6531376320cd06c25bef2035532948f7c3b56e579f54ac92a508e5f` |
| Restored input set        | `hyp015-20261105T061202Z-edc60cff75c24ebd9324a9848333e982`         |
