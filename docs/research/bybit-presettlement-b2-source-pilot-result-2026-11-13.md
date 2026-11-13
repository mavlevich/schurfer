# B2 native source pilot, 2026-11-13

**Source inspection complete: 12/12 pairs parsed; executable volume remains
unidentified.** The fixed source pilot was downloaded without errors. This is a
source-quality study, not a maker-fill or profitability result. B1's frozen 4h
event population, entry policies and economic thresholds are unchanged.

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

## Completed native read

Source-quality v2 ran from clean `1f03acf` in 510.436 seconds, streaming
7,130,337,280 inflated bytes with bounded memory. Its output SHA-256 is
`7cb1a8af1444fd890e3566b799991c18cfa2ac880457a788ef38bf8564732b67`.

| Diagnostic                                        |       Observed |
| ------------------------------------------------- | -------------: |
| Pairs successfully inspected                      |        12 / 12 |
| In-day book messages, all with engine `cts`       |      8,310,362 |
| Snapshots / deltas                                | 13 / 8,310,349 |
| Off-day book rows ignored before level conversion |            152 |
| Observed clock / sequence regressions             |          0 / 0 |
| Observed unmasked empty / crossed books           |          0 / 0 |
| Native trade rows                                 |     11,493,698 |
| Reserved trade rows, count only                   |      2,073,501 |
| Unmasked trades checked                           |      9,420,197 |
| Native lot/minimum constraints identified         |         0 / 12 |

Reconstructed depth is 500 levels per side on each original fixture and 200 on
each extra-format fixture. Per-file median engine-clock gaps range from 100 to
147 ms; p99 from 134 to 1,804 ms. The worst gap is 13,816 ms. Median system minus
engine timestamp ranges from 3 to 40 ms; p99 from 39 to 100 ms. This is calibration
data, not an adopted replay gap threshold. Per-event continuity and an explicit
gap rule still have to be fixed before a fill replay.

The strictly earlier **engine-clock** quote check records 8,508,452 consistent
trades, 911,480 price/side disagreements and 265 quotes older than its diagnostic
5-second limit. These are source-alignment diagnostics, not fills, eligible-flow
labels or a 90.3% execution success rate. In particular, engine `cts` is not a
receipt/availability timestamp: a decision-time quote must also respect published
`ts` and the registered latency model. The diagnostic must not be reused as an
entry rule with retrospective quote availability.

## Quantity and flag diagnostic

The strict decimal notional check flagged 1,317,035 trades. Before investigating,
clean `5e8f09d` registered a source-only breakdown on the same 12 tapes, with fixed
relative-error bins and no replacement gate. The result SHA-256 is
`129ccf6e1803aedac7f4e8a5fc67eb752be692845bba54bec83fdf6b2c27df11`.

- All 9,420,197 checked rows have `homeNotional == size` exactly.
- `foreignNotional == price * size` exactly on 7,065,040 rows. The remaining
  2,355,157 all have relative error at most 1e-15; the maximum is approximately
  3.123e-16. This is consistent with binary floating-point serialization, not
  evidence of a quantity multiplier discrepancy. The original strict counter
  remains in the report; it was not silently removed or converted to a pass.
- `BT` is absent on every checked row. Native RPI representations are `0` on
  6,440,684 rows, `1` on 161, and absent on 2,979,352. These are observed labels,
  not proof that an RPI-zero row is an ordinary non-block trade.

No identified ordinary-volume estimate follows from this source. A later replay
can report explicitly bounded assumptions, but cannot call an all-prints
sensitivity identified API-maker execution. A complete download is not grounds
to fetch the full corpus or promote a strategy.

## Verification and next boundary

Eleven focused transport/source tests pass, including interrupted/resumed
downloads, exhausted attempts, byte-cap refusal before body consumption, source
version mismatch, corrupted native bodies, masked tape and the off-day trailer
regression. The ten metadata-scope tests also passed. Ruff and strict mypy pass
for the product modules/tests. Full `make verify` passed on an isolated local
PostgreSQL before and after the boundary fix. The auxiliary unit diagnostic passed
Ruff and its synthetic controls before the registered native read.

Next comes a separately fixed pilot replay with explicit clock availability,
per-window continuity, unknown-flow bounds and native sizing limitations. This
source study computed no fill rate and released no B3 economic read. The source
limitation should be resolved or carried as a bound before any larger download.

## Scope of the read

Native trade minutes :55 through :04 remain row-count-only, preserving the
HYP-031 seconds-study reservation. No masked price, size or turnover was converted
into an analytical observation. Source diagnostics emit no numeric quotes, price
series, fill quantities, fill rates or strategy outcomes. No test-year, backward
holdout, HYP-012 v2 blind-window, production-service or live-order action occurred.
