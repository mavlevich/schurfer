# B3a discovery evidence, 2026-11-14

This is the original two-exit read from clean main `45fc1a1`, not a tuned rerun.
The owner separately authorized fetching and the formal report. Both current
taker policies return `fail`; the budget result is `maker_source_review_needed`.

- `result.json.gz`: deterministic gzip, mtime 0, of the exact original report
  JSON. Decompressed SHA is `4d4f6f1adc18bda779c48aa78bf7ea997f34f47b79bf602e5f32e3cd87444b33`.
- `run.json.gz`: exact immutable request/run binding, compressed with mtime 0.
- `fetch-summary.json`, `source-completion.json`: source-only completion facts.
- `main-publication.json`: #593 merge/CI witness before native requests.
- `fetch-authorization.json`, `report-authorization.json`: separate owner go
  records retained before their respective actions.
- `invocation.json`: actual reader arguments, revision, original result hash,
  stdout and durable source location; recorded after completion.
- `archive-location.json`: native snapshot location and the hash-verified funding
  index retained outside worktrees as a hard link of the same immutable input.
- `verification.json`: independent arithmetic on the existing registered output,
  with no additional variants or market-data queries.
- `manifest.json`: byte counts and SHA-256 for the binary/JSON evidence, excluding
  the inventory itself and this explanatory README.

The 15.3 MB sealed native snapshot and all 4,206 attempt receipts remain at
project-root `runtime/research/b3a-v1/`, outside disposable worktrees. They are
not duplicated into git. Their sealed SHA is
`d952ecf2b22d3ede3b393347dda51f12a518b3601b5537bb9842a383be29740d`.
The exact original funding index is also retained at
`runtime/research/b3a-v1/inputs/index-d87b3a2.jsonl.gz`, SHA `f48fe09f...`.
This is retention of the same bytes, not another fetch or independent sample.
The frozen request list is already published under
`docs/research/evidence/b3-economic-screen-proposal-2026-11-13/requests.json.gz`.

For reproduction use clean `45fc1a1` and the original
[runbook](../../b3-economic-screen-runbook-v1.md), preserving the native run
directory and all budget/receipt files. Do not overwrite the original result;
an authorized byte-equivalence rerun would need a different output path, the
same sealed/index hashes and identical code/parameters. No rerun is performed
as part of this evidence publication.

Native price paths are exact Bybit klines; execution prices are bar-open
proxies with registered costs. Mark-bar funding valuation is approximate.
The portfolio's unknown tail remains unknown. This evidence does not open
the test/holdout/blind windows, a new maker download or live trading.
