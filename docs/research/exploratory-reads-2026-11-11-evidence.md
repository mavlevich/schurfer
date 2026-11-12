# Exploratory reads of 2026-11-11: where the figures come from

Status: evidence index, 2026-11-12. It reads no new data.

Two series of exploratory reads ran on the discovery year (2024-10-01 to 2025-09-30) in
one session of another agent, outside this repository. The seen-windows registry and the
discovery ledger record them. This note says which local files hold the figures, with
their SHA-256 at 2026-11-12, so the figures can be traced without rerunning anything.

The files are on the owner's machine under
`~/.gemini/antigravity/brain/f3296c9b-e3f0-4fe0-88eb-444956539447/`. They are not copied into the repository; a digest does not replace the file, so they need a durable archive. The session transcript is still open, so its digest below is a
snapshot and may change.

## Series 1: HYP-032, HYP-033, HYP-034, HYP-031 before settlement, Bybit vs Hyperliquid

**Reproducibility limit.** The scripts of this series were deleted. What survives is the
command output in the session's task logs and step outputs. The figures in the ledger
are reconstructed from that output and from the agent's report. Rerunning them needs new
scripts.

| File (under `.system_generated/`) | SHA-256                                                            | Figures found in it                                                                                                                       |
| --------------------------------- | ------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------- |
| `tasks/task-406.log`              | `7a0c7f8e36b75937d158b4c7402d2d407b0dc502044c304533cbf842d501c8ac` | Bybit vs Hyperliquid funding difference +44.6, next 4 h +11.8, 8 h +25.4                                                                  |
| `tasks/task-535.log`              | `7f1407779be09494e271edc405fe8ae15dcb517366747133c7cd60e92b043e45` | HYP-031 before settlement, false triggers                                                                                                 |
| `tasks/task-550.log`              | `7d8452fdb9248e1823009a9914bbe69c70ea576a3ea5b956af3f64e9b0f12b94` | HYP-033 cells, HYP-034                                                                                                                    |
| `tasks/task-563.log`              | `c76bfad330f038ed0882fc890eeca9eb4c15c5959794bb1ea2123017d5203eb7` | HYP-031 4 h +55.3 gross, Net30 +25.3, 8 h -4.7; HYP-032 -23.1                                                                             |
| `tasks/task-585.log`              | `e23f048e9075647c857595f83ff722503bc202f4cd1645dcfa4d5161cbd91f2d` | HYP-033 f_only, HYP-032                                                                                                                   |
| `tasks/task-597.log`              | `9c7de18419fae7ee4715d93c04a57b746654e4f132b2db77928896ced17f4d3f` | HYP-033 f_only one-slot portfolio -61.1                                                                                                   |
| `tasks/task-601.log`              | `037b653876ce640718c7e4191ec91b727d985d2ad7cbae61d53d0ddcfe90134e` | HYP-033 market-wide hours +236.7                                                                                                          |
| `tasks/task-626.log`              | `85c735a137e09773532c0332399dedac0f3014268a794a0cae80f020de95d6f3` | Bybit vs Hyperliquid regime A, funding +22.7                                                                                              |
| `tasks/task-686.log`              | `7de688e861d1d32479eeff5620eac56828f6c5c55556afe207c7d5bcc68860da` | Bybit vs Hyperliquid regime A gross +51.2                                                                                                 |
| `tasks/task-692.log`              | `7840e51a8f938bb817b873a13770fe5b10e3c1d128cb33e6baecbf623a9fb760` | HYP-033 portfolios, HYP-034                                                                                                               |
| `tasks/task-798.log`              | `b005b5e3c6e9186f7c1c964a90de59e66ad88e4273719644e910ce9f9bbc8946` | HYP-031 before settlement, repeat run                                                                                                     |
| `steps/418/output.txt`            | `0067dc83790c3edca866204a4ae9c5f4694fa58574ae7d22a2d9e8cbdda82385` | Bybit vs Hyperliquid regime B +74.9 (median +32.1)                                                                                        |
| `steps/424/output.txt`            | `2b0df5b1b0171c9377b4c12ab1d99573249325975bea25d0dd38c3a8e1948c54` | Bybit vs Hyperliquid regimes, repeat                                                                                                      |
| `steps/434/output.txt`            | `dc4a252fd53608f16a014d7a6a1458a5eba68439a8bc52f052c30077cefc1d1b` | Bybit vs Hyperliquid regimes; Bybit short return in [t, t + 1 m] +54.0 bps mean, median +44.9, 95 of 97 regime A events (lines 43 and 44) |
| `steps/763/output.txt`            | `e756c511054deb24dd81f0184910dfba7618f3e55965c996a679aff4ebac60a1` | Bybit vs Hyperliquid summary                                                                                                              |
| `logs/transcript.jsonl`           | `05db0492e76a844fe6ae26ff2ce9ed3324721d63b7443ead9c882fc29b4cc5f1` | whole session (snapshot, still open)                                                                                                      |

