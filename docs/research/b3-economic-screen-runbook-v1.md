# B3a runbook: native minutes and the two fixed exits

This runbook activates no orders, production changes, exam or native trade tape.
Use the [contract](b3-economic-screen-proposal-v1.md) and its frozen request list.
The owner's design go is recorded; one implementation review and publication
of code, contract, scope and registry in main precede any native request.

## Before the first request

1. Complete the focused code review and required checks. Merge on green CI.
2. Fetch main and create a clean research checkout of that merged revision.
   Do not rebase or modify code after preparing the local run binding.
3. Verify the frozen event/scope hashes and pinned discovery funding index.
   All paths below are local CLI arguments; no current instrument/quote lookup.
4. Choose a durable output directory outside disposable worktrees. Keep failed
   attempts and budgets; never start a new folder merely to replenish a cap.

Commands below run from the clean repository root. Set `--root` to the same
absolute durable directory in every command. The checked-in request list is
already complete; `scope` is an offline reproducibility tool, not a necessary
network stage, and refuses to overwrite an existing scope.

```sh
uv run python -m schurfer_analytics.bybit_presettlement_economics prepare \
  --root /Users/mavlevich/Desktop/Projects/schurfer/runtime/research/b3a-v1

uv run python -m schurfer_analytics.bybit_presettlement_economics fetch \
  --root /Users/mavlevich/Desktop/Projects/schurfer/runtime/research/b3a-v1

uv run python -m schurfer_analytics.bybit_presettlement_economics seal \
  --root /Users/mavlevich/Desktop/Projects/schurfer/runtime/research/b3a-v1
```

`fetch` summarizes source counts only; the source payloads themselves expose
prices, so registration is required before fetching, not just before reporting.
Record the SHA printed by `seal` and use it as the mandatory `--sealed-sha256`
argument below. Do not substitute a reconstructed/rounded artifact.

```sh
uv run python -m schurfer_analytics.bybit_presettlement_economic_report \
  --root /Users/mavlevich/Desktop/Projects/schurfer/runtime/research/b3a-v1 \
  --sealed-sha256 SEALED_SHA_FROM_PREVIOUS_STEP \
  --index /Users/mavlevich/Desktop/Projects/schurfer/.claude/worktrees/hyp031-discovery/runtime/research/hyp031-discovery/index-d87b3a2.jsonl.gz \
  --output /Users/mavlevich/Desktop/Projects/schurfer/runtime/research/b3a-v1/result.json
```

Reporting verifies receipts and sealed bodies again, then produces both exits
together. `publish_once` refuses to overwrite the report. Retain invocation,
clean revision, exact publication/contract hashes, source receipts, budgets,
sealed SHA and the result. Publish evidence without modifying original files.

## Failure and continuation

- A corrupt/invalid response or bound identity conflict stops sealing. Preserve
  the failure; do not discard it as an unavailable symbol.
- Fetch errors and gaps after two attempts remain in the original event and
  cluster denominators. Coverage failure is not a negative return or zero PnL.
- If a request is interrupted, rerun `fetch` against the same directory and
  clean revision. Attempts and conservative byte reservations remain spent.
- Budget exhaustion stops the stage; no automatic increase or fresh run folder.
- `track_b_paused_budget` pauses larger maker expenditure and keeps the exam
  closed. `maker_source_review_needed` authorizes no new download by itself.
- A taker `nominated_for_protocol` needs the separate B4 exam contract and
  fixed confirmation batch. Discovery never permits live orders.
