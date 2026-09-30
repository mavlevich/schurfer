# Pre-blind book-cost baseline v1

Status: REGISTERED 2026-09-30 in #477 (`33cf8da`). The protocol and disabled
reader were merged before any production cost value was read. This separate
results PR activates the fixed read and publishes one immutable, hashed result
only after its own merge. It does not change the population, formula or groups
below.

## Question and allowed decision

How large a midpoint move would have been needed to pay the visible taker
book cost and a **scenario** fee on the quote samples already captured before
the HYP-012 v2 blind window? This is an engineering cost diagnostic, not a
strategy test, a realized fill study, or evidence of a profitable signal. It
may inform the minimum effect and capacity requirements of a new, separately
registered cohort. It cannot change HYP-012 v2 or HYP-015, revise their cost
models, select assets or parameters, or authorize orders.

## Fixed populations and data boundary

The window is **[2026-08-01T00:00:00Z, 2026-09-29T00:00:00Z)**.

1. **Momentum paper:** one row for every
   `app.momentum_flow_paper_probes` with `watch_decision_at` in the window
   and final `updated_at` before its end. A row revised later is excluded
   rather than letting its post-cutoff state into this read.
   `paper_version` is the primary key of `momentum_flow_paper_runs`, so the
   contract-notional join cannot duplicate a probe. Report each
   `paper_version`, venue and contract notional separately. Only
   an opened, closed probe with entry and exit quotes observed before the end
   and both quote sides filled to the contract notional contributes a paired
   cost. Entry and exit quote status, timestamps, spread, impact and filled
   notional are allowed. No return, PnL, fees, funding, OHLCV, outcome row or
   price level is selected. A quote observed after the end has its cost fields
   masked by SQL before the row reaches Python.
2. **Source-lead:** every `app.source_lead_captures` whose
   `source_first_observed_at` and final `updated_at` are in the window, left
   joined to target observations **observed and last updated before the end**.
   Retain missing and failed targets in the denominator. Report separately by capture version, source
   and target venue, and requested size. A sampled target with complete ask
   and bid depth contributes an **immediate same-book crossing scenario**.
   Its bid is not an observed 30-minute exit and cannot be pooled with paper
   entry/exit pairs. Neither qualifications nor v2 outcomes are read.

Identity and versions are taken from persisted capture-time rows, not today's
catalog. The two populations, paper versions, venues and sizes are never
pooled into one headline estimate. Missing and rejected quotes stay in the
funnel; no missing cost is replaced by zero. Count rows excluded because their
`updated_at` or target observation falls on or after 2026-09-29 in separate
count-only queries. They supply no cost value or current operational state.
The queries run together in one read-only, repeatable-read transaction, with a
fixed 100,000-row ceiling per population.

## Cost definition

For each eligible long quote pair, let `a` be ask VWAP impact at entry and `b`
bid VWAP impact at exit, each as a fraction of **its own book midpoint**.
For a hypothetical fee `f` per side, the required midpoint rise in bps is

`10,000 × ((1+a)(1+f) / ((1-b)(1-f)) − 1)`.

The registered fee scenarios are **0, 5.5 and 10 bps per side**. They are
assumptions, not claims about the account's actual tier or filled orders.
Spread is shown as a liquidity descriptor; it is already inside VWAP impact
and is never added a second time. Funding, liquidation risk, latency between
quote and order, maker fill probability and adverse selection are not in this
threshold. The two paper quotes each target the contract dollar notional; they
need not represent exactly the same base-asset quantity. Thus the threshold
is a **book-cost scenario**, not a reconstructed trade return.

Only recorded notional sizes are supported. Historic rows persist VWAP and
impact summaries, not the 50 raw book levels. The lev3 paper contract has
observed **$150** quotes and will be shown separately; differences from the
$50 policy are not a same-book size experiment. Repricing a $50 observation at
$500 or $5,000 would be invented depth; this report will explicitly mark
those sizes **unavailable from historical depth**. A separate prospective
capture of raw depth would be required, with `not_executable` when even 50
levels cannot fill a requested size. No scaling curve is inferred here.

## Registered output of the separate results PR

- Full denominators and reasons: unopened/open paper, missing exit,
  incomplete depth/invalid cost, missing and failed source targets, plus
  count-only exclusions for paper/capture rows updated after cutoff and target
  observations outside the pre-blind snapshot.
- For each population/version/venue/notional and entry-spread bucket
  `<5`, `[5,20)`, `[20,50)`, `≥50` bps: `n`, mean, p50, p90, p99 and max of
  entry spread, entry ask impact, exit bid impact and the break-even threshold
  in each fee scenario. Groups also distinguish both books fresh (`−1000` to
  `2000` ms), unknown timestamp, and outside that range. Small groups retain
  their `n`; they are not presented as precise population estimates.
- Exact SQL/query version, code revision and dirty-tree flag, read timestamp,
  fixed window, row counts and deterministic input digest. The result is
  written once with SHA-256 to `runtime/research/preblind-book-cost-baseline`.
  A retry may verify the existing artifact, never silently replace it.

The results reader first saves the queried rows to a write-once `inputs.json`
with SHA-256 under that directory. A crash before `result.json` resumes from
those frozen rows, without querying the database again. A crash between the
result file and its digest completes only if the file equals a replay from the
frozen input. Once complete, reruns verify the saved hashes without a database
read. The production command requires a clean `main` and uses the fixed output
directory; the analytics image is built for the one-shot command without
restarting the persistent scanner.

The result is descriptive and may support a later power calculation. A future
cohort must still register its own executable side, costs, target effect,
cluster-aware sample size, calendar limit and one formal read before results.

Read completed once on 2026-09-30 under merged reader `9774c79`. The complete
funnel, all registered groups and the two artifact hashes are recorded in
[preblind-book-cost-baseline-v1-readout.md](preblind-book-cost-baseline-v1-readout.md).
The fixed window contained no $150 lev3 row, so the anticipated second size
point was unavailable in this read.