**First-minute move.** A first search missed the Bybit first-minute figure because the output prints it as a short's return (+54.0), not as a price change (-54.0); a colleague located it in `steps/434/output.txt`. Only the Bybit leg is measured: Hyperliquid's price for that minute was not fetched, so first-minute basis convergence stays an unverified assumption.

**Untouched windows.** The agent states that the test year, the holdout and the v2
blind window were not read. These files do not prove a negative. The statement is
recorded as the agent's.

## Series 2: pump long, post-pump short and maker entry (v1 to v3), 54 configurations

Run 2026-11-12 between 00:19 and 00:58 on the owner's local clock, right after series 1.

The scripts and results were kept, and every digest below was recomputed on 2026-11-12
and matches the agent's report. The scripts import no network library and contain no
URL. They cap every bar at `1759276799999` ms (2025-09-30 23:59:59.999 UTC). Decisions
run from 2024-10-16 to 2025-09-30 16:00 (8 h) or 20:00 (4 h).

| File                                     | SHA-256                                                            |
| ---------------------------------------- | ------------------------------------------------------------------ |
| `scratch/pump_short_lab_v1.py`           | `2e02b43ecd9c1bc0efb58ef83d1645fb4462bfb0728a9e3f014c1856878aee7f` |
| `scratch/pump_short_lab_v1_results.json` | `291dddfe40a00a5b222436ff05dd5bb092476a61b20b16651dc68f2734058555` |
| `scratch/pump_short_lab_v2.py`           | `b68001e09af452ee787f217c58d550f0543b5ce9f5defca16401b7985458dde2` |
| `scratch/pump_short_lab_v2_results.json` | `6a838c8b91a09cd25a719905fee855818e0fc4f5225d8f595593afeaa72b5adf` |
| `scratch/pump_short_lab_v3.py`           | `f4af7739215d6dbbaac227b839990743389c5f4d18401731b183147ebd95b43c` |
| `scratch/pump_short_lab_v3_results.json` | `6e1c14665f7421317749a016b95fa035fbd0c516ec17d275a5920b6345ceca76` |

Inputs, read-only local snapshots, digests recorded in the results and rechecked:

| Snapshot (repository worktrees)                                               | SHA-256                                                            |
| ----------------------------------------------------------------------------- | ------------------------------------------------------------------ |
| `hyp032-run/runtime/research/hyp032/features-8522f0c.jsonl.gz`                | `a3b54d6ce092a73afbd4911e8ea72862a707846b45fbb23d05ec1d91f71ebab6` |
| `premium-protocol/runtime/research/hyp034/premium-a6a1408.jsonl.gz`           | `ec05a2c39f6e9eaa5fba52396ff286b331253ad42a30678e24204c720c28e43a` |
| `hyp031-discovery/runtime/research/hyp031-discovery/windows-d87b3a2.jsonl.gz` | `b20a5428e031b1764deea40dc605cc0be3659591296cb05ef6d4b39f267c0cd4` |

The v3 script's docstring names a different digest for the minute windows
(`e3cbffd2...`). The script hashes the file at run time, and its results record
`b20a5428...`, which matches the file. The docstring is stale.
