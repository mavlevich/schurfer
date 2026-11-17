# Catalyst feasibility v1: A2 base-extraction diagnostic (2026-11-16)

Status: **diagnostic only. No fix, no amendment, no recount.** It reads no market value,
fetches nothing and contacts no exchange. Sections 1 to 5 open no holdout title,
ticker, date or monthly count; they read discovery-year titles only, which the
[contract](catalyst-feasibility-v1.md) (section 7, "Source gating") allows for a parser
fix. Section 6 records one owner-approved structural look at the holdout titles that
printed shape-class counts and nothing else.

Context: the [count result](catalyst-feasibility-v1-result-2026-11-16.md) left bucket A2
(Binance USDⓈ-M futures listing of a base with a live Bybit USDT perp) at `lab_only`:
its sealed holdout has 2 events from 136 articles, against 106 articles and 71 events
in the discovery year. 134 of 136 holdout articles gave no base from the title, against
6 of 106 in the discovery year. The question here is whether A2 is a correctly testable
candidate, not whether its number can be improved.

## 1. Outcome

**A limit of the code is confirmed on synthetic titles. The one holdout structural look
found that none of the 134 no-base holdout titles has any of the seven fixed shapes
(parenthesised ticker, slash pair, spaced pair, USDC pair, `Multiple`, index, or a
`...USDT` pair): all 134 are `other`.** So the parenthesis hypothesis is not
supported, neither is the `Multiple` or index hypothesis, and the real shape is still
unknown. No defect in the sense of the contract's correction clause is established.

**Status: A2 stays `lab_only`. The cause of the lost bases is not established.**
`other = 134` means only that the shape classes fixed in advance did not recognise
the title format. It is not evidence that the events do not exist, that no edge
exists, or that the titles are unreadable. This note proposes no fix, writes no
amendment, and does not close A2. No second class selection on the holdout titles is
allowed now.

## 2. What the A2 branch does

`classify_parsed`, `catalog == "48"`: a title containing `Futures Will Launch` and
`Perpetual` is bucket A2, and its bases are `_pairs(title)`, that is every `...USDT`
token whose prefix is not in the stop list, with the contract-size multiplier removed.
It never calls `_parens` (the A1 and A3 branches do) and never reads the free-text
parser. An article with no `...USDT` token stays an A2 article with no base: it is
counted as an article and as `no_base` in the report status, and makes no event.

## 3. Synthetic test cases (step 1)

Pinned in `test_a2_base_rule_reads_only_usdt_pairs` (all tickers are invented). Every
case is bucketed A2; only the bases differ.

| Shape                                      | Bases read today | Lost?                   |
| ------------------------------------------ | ---------------- | ----------------------- |
| `EXAMPLEUSDT`                              | EXAMPLE          | no                      |
| `1000EXAMPLEUSDT`, `EXAMPLE1000USDT`       | EXAMPLE          | no                      |
| `AAAUSDT, BBBUSDT and CCCUSDT`             | AAA, BBB, CCC    | no                      |
| `Example (EXAMPLE) EXAMPLEUSDT`            | EXAMPLE          | no                      |
| `Example (EXAMPLE)` only                   | none             | **yes**                 |
| `Aaa (AAA) and Bbb (BBB)` only             | none             | **yes**                 |
| `1000EXAMPLE (1000EXAMPLE)` only           | none             | **yes**                 |
| `AAA/USDT`                                 | none             | **yes**                 |
| `AAA USDT` (space)                         | none             | **yes**                 |
| `AAAUSDC` (other quote asset)              | none             | **yes** (see below)     |
| `Multiple Perpetual Contracts` (no ticker) | none             | **yes** (unrecoverable) |
| `ALL Composite Index Perpetual Contract`   | none             | **yes** (not an asset)  |
| `AAAUSDT and Bbb (BBB)`                    | AAA              | **partly**: BBB lost    |
| `PENGUUSDC, CVXUSDT and SLPUSDT`           | CVX, SLP         | PENGU dropped           |

The same parenthesised form is explicit for A1 (`Binance Will List Example (EXAMPLE)`
gives `EXAMPLE`) and ignored for A2 (`test_a2_parentheses_are_read_for_spot_but_not_for_futures_titles`).
`test_a2_no_base_article_is_counted_as_status_no_base_not_dropped` pins that a lost
base is reported as `no_base` and is never silently dropped.

