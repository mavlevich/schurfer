# HYP-031 discovery inputs

The discovery decision window for `bybit_post_funding_minute_v1_a3` is
`[2024-10-01, 2025-10-01)` UTC. Both snapshots were fetched from Bybit's public
API on 2026-11-05 from the clean `origin/main` revision
`d87b3a23c9aa1b4d7e00879f67723b15025a54b2`. The files and command logs live in
`runtime/research/hyp031-discovery/` in the collection checkout.

| File                       | Started (UTC) | Finished (UTC) | Bytes       | SHA-256                                                            |
| -------------------------- | ------------- | -------------- | ----------- | ------------------------------------------------------------------ |
| `index-d87b3a2.jsonl.gz`   | 09:07:50      | 09:27:16       | 112,108,847 | `f48fe09f7023a476e524cf70c6ec1b629eee30a9a07cff41c9d149dfb59e0cf5` |
| `windows-d87b3a2.jsonl.gz` | 09:27:36      | 10:36:30       | 104,225,243 | `b20a5428e031b1764deea40dc605cc0be3659591296cb05ef6d4b39f267c0cd4` |

These are the complete fetch summaries, one attempt per snapshot:

```jsonl
{"stage": "discovery", "symbols": 672, "errors": {}, "responses": 9266, "recovered_requests": {"kline": 14}, "sha256": "f48fe09f7023a476e524cf70c6ec1b629eee30a9a07cff41c9d149dfb59e0cf5"}
{"stage": "discovery", "events": 13977, "windows": 39737, "errors": 0, "responses": 39738, "recovered_requests": {"window": 177}, "sha256": "b20a5428e031b1764deea40dc605cc0be3659591296cb05ef6d4b39f267c0cd4"}
```

The window fetch consumed the index with its recorded SHA-256. Both files pass
`gzip -t`, and independently computed SHA-256 values match the fetch summaries.
The event count describes the fetch plan, including diagnostic windows. The
[registered runbook](../../../bybit-post-funding-minute-v1.md#runbook-2026-10-26-sizes-from-a-pre-discovery-smoke-no-stage-data-read)
specifies a separate owner go for the discovery report.

The owner approved the single discovery read on 2026-11-05. The first launch
stopped at `uv` cache initialization, before the reader started. The subsequent
offline launch ran on the recorded clean revision and published `fail`. The
[original result](result.json) matches its [SHA-256 sidecar](result.json.sha256).
The [runbook result](../../../bybit-post-funding-minute-v1.md#discovery-result-2026-11-05)
records the failed gates and the separate seconds-level research option.
