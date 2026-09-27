# HYP-012c: pooled source-gap rule on the HYP-012b holdout (v1)

Status: REGISTERED 2026-09-27, before any holdout read. The HYP-012b holdout (ISO weeks 36-39,
2026-08-31..09-28) was never read, because HYP-012b had no discovery survivor. No holdout input
had been prepared when this was written. The code refuses the read before 2026-09-29T00:00Z.

Code: `source_lead_gap_hyp012c.py` (CLI `source-lead-gap-hyp012c`, target
`make prod-source-lead-gap-hyp012c`). It reuses the HYP-012b preparation, claim and read
(`source_lead_multi_source_report.py`).

## Why

HYP-012b found no venue whose signal pays on Bybit when the entry is at the signal. The
exploration on its burnt discovery window found one structure worth a single honest test. When a
source's first price is well above Bybit's last pre-signal close, the move had not yet reached
Bybit.

In the band 0.6% to below 2%, over the five formal sources, the discovery window had 284 episodes
over 126 assets. The mean net was +0.46%, but the median was -0.32% and the win rate 44%, so the
mean rests on a tail. Episodes were even across the three weeks (95, 105, 84).

The upper bound 2% was chosen on that exploration: 2-5% gaps looked negative. Only the independent
holdout tests the rule as a whole.

## Rule (fixed before the read)

- **Candidates.** HYP-012b holdout candidates, prepared by the same code and rules:
  - unique first source;
  - HYP-012 identity checks;
  - a single Bybit perpetual live over the episode;
  - the 2x price identity filter.

  From the five formal sources BloFin, MEXC, BingX, Gate and LBank, one episode per event.

- **Band.** `0.006 <= source_first_price / bybit_last_closed_close - 1 < 0.02`. The reference is the
  close of the Bybit minute that ended at or before the signal, and the band uses pre-signal data
  only.
- **Other statuses.** Candidates from other sources are `other_source`; outside the band,
  `out_of_band`; with no usable reference, `no_gap`. These are counted and never evaluated, so
  their holdout outcomes stay unread.
- **Entry, exit and costs.** Unchanged from HYP-012b:
  - entry at the open of the first minute after the signal;
  - exit at the close of the bar at entry + 30 minutes;
  - cost function `hyp012b_taker10_impact20_funding5per8h_v1`, literally 0.403% per trade (10 bps
    taker per side, 20 bps impact, 5 bps per 8h funding over 31 minutes).

## One test, one verdict

- **The test.** One pooled hypothesis, `pooled_gap_0.6pct_to_2pct`: the mean net return against
  zero, with a cluster bootstrap by asset (10,000 iterations, seed derived from the family),
  alpha 0.05.
- **Floor.** At least 100 resolved episodes, at least 30 assets, and no week above 45%.
- **Missingness ceilings.** Over either one, a positive verdict is not allowed.
  - **Unknown gap: at most 5%.** The denominator is every candidate of the five sources that
    reached the price check, including those excluded there. The unknown ones are those whose
    gap cannot be known: no source price, no reference bar, or a failed fetch. A price-level
    mismatch is a known gap outside the 2x band, so it is not unknown.
  - **Unresolved: at most 5%** of the in-band candidates.
- **Verdict**, in this order:
  1. `insufficient_data` when no estimate exists;
  2. `fail` for a mature result (at least 100 resolved) with a mean at or below zero, even below
     the floor or over a ceiling;
  3. `insufficient_data` below the floor, or over either missingness ceiling;
  4. `candidate` for a positive mean with p below 0.05;
  5. otherwise `fail`.
- **By source.** Per-source rows are descriptive only: in-band count, resolved, assets, mean and
  unresolved reasons, with no p-value and no interval. Missingness is reported by source and ISO
  week.
- **No adjustment.** If the blind counts show too little data, the result is `insufficient_data`.
  The band and the ceilings are never moved.
- **Pinned contract.** Every threshold above, plus the cost function, is hashed into one contract
  digest.

## Protocol

- **Two phases and a claim** (as HYP-012b).
  1. `prepare` freezes the inputs once and computes no return. The inputs are the catalogue,
     candidates and raw klines. It then prints the blind band counts.
  2. `read` creates the claim and computes the result from the stored inputs only. The claim
     pins the inputs, the one-hypothesis family, the contract digest, and the reader's own code
     revision and dirty flag.

  A crashed read resumes only with the same inputs, contract and reader; a completed read is
  never repeated. The result records the reader's revision next to the one that prepared the
  inputs. Files are
  published atomically.

- **Maturity.** Refused before 2026-09-29T00:00Z.
- **Nothing on or after 2026-09-29 is read,** for any venue: the HYP-012 v2 cohort starts then.

## What a verdict allows

A `candidate` is a historical price-filter result, not proof of an executable edge. A large gap
can also mean a stale Bybit price or a wrong identity that the 2x band did not catch.

So a `candidate` only allows:

1. checking the rule on identity-confirmed routes;
2. a shadow-execution check of the same rule.

Each is registered separately. It never allows an order. `fail` and `insufficient_data` close the
line.
