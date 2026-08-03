# Discovery ledger

Status: protocol plus a live, append-only log. An entry here never approves a
strategy, changes production, or authorizes a cohort. See
[ROADMAP.md](../../ROADMAP.md), Research portfolio and capital discipline, for the
four-level model this ledger belongs to (Observation, Discovery, Confirmation,
Promotion).

## Purpose

Discovery screens are cheap and wide by design: dozens of parameter variants against
already-collected or historical data in one pass. That speed is real, but it is not
free. Two costs a per-PR count does not capture:

- Running many combinations against one historical window turns that window into a
  training set for every hypothesis it touched. A good result from that same window
  afterward is not new evidence — it needs its own untouched forward cutoff before it
  can move to Confirmation, exactly like a cross-family result already does.
- A batch of many cheap screens produces false positives at a predictable rate even
  when nothing in the batch has real edge. Logging only the positive result from a
  batch of 40 would misrepresent 1-in-40 as if it were the only variant tried.

This ledger exists so a negative or parked screen is exactly as visible as a
candidate, and so no batch can quietly present its best row as the whole story.

## Required fields per entry

- `hypothesis_id` — stable id (`HYP-011`, or a family-scoped slug).
- `family` — pump-reversion, order-flow, source-lead, listing-delisting,
  open-ended-margin, or other.
- `data_window` — the exact historical or already-collected range and its cutoff.
  Any later Confirmation-level cohort for the same hypothesis or asset population
  must use a window that does not overlap this one.
- `parameters_tested` — the variant or range screened in this pass.
- `primary_metric` — the one pre-declared metric the screen ranks on, chosen before
  seeing results, not after.
- `variant_count` — how many parameter combinations this pass actually screened.
  Needed to size any within-batch FDR/Holm correction.
- `result` — one-line factual summary. No verdict language ("edge confirmed") at
  this level — discovery generates a hypothesis, it does not test one.
- `status` — `rejected`, `parked`, or `candidate`.
- `confirmation_requirement` — the untouched forward cutoff and sample size a
  `candidate` would need before it can enter Confirmation.

## Log

Empty as of 2026-08-03. This ledger does not backfill discovery work performed
before this date — the same left-censoring rule already used for every other forward
cohort in this project. Append new rows as screens run. Never delete or edit a
`rejected`/`parked` row to make room for a later positive result on the same
`hypothesis_id`; register a new id instead and note the relationship in `result`.

| hypothesis_id | family | data_window | parameters_tested | primary_metric | variant_count | result | status | confirmation_requirement |
| ------------- | ------ | ----------- | ----------------- | -------------- | ------------- | ------ | ------ | ------------------------ |