**What this proves:** the A2 rule understands one form only. It also ignores a
parenthesised ticker, as the colleague's example showed.

**What it does not prove:** that any real article has one of the lost shapes, or that
any of them explains the 134 holdout articles. The matrix is written from the code, not
from titles.

## 3a. The USDC and multi-asset cases are not parser questions

- A `...USDC` perpetual of a base that already trades as a `...USDT` perpetual on the
  same venue is a new margin asset, not a new listing. Counting it would change what an
  A2 event is, which is a definition change, not a parse fix. No change is proposed.
- A title that says `Multiple` names no ticker; the list is in the article body. No
  title rule can recover it, and the body is a new data read.

## 4. Discovery-year titles (step 2)

Source: the kept receipts of run 5, discovery window only, read by a local script that
filtered the window before printing. The script and its output are not stored in Git.

All 106 discovery-year A2 titles share one template:
`Binance Futures Will Launch USDⓈ-Margined <TICKERS>USDT Perpetual Contract(s)`,
optionally followed by `With up to Nx Leverage`, `Pre-Market Trading` or a
`(YYYY-MM-DD)` date.

| Property                                        | Discovery year |
| ----------------------------------------------- | -------------: |
| A2 articles                                     |            106 |
| Titles with a `...USDT` token                   |            100 |
| Titles with a parenthesised ticker              |          **0** |
| Titles with a `/USDT` pair                      |          **0** |
| Titles with `USDC` (4, three of them USDC-only) |              4 |
| Articles with no base                           |              6 |

The 6 no-base articles are: 2 `Multiple ... Perpetual Contracts` titles (no ticker), 3
USDC-only titles (`AAVEUSDC and UNIUSDC`, `BIOUSDC`, `WLFIUSDC`) and 1 index product
(`ALL Composite Index`). A parenthesis rule would recover none of them: adding
`_parens` to the A2 branch changes **0 of 106** discovery events. So the discovery
year can neither show nor refute a need for it, and any evidence for a rule change has
to come from the holdout's title shape (section 6 records the one look taken).

## 5. What the public aggregates say about the holdout

From the published report only (no title, ticker or date): holdout A2 has 136 articles,
2 events, `status = {matched: 2, no_base: 134}`. There is **no** `no_candidate` and **no**
`no_live_perp` row. So the 134 articles did not lose their events at the Bybit join:
they gave no base at all. That points at extraction, but the aggregates cannot say which
shape. Hypotheses, none of them tested:

1. a form the A2 rule does not read (parenthesised ticker, `AAA/USDT`, spaced pair);
2. a title with no ticker (`Multiple ...`), whose list is in the body: unrecoverable
   from titles, and then not a parser defect;
3. a different article population matched by the `Futures Will Launch` and `Perpetual`
   pattern (for example a coin-margined product);
4. a mix.

Only hypothesis 1 is fixable by a parser change inside the contract. Hypotheses 2 and 3
are data-class limits and would keep A2's holdout at `lab_only` whatever the parser does.

## 6. The holdout structural look (approved by the owner, run once)

**Approval.** The owner approved one structural check of the 134 holdout A2 titles on
2026-11-16 ("Да, разрешаю"), relayed through the coordinating session, under these
limits: classes fixed in advance; output only the total (asserted to equal 134) and one
count per class; no title text, ticker, article id, date or count by month or week; no
intermediate file; run exactly once; no iteration on the classes after the counts.

**Order.** The class rules, the classifier and its synthetic tests were committed and
pushed first (`026902d`, `catalyst_a2_shape.py`, `test_catalyst_a2_shape.py`), then the
command was run once.

**Classes, in this precedence.** `usdt_pair` (a `...USDT` token the current A2 rule
reads), `paren_ticker`, `slash_pair` (`AAA/USDT`), `spaced_pair` (`AAA USDT`),
`usdc_pair` (`AAAUSDC` or `AAA/USDC`), `multiple_no_ticker`, `index`, `other` (everything
else, including coin-margined and any other quote asset).

**Command and result.** Read-only on the kept receipts of run 5 (nothing under
`schurfer-runs/` was written):

```text
python -m schurfer_analytics.catalyst_a2_shape --root <run 5 root> --window holdout --expect-total 134
```

