# B2 native source pilot, 2026-11-13

The fixed source pilot was downloaded without errors. This is a source-quality
study, not a maker-fill or profitability result. B1's frozen 4h event population,
entry policies and economic thresholds are unchanged.

## Exact download

| Item                                                         |      Observed |
| ------------------------------------------------------------ | ------------: |
| Symbol-days / native files                                   |       12 / 24 |
| Original ob500 pilot days / additional ob200 format fixtures |        10 / 2 |
| GET attempts / retries / failed requests                     |    24 / 0 / 0 |
| Native body bytes                                            | 1,555,848,890 |
| Durable byte reservations including guards                   | 1,558,183,936 |
| Hard transfer cap                                            | 2,147,483,648 |

The downloader ran from clean `c1c9fae`. Every Content-Length and ETag matched the
frozen HEAD inventory, and every completed body has a SHA-256 receipt. Failed or
partial attempts would have retained their entire reservation; no retry was
needed. No archive outside the fixed plan was requested. The full 75.79 GB corpus
has not been downloaded.

The plan SHA-256 is
`39c5bda6f438f51e90fbb899a2eb93250acf995727d032a6e725f5a011fe8630`.
The sealed native-file manifest SHA-256 is
`527b7446887d1f6c466c9891d9f1ef67134dc4b86aaf7a30d3f83c8ddc9bb0a0`.
[Evidence](evidence/b2-archive-scope-2026-11-13/source-pilot/) retains the exact
binding, all attempts, version headers, receipts, budget ledger and diagnostics.
Native archive bodies remain in ignored local runtime storage, not Git.

## Schema and eligibility limitations

The first-record/header inventory found `cts` in all 12 book archives. Seven
trade CSVs have an `RPI` column; five do not. None has a `BT` column. Thus absence
of a block/RPI flag cannot establish ordinary API-executable volume. Current API
documentation is not substituted for missing historical fields.

This restriction matters before a queue replay: unclassified prints cannot be
called identified maker fills. The registered excluded-volume bound and an
included-volume sensitivity must remain distinct. Unknown execution evidence is
not an observed unfilled order or a cash outcome. Historical native lot/minimum
constraints also remain unidentified, so continuous USD 50 quantities are not
proof that native orders would have been accepted.

## Archive-boundary correction

The original reader, clean `c1c9fae`, rejected all 12 pairs because it assumed
every book row belonged strictly to the filename's UTC day. Its exact
`quality-v1.json` is retained, SHA-256
`37edabafeb9954f63953fa9e0db433398d853f41a43ccc157392eaa119444f0d`.
It streamed 7,129,991,849 inflated bytes in 541.073 seconds and computed no fills
or strategy returns.

A timestamp-only first/last-record check then found that all 12 files end with
a snapshot 0.655 to 1.904 seconds after the next midnight. Before rerunning,
clean `1f03acf` registered version 2: count off-day book rows and skip their levels
before numeric conversion or trade alignment. Entirely off-day books remain
invalid. This clips the native source to the existing population; it changes no
signal, execution policy, threshold, source file or event ID. The first failure
is not erased by the corrected read.

## Scope of the read

Native trade minutes :55 through :04 remain row-count-only, preserving the
HYP-031 seconds-study reservation. No masked price, size or turnover was converted
into an analytical observation. Source diagnostics emit no numeric quotes, price
series, fill quantities, fill rates or strategy outcomes. No test-year, backward
holdout, HYP-012 v2 blind-window, production-service or live-order action occurred.
