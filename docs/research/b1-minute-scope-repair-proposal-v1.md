# B1: remove the unverified hourly envelope from candidate selection

Status: **proposal for design review, 2026-11-12**. The local diagnostics and request
plan below have run. The proposed selection/report method has not been activated,
no supplemental premium has been fetched and no strategy return has been read.
This amends B1's data-selection method only if accepted. It changes no trading
threshold, decision offset, exit, cost, holdout, trading mode or production service.

## Diagnosis: the hourly selection proof does not hold for these inputs

The [original B1 result](bybit-presettlement-feasibility-result-2026-11-12.md) is
retained unchanged: `superset_rebuild_needed`. The parser follows the documented
start/open/high/low/close order. A native saved 0GUSDT hour was checked through
request parameters, response symbol, serialization and `load_hourly`: all agree.
Its 60 native minute rows are individually coherent, yet 53 closes are below the
reported hourly low and 3 above the high. Serializing the native hourly payload
and loading it again preserves both fields exactly. There is no demonstrated
field-order or snapshot-writer bug to patch here.

The same hour matches the independently saved HYP-034 hourly payload. Even the
illustrative response on Bybit's [official endpoint page](https://bybit-exchange.github.io/docs/v5/market/premium-index-kline)
shows an open below its stated low. This does not establish the exchange's internal
aggregation semantics or prove all its historical data corrupt. It establishes
that a conventional nesting assumption is not safe for these snapshots. Swapping
fields or lowering the consistency threshold cannot repair the missing proof.

## Proposed superset, with a direct completeness argument

For every forecast interval `iv` and existing decision offset d in 30/15/10 minutes,
compute only the already registered, premium-independent conditions at `t - d`:

1. no later settlement already known at the decision supersedes the forecast;
2. the last two known settlement intervals agree;
3. the original closed-bar USD 2M liquidity condition holds;
4. at least one funding settlement at or below -0.30% is known in the prior week.

An interval enters the candidate superset if these hold at any d. Fetch minute
premium for that interval without consulting the hourly premium. Compute the
unchanged F_hat trigger only after the minute series has been validated.

Let C(iv,d) be the conjunction above. The old entry definition is
C(iv,d) AND complete-minute-premium AND F_hat <= -0.30%. Therefore every possible
entry implies C(iv,d). The superset is the union of C over the three fixed offsets;
no hourly-low assumption, future settled rate, `actual_next` or strategy outcome
is needed for that implication. This is a broader enumeration of a fixed signal,
not a new trading filter selected to improve returns.

Keep the original independent 2,000-interval sample, seed 31, unchanged. The original
index, previously captured minute responses and blocked result remain immutable.
Hourly/minute disagreement remains a diagnostic and must not disappear from the
history; it no longer gates the completeness proof because hourly extrema no longer
select the universe.

## Resource priority and computed scope

The program plan prioritised **4-hour contracts before this read**. The proposed
first supplemental run covers that class plus the unchanged original validation
sample across classes. Other classes do not silently pass or get a new result;
expanding them remains a separate budget decision.

The offline planning command inspected the pinned index and request metadata only.
It fetched nothing and computed no new premium or strategy-return value.

| Item                                           | All classes, for comparison |         Proposed first run |
| ---------------------------------------------- | --------------------------: | -------------------------: |
| Potential intervals before the F_hat trigger   |                      49,286 | 17,892 four-hour intervals |
| Candidate plus original validation union       |                      51,164 |                     19,845 |
| Existing requests with exactly matching bounds |                      15,947 |                      8,185 |
| Additional requests                            |                      35,217 |  **11,660**, all four-hour |
| Additional minute bars                         |                   6,235,620 |              **2,798,400** |

All 8,185 reused records were separately checked for the exact expected minute
timestamps and matching request bounds; none needs a timestamp-gap repair. Finite
values had already been checked by the original loader. This does not replace the
new merger's identity, numeric, duplicate and integrity checks.

Observed four-hour raw snapshot records average 14,837 bytes. That extrapolates to
**173,001,774 bytes, about 173 MB decimal**, for the extra records. This is a storage
estimate from the existing capture, not a measured network budget. At 3 requests per
second the base request count needs about **1.08 hours**, excluding retries and
interruptions.

### Small pilot before the supplemental run

Freeze 64 missing intervals by the smallest SHA-256 of
`b1-minute-scope-repair-pilot-v1:<symbol>:<start_ms>`; their IDs are preserved now.
The pilot reads historical minute premium only, not trade prices or returns.

- At most 128 HTTP attempts including retries, 3 requests per second, 10 MiB of
  response-body bytes before JSON parsing, inclusive of failed/retried requests.
- Request endpoints, instruments and bounds are exactly those of the frozen list.
  Fail on wrong symbol/category, invalid boundary, conflicting duplicates or
  non-finite values; retain raw responses and error reasons.
- At least 95% of the frozen intervals must have complete expected timestamps.
  Missing intervals stay missing, without replacements. This is a transport/data
  gate; 64 intervals cannot pass S1's statistical floors or prove profitability.
- A failure stops the supplemental run. Do not loosen this gate after the pilot.

The 64 successful pilot intervals are reused, not downloaded again. The full run's
budget is at most 23,320 HTTP attempts and 512 MiB of response-body bytes before JSON
parsing, retries included. Persist consumed attempts/bytes so a restart cannot reset
the budget. A cap stops the run with explicit incompleteness. No archive purchase or
new production writer is needed.

## Implementation requirements after design review

1. Preserve the existing selection/report version and original result. Add an
   explicit version for `prior_filters_then_minute_premium`, bound to the index,
   original minute SHA, supplemental SHA, scope SHA and code revision.
2. Write a supplemental immutable snapshot. Do not modify the original files.
   Merge by native symbol and minute timestamp; equal duplicate observations fold,
   conflicting OHLC values fail instead of letting the last file win.
3. Reuse the existing boundary, retry, raw-snapshot and report machinery. Add and
   test persisted request/byte caps and pilot reuse before a request can run.
4. Keep S1 formula tolerance, sensitivity, specificity, coverage, sample and minimum
   counts unchanged. Apply the 4-hour class's own gates and S3 floor unchanged;
   a pooled result cannot admit it. Preserve the current pooled formula gate as
   well, using the candidate-plus-original-validation population specified here.
5. Report every missing/invalid interval in the original fixed denominator. No
   return is computed, no event is replaced and no d is chosen by performance.
6. Only an accepted new B1 report can release B2 for that class. A failure retains
   its reason and ends this attempt; no threshold relaxation follows the read.

## Evidence already produced

[Evidence](evidence/b1-minute-scope-repair-v1/) contains the frozen proposed scope,
pilot IDs, request counts, payload subset, diagnostic scripts and SHA-256 manifest.
The all-class plan is capacity context, not another active experiment.

- 6,000 prior-filter checks against the original functions on the original
  independent validation sample matched exactly.
- A synthetic counterexample with a true minute trigger and zero hourly lows is
  retained by the new scope while the old hourly filter would exclude it.
- Future and same-cut funding settlements are excluded from recent-extreme inputs.
- Native request/response identity and hourly serialization roundtrip checks passed.
- No market request, perpetual-return read, test-year read, order or production
  mutation was performed. The active B1 verdict remains blocked until review,
  implementation and validation of this amendment.