| Class                | Count |
| -------------------- | ----: |
| `usdt_pair`          |     0 |
| `paren_ticker`       |     0 |
| `slash_pair`         |     0 |
| `spaced_pair`        |     0 |
| `usdc_pair`          |     0 |
| `multiple_no_ticker` |     0 |
| `index`              |     0 |
| `other`              |   134 |
| **Total**            |   134 |

**What the counts allow, and no more.**

- `usdt_pair = 0` is the consistency check: these are the articles the rule read no base
  from.
- No parenthesis, slash, spaced or USDC rule would recover a single one of the 134. The
  hypothesis that the loss is the missing parenthesis form (section 5, hypothesis 1) is
  not supported.
- The `Multiple` and index shapes are not what lost them either (both 0).
- `other` is 100% and so the look did not identify the shape. It says the classes did
  not recognise the title format, and nothing about whether events or an edge exist.
  The classes cannot tell whether a parser could read these titles, so no parser change
  is shown to matter and none is proposed. The look does not show that the titles are
  unreadable either.
- Not tested, and not claimed: that the titles are of a coin-margined product, use another
  quote asset such as `BUSD` or `USD`, or are another article type matched by the
  `Futures Will Launch` and `Perpetual` pattern. The classes were fixed before the run and
  are not changed after it. A second class selection on the holdout titles is not
  allowed now.

**Status of A2.** It stays `lab_only`; the cause of the lost bases is not established.
This look is logged in the
[registry](seen-windows-registry.md) as a structural look at the holdout titles of A2
(shape-class counts only, no returns).

## 7. If a parser defect is later confirmed (design only, nothing implemented)

Sketched so that the decision is not slowed by design work. None of it is in force, and
after the look in section 6 there is no confirmed defect to apply it to.

- **Amendment, written before any recount.** It fixes exactly the A2 base rule (which
  forms are explicit) and states that minimums, windows, other buckets and the USDC
  definition do not change, and that exactly one recount runs.
- **Recount command**, separate from `report`. `report` keeps refusing to run from a
  revision other than the fetch revision; that check is not relaxed. The new command
  (`recount`) takes `--root` (the original run), `--expect-binding-sha256`
  (`c477aebd...`), `--expect-report-sha256` (`39f06357...`),
  `--expect-holdout-events-sha256` (`52baf153...`) and `--out`. It refuses unless the run
  root's `run.json` hashes to the expected binding, the original report in the evidence
  folder hashes to the expected SHA, and the kept receipts parse to the same article set
  (same ids and times) as the original. It re-classifies the same kept receipts with the
  new parser version, never fetches and never rewrites the original `report.json.gz`,
  the sealed `holdout-events.json` or anything under the run root.
- **Provenance of the output.** The recount output is a new file with a new rules
  string (for example `catalyst_feasibility_v1_recount1`), the new code revision, the
  original binding and report hashes it was checked against, the amendment's path and
  hash, and a flag `recount_of`. Its sealed holdout list is written beside it as a new
  file, never over the original.
- **Tests.** Wrong binding, wrong report SHA or a changed receipt must refuse; a
  recount with an unchanged parser must reproduce the original report byte for byte
  apart from provenance; the original files must be untouched after a run.

## 8. Open questions for the colleague

1. **Holdout status.** The holdout titles of A2 have now had one structural look
   (section 6, counts only). If the A2 rule is ever amended and recounted, is that a
   holdout read under the [registry](seen-windows-registry.md)? Proposed: only if the
   amendment rests on evidence that does not come from the holdout titles beyond the
   disclosed look, and the recount is recorded as a disclosed look.
2. **Sealed list hash.** The holdout events hash `52baf153...` covers 2 events. A
   recount that finds more events must produce a new sealed list and a new hash. Should
   the original hash stay the evidence of the first count and the new one be added as a
   second entry (proposed), never a replacement?
3. **A definition question, separate from the parser:** should a `...USDC` perp
   listing ever be an A2 event? Proposed: no.
4. **Status and return condition (stated, not a question).** A2 stays `lab_only` and
   the cause of the lost bases is not established. A2 returns only when independent
   information about the title format appears (for example from a documented source, or
   from discovery-year or other non-holdout material) or when a new confirming window
   exists. No second class selection on the holdout titles is made in the meantime.
