# I25 stage 3: power calibration, dispersion only (v1, DRAFT, 2026-11-20)

Status: **draft for two external reviews, then the owner's separate go for the price read.**
This document, its frozen contract and its code fetch nothing and read no price. The code was
tested only against a mock transport and a synthetic Bybit; it was never run against a network.
It is the stage 3 gate of the [I25 laboratory protocol](new-perp-short-lab-protocol-v1.md)
(sections 7.4, 8, 10 and 11) and it changes the protocol text only through the proposed
Amendment 7 of section 11 below.

- Frozen contract: [`contract.json`](evidence/new-perp-stage3-v1/contract.json).
- Code: `apps/analytics/schurfer_analytics/new_perp_stage3.py`. It reuses the stage 1 fetcher,
  reducers, verdict loader and cohort rules by import (`new_perp_stage1.py`,
  `new_perp_stage1_verdict.py`); no new dependency.
- Tests: `apps/analytics/tests/test_new_perp_stage3.py` (89 tests, section 15).
- Registry: the section "I25 Stage 3 registration" of the
  [seen-windows registry](seen-windows-registry.md).

## 0. What the owner is asked to approve: one go

One class of requests, one go, one hash. Nothing below is approved by this document being
merged; the price read needs the owner's separate go for the exact hash below, after review and
merge.

```text
request_list_sha256 (whole contract, bound into the run) =
  3392f75267e684e46093cbc0fb55e0714036df585a98d220b416dae4594a08d9
go_sha256["1"] (what the owner approves) =
  701fcfd51b9d10f0e4093ef9eeb585b8a97a95e66bba7685dcfd9666dead7daf
contract.json file SHA-256 (as formatted by prettier) =
  3d5b7b9e3740a97df167378ee756a05c94d008874ac6d5329bd866710a24f659
```

Check without a network request (the first two are the only commands run in this work; the third reads the sealed
Stage 1 root locally and prints aggregates only, so the owner can see the cohort size and slot count before approving):

```bash
uv run python -m schurfer_analytics.new_perp_stage3 plan
uv run python -m schurfer_analytics.new_perp_stage3 write-contract --check
uv run python -m schurfer_analytics.new_perp_stage3 cohort --stage1-root <sealed Stage 1 root>
```

## 1. What stage 3 settles, and what it does not

The stage 4 power statement needs two numbers that the protocol only assumed: the dispersion
of a weekly return of a young perpetual (planning guess 1,500 to 2,500 bps) and the dependence
between launches (planning guess `DEFF` 1.0 to 4.0). Stage 3 **measures both on the pre-entry
days** of the discovery-year cohort, so that the MDE of stage 4 is computed from measured
variance and the cheap stop `insufficient_power` can happen before any return is read.

It settles: the exact estimand (section 3), which bars are read (section 4), how the cuts are
enforced (section 5), the statistics and the gate (section 6), the pre-registered decision
table (section 7) and the request list (section 9).

It does **not** settle, and says so: whether a short of new perps has an edge; the mean, sign
or size of any outcome (none is computed, section 8); whether the pre-entry dispersion carries
to the days after entry (an assumption, section 13); anything about the backward holdout (not
touched, and no holdout fact tunes any parameter here).

## 2. Inputs: the cohort and its prerequisites

- **Cohort: the 228 primary V1 episodes in 48 ISO weeks** of the completed discovery Stage 1
  run (`n_primary` 228, `iso_weeks` 48 in the reviewed
  [report](evidence/new-perp-stage1-v3/stage1-report.json.gz)). The protocol's 263 is the
  launch count; 35 launches are out for reasons already decided at stage 1 (6 `t0_ambiguous`,
  1 `t0_unavailable`, 16 `window_censored`, 12 `lookback_censored`, none for identity, funding
  or entry bar). 263 is therefore not the request count of stage 3; 228 is.
- **How the cohort is derived: locally, with no request.** The sealed Stage 1 run root (outside
  Git) is read through `new_perp_stage1_verdict.load_run`, the stage 1 launch statuses are
  recomputed by the stage 1 functions, and the episodes with reason `primary` are the cohort.
  Before any request the code requires that (a) the root's `manifest.json` has SHA-256
  `759305ceaf5730ff0b7f23963a2c17292590ecb590d0336376cc139a0b5ca76d` (this fixes the Go 1 plan,
  not the Go 2 results), (b) the committed report decompresses to SHA-256
  `400f635ea29ac52dc5999caa866b152fa14cea1b82381ce91270012821a853ca`, (c) the recomputed
  `n_primary`, ISO weeks and the full `reasons` table equal that report, (d) the report has
  no stop code and no stop, (e) the cohort is 228 episodes in 48 weeks, and (f) **the actual
  inputs are fingerprinted**: a SHA-256 over the sorted map path to file SHA-256 of
  `phase0.json`, `manifest.json`, `go2_done.json` and every attempt and receipt of the root
  (a stop file is refused), taken before and after the derivation (it must not change). Counts
  that match an aggregate report do not prove equal inputs, so the owner approves the **cohort
  SHA-256** printed by the `cohort` command and `fetch` refuses any other (`--expected-cohort-sha256`);
  the fingerprint is stored in the sealed cohort file, exported in the report and re-checked
  against the root at report time. Limit: no committed hash covers these files, so the
  fingerprint's first value is trusted at the `cohort` step; the earlier `input_hash_map_sha256`
  (`cc20f7d5...`) has no committed file list to reproduce it from. Any difference stops before
  the first request.
- **Verified by code versus documentary only.** Checked by the code: the manifest hash, the
  committed report hash, the recomputed counts and reasons, the source fingerprint, the cohort
  hash. **Documentary only, not checked here:** the M2(ii) v3 report (`38b569aa...`,
  `stage1_pass_partial`), its evidence map (`532d1014...`) and the Stage 1 input-map hash
  (`cc20f7d5...`); they are in the contract under `documentary_only`. The M2(ii) completion did
  not change the 228 episodes (registry, 2026-11-19), and the sealed files they cover are not
  in Git, so the code does not read them.
- **The week of an episode** is the ISO week of the catalog `launchTime`, the same assignment
  as the 48 weeks of Stage 1 (question Q8).

## 3. The estimand (exact)

All times UTC. `T0` is the first public trade (stage 1), `E = ceil_minute(T0 + 7 d)`
(protocol 5.2). Two grid points on the whole hour:

```text
A = ceil_hour(T0 + 1 d)        first whole hour at or after T0 + 1 day
B = floor_hour(E)              last whole hour at or before the entry minute
h = (B - A) / 1 hour           always 143 or 144 (shown in section 3.1)
C(t) = close of the 1-hour trade bar that starts at t - 1 hour
L  = ln( C(B) / C(A) )                  close-to-close log return over [A, B]
Z  = L * sqrt( 168 / h )                scaled to the 168 hours of a V1 hold
Y  = 10,000 * ( exp(Z) - 1 )            the planning variable, in bps
```

`Y` is the price return of a 7-day hold, in bps, of the pre-entry proxy window. A short's net is
about `-Y` plus funding and fees, so the dispersion of `Y` is the dispersion of one episode's
net **only as a planning assumption** (section 13: stops, gaps, funding, costs and liquidation
make the real net differ). The factor `168 / h` is the **variance** scale and `sqrt(168 / h)` its
**standard-deviation** scale, applied to the log return `L`. The protocol's "7/6 as a variance
scale" (7.4) is the case `h = 144`, where the variance factor is `7/6` and the SD factor is
`sqrt(7/6)`. The scaling is exact for the variance of `L` under independent increments; after
`exp(Z) - 1` it is **not** an exact linear scaling of the simple-return variance, which is why
`sd_bps` is computed on `Y` itself and `sd_log_7d` on `Z`.

### 3.1 The window arithmetic

`T0 + 24 h <= A < T0 + 25 h`, and `T0 + 167 h < B <= E < T0 + 168 h + 1 min`. Hence
`B - A > 142 h` and `B - A < 144 h + 1 min`; on the whole-hour grid `h` is 143 or 144. (A first
draft of the code carried 142 to 144; the property test over 4,000 start times found that 142
is unreachable, and the contract carries 143 and 144.)

- The proxy window `[A, B]` lies inside `[T0 + 1 d, E]` and ends **at or before** `E`.
- The first bar read starts at `A - 1 h` (at least `T0 + 23 h`, after `T0`); the last bar read
  starts at `B - 1 h` and **ends at `B`, never after `E`**. If `E` is on the whole hour then
  `B = E` and the bar that starts at `E` is **not** requested (test:
  `test_an_entry_on_the_hour_never_reads_the_bar_that_starts_at_e`).
- Windows of different episodes overlap in calendar time and each overlaps the V2 hold; this
  exposure is registered (protocol 7.4).

## 4. What is read, parsed, discarded, stored

| Item                             | Rule                                                                                                                                                                                                                                                                         |
| -------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Request                          | `GET /v5/market/kline`, `category=linear`, `symbol`, `interval=60`, `start=A-1h`, `end=B-1h`, `limit=200`; one per episode. A legitimate page has 144 or 145 rows                                                                                                            |
| Parsed, every row                | the row length (at least 7) and column 1, the bar start time. A start outside `[A-1h, B-1h]`, off the hour grid or repeated **stops the run as a schema mismatch before any other field of that row is read**. A page of 200 rows, a wrong symbol or a bad envelope stops it |
| Parsed, two rows                 | only the rows that start at `A-1h` and `B-1h`: column 5, the close, which must be a positive decimal text (`\d+(\.\d+)?`, finite, above zero) else the run stops                                                                                                             |
| Discarded unparsed               | open, high, low, volume, turnover of every row, and the close of every other row                                                                                                                                                                                             |
| Reduced in memory                | the two closes become `L = ln(C(B)/C(A))` and are dropped; `L` is the only price-derived value that exists after the response is reduced                                                                                                                                     |
| Undefined proxy                  | one of the two endpoint bars absent, or the request unavailable after three sends. No imputation, no substitute bar. Counted in the 95% bar of section 7                                                                                                                     |
| Stored in the sealed root (0700) | per request: `log_return` (or null), row count and expected row count, plus the stage 1 receipt fields (attempt number, class, key, symbol, HTTP status, body byte count and SHA-256, time). Symbols stay in the sealed root only                                            |
| Stored in Git (aggregates only)  | the allowlisted report of section 10: no symbol, date, close, per-launch value, mean, sign, quantile, minimum, maximum or count of anything but episodes, weeks and requests                                                                                                 |
| Never stored, never printed      | the two closes, any other field of a row, any response body. The first successful response of the class also stores key paths and types, never values (stage 1 behavior)                                                                                                     |

## 5. The cuts: 2025-10-01, the blind window, the holdout

- **Cut at 2025-10-01.** The cohort contains only episodes whose V1 path ends by the cut
  (`X = E + 7 d <= 2025-10-01T00:00`, stage 1 `window_censored` removed 16). Every request
  ends at `B - 1 h <= E - 1 h <= cut - 7 d - 1 h`, so no request reaches the last 7 days of
  the year, let alone the cut. The code checks this for **every** episode before the first
  request (`check_episode`: the entry clock of protocol 5.2, `X <= cut`, the lookback of 30
  days from `E` inside 2024-10-01, `B <= cut - 168 h`, `B - A` in 143 or 144) and refuses
  the whole plan if one episode fails. The reducer additionally stops the run on any row that
  starts after `B - 1 h`.
- **HYP-012 v2 blind window from 2026-09-29.** Unreachable: the latest instant any request
  can end is 2025-09-23. No live ticker, no recent kline.
- **Backward holdout 2023-02-01 to 2024-09-15.** Not touched: the cohort comes from the
  discovery run, and no holdout metadata or price is read. No holdout fact (stage 1b has not
  produced one) entered any parameter below.
- **HYP-031 test year 2025-10-01 to 2026-09-28.** Not requested.

## 6. Statistics and the power gate

All parameters are in the contract and move its hash (test:
`test_every_pre_registered_rule_moves_the_hashes`).

### 6.1 Dispersion

With the defined episodes `i = 1..n` (those with a proxy):

```text
sd_bps   = sample standard deviation of Y_i                 (n - 1 in the denominator)
sd_log   = sample standard deviation of Z_i                 reported, gates nothing
top5     = share of the sum of squared deviations of Y from its five largest absolute deviations
```

`sd_bps` uses the simple return (not the log) because a short's net is linear in the simple
return (question Q5). It is the dispersion of a **terminal** return over a pre-entry window and
**not an upper bound** of the dispersion of the real net: the stop and liquidation rules of 5.3
act on the path (two paths with the same terminal return can have different stopped results),
and funding, dynamic costs, gaps and fills beyond the stop are not in it. It is a planning
scale, with its limits listed in section 13. The mean is formed in memory to take deviations
and is never emitted.

### 6.2 Dependence: moving-block bootstrap over ISO weeks

- The week sequence is the **contiguous** sequence of ISO weeks from the first to the last week
  that has a defined episode; interior weeks without an episode are kept as empty weeks. `m` is
  its length.
- Block lengths `b` = 2, 4, 8 weeks (protocol 7.2; 4 is the primary, none is chosen by its
  result). 10,000 draws. A draw concatenates `ceil(m / b)` blocks of `b` consecutive weeks,
  each with a start uniform on `0 .. m - b`, truncated to `m` weeks. Seed: a fixed integer,
  `25030001 * 100 + b`; sampler: Python `random.Random(...).randrange` (no new dependency).
- Two statistics per draw: the **cluster mean** (the mean of the weekly means over the non-empty
  drawn weeks; clusters weigh equally, as the stage 4 primary statistic does, protocol 7.2) and
  the **episode mean** (episodes weigh equally). Per block length the variance across draws of
  each is the output; the draws are never emitted.
- `DEFF(b) = Var_boot(cluster mean) / (sd_bps^2 / n)` (protocol 7.2). The numerator includes
  the effect of weighting clusters equally as well as the dependence, because the stage 4
  statistic weighs clusters equally; the same ratio for the episode mean is reported as
  `deff_episode_weighted` so a reader can see the split (question Q4).
- `DEFF_used = max( max over b of DEFF(b), 1.0 )`: the **largest** of the three block lengths,
  never the favorable one (protocol 7.4), and never below independence (question Q9).
  `n_eff = n / DEFF_used`. The maximum of three bootstrap estimates is a planning choice that
  avoids picking the favorable one; it is **not** a guaranteed upper confidence bound of the
  true design effect, which 48 weeks cannot bound.

### 6.3 The gate

```text
SE        = sqrt( DEFF_used * sd_bps^2 / n )
MDE_gate  = 2.80 * SE      = 2.80 * sd_bps * sqrt(DEFF_used) / sqrt(n)        (protocol 7.4)
gate      : MDE_gate <= 400 bps, compared unrounded
```

The multiplier `2.80 = 1.96 + 0.84` (one-sided 2.5%, 80% power) and the limit 400 bps are the
protocol's, fixed before any price. Two other multipliers are **reported and gate nothing**:
`2.487 = 1.645 + 0.842` (one-sided 5%, the convention of the HYP-037 calibration and the cost
and power planning notes) and `2.123 = 1.2816 + 0.8416` (one-sided 10%, the 90% lower bound
that stage 4 will use, protocol 8). Reporting them in advance is not a way to pick the favorable
one (question Q2).

Equivalent form: at `n = 228` the gate passes iff `sd_bps * sqrt(DEFF_used) <= 2,157` (arithmetic,
`400 * sqrt(228) / 2.80`). At `n = 217`, the smallest `n` that meets the 95% bar, it is 2,104.

| `n` defined | `DEFF_used` 1.0 | `DEFF_used` 1.5 | `DEFF_used` 2.5 | `DEFF_used` 4.0 |
| ----------: | --------------: | --------------: | --------------: | --------------: |
|         228 |           2,157 |           1,761 |           1,364 |           1,079 |
|         217 |           2,104 |           1,718 |           1,331 |           1,052 |

The cells are the largest `sd_bps` (bps) that still passes; they are arithmetic on the formula,
not measurements.

### 6.4 The V1, V2, V3 statement (does not gate)

`V1` decides (protocol 5.1). Its MDE is `MDE_gate`. For `V2` and `V3` the **same calibrated `SE`**
is scaled by `sqrt(n / n_V)`, where `n_V` is the number of defined V1-primary episodes that also
pass the variant's own cuts (path inside the year by the variant's exit; lookback of 30 days from
its entry inside 2024-10-01). `n_V` is therefore a **lower bound** of the variant's stage 4
count, because the variant's own cut can admit launches that V1's cut removed. Each variant is
reported with `episodes`, `weeks`, `floors_met` (at least 100 episodes **and** 26 ISO weeks) and
its MDE. A variant below a floor is reported and decides nothing; V2 and V3 never decide
(protocol 5.1). The V2 and V3 MDE is a **conditional extrapolation** of V1's calibrated `SE` (the
report labels it `conditional_extrapolation_from_v1_se`), not a measurement of those variants.
It also assumes the dispersion of days 3 to 10 and 14 to 21 is not above that of the days 1 to 7
read here; if post-entry dispersion is larger, the extrapolation understates (section 13).

### 6.5 The portfolio against the 5% hurdle (does not gate)

Slots `k` are computed **at plan time, from the cohort's entry and planned-exit times only**
(protocol 5.4): the smallest `k` such that at least 90% of the 228 episodes find a free slot, a
slot being released at `X` and an exit at an instant being processed before an entry at the same
instant. `entered` is the count that enter at that `k`. Both are metadata, are written to the
sealed cohort file before the first request, and are an input of stage 4's own contract (which
must state the same `k` or say why not). With `window_days = 365`:

```text
roc_annual            = mean_net_bps / 10,000 * entered / k          (protocol 6.4)
hurdle_required_bps   = 0.05 * 10,000 * k / entered                  (the mean that earns exactly 5%)
roc_annual_at_mde     = MDE_gate / 10,000 * entered / k
mde_over_hurdle       = MDE_gate / hurdle_required_bps
```

The report also gives `hurdle` and `at_mde` in dollars per year for `C` = 50, 250, 3,000 and
5,000 (protocol 6.4). Planning arithmetic only: at `k = 8`, `entered = 205`, the hurdle needs
about 19.5 bps (protocol 6.4 says about 20), and an effect equal to the gate limit of 400 bps is
`0.04 * 205 / 8 = 102.5%` a year on reserved capital, 20.5 times the hurdle's requirement. So the
400 bps is the pre-chosen **spending rule** (a ceiling of the allowed MDE), not the achieved MDE.
Detectability is stated through the **achieved** `MDE_gate` the report gives: for example, at
`sd_bps * sqrt(DEFF_used) = 600` and `n = 228` the achieved MDE is about 111 bps, so a 200 bps
effect would not be excluded, and the hurdle's 20 bps requirement can still lie far below it.
Whether the hurdle binds depends on the achieved MDE: it is below the statistical bar whenever
`mde_over_hurdle` is above 1, and the report gives that ratio. Nothing is declared unprovable
before the read. Question Q3 asks the colleague whether 400 bps is the intended spending rule.
If `k` or `entered` change after any outcome is read, that needs a pre-registered amendment
(question Q12).

## 7. Pre-registered decision table

First applicable row. The `Order` column is the **local** order within stage 3 (0 to 5); the global orders of protocol section 8 are different numbers for the same rows: `incomplete_coverage` is global order 2, `insufficient_events` global order 4 and `insufficient_power` global order 6, and the relative order of these three is the same in both. The contract's `order` field is the local one. Every number is unrounded. No row reads a
return.

| Local order | Verdict               | Condition                                                                                                              | Consequence                                                                                                                                                          |
| ----------- | --------------------- | ---------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 0           | `stage3_stopped`      | the run has a `stop.json` (HTTP 403, a fatal `retCode`, a schema mismatch, a cap, a transport chunk above the reserve) | counts only; no statistic; no verdict; a stopped root never continues; a corrected revision starts a fresh root                                                      |
| 0           | `stage3_incomplete`   | no `done.json`, or fewer than 228 finished requests                                                                    | counts only; no statistic; the same root may continue under the same binding only                                                                                    |
| 1           | `incomplete_coverage` | defined proxies below 95% of the 228 episodes (`defined * 100 < 95 * 228`, so 217 passes and 216 fails)                | stop code `incomplete_coverage`; counts only; I25 stops; no return is read                                                                                           |
| 2           | `insufficient_events` | fewer than 100 defined episodes, or fewer than 26 ISO weeks with a defined episode                                     | stop code `insufficient_events`; counts only; I25 stops; no return is read                                                                                           |
| 3           | `not_estimable`       | zero or non-finite variance, a draw without an episode, or a block longer than the week sequence                       | no MDE statement; stage 4 closed under this contract; no return is read; a new design needs its own registration                                                     |
| 4           | `insufficient_power`  | `MDE_gate > 400` bps                                                                                                   | stop code `insufficient_power`; **I25 stops before any return is read**; the aggregate report (all of section 10) is the record                                      |
| 5           | `power_gate_passed`   | `MDE_gate <= 400` bps                                                                                                  | stage 4 **may be proposed**: its own contract, request list, hash, code and registry row, then the owner's separate go for the read of returns. No effect is implied |

Local rows 1, 2 and 4 use the codes already named in protocol section 8 (global orders 2, 4 and 6); Amendment
7 adds stage 3 to the stage column of orders 2 and 4. `not_estimable` is the status the protocol
names in 7.4 for a calibration that cannot be computed, and it has no section-8 code. When a
verdict is one of rows 1 to 3 no dispersion, dependence or power number is emitted, so a cohort
that failed its floors does not also leak its variance. `returns_read` is always `false`.

## 8. No outcome is computed or inferable

- **Nothing after `E` is read.** The last bar read ends at `B <= E`; the reducer stops the run on
  any row that starts after `B - 1 h`, before reading any of its fields. The entry bar (the
  minute that starts at `E`), every exit bar, every bar of the hold week and every bar from the
  cut are outside every request.
- **No entry-to-exit price pair exists.** `P_e` is the open of the minute bar that starts at
  `E`, `P_x` the close of the bar that ends at `X`; neither is read. `C(B)` is a price of the hour
  before entry and `C(A)` of the hour before `T0 + 1 d`; both are dropped after one ratio is
  formed, so not even the price level of any episode is stored.
- **No mean, sign or per-launch value leaves the sealed root.** The report is built from an
  allowlist of keys (test: every key of every verdict's report is checked against forbidden
  substrings such as `mean`, `median`, `min`, `max`, `quantile`, `skew`, `sign`, `net`,
  `outcome`, `close`, `price`, `symbol`, and no 13-digit timestamp, no symbol and no closing
  price may appear in it). No raw second moment is emitted, so `mean^2 = E[Y^2] - Var` cannot be
  recovered; every dispersion function is invariant to a shift of the data (test:
  `test_every_dispersion_statistic_is_invariant_to_the_mean_level`).
- **Stated limit.** The proxy window is days 1 to 7; stage 4 reads days 7 to 14. A correlation
  between the two can exist (momentum or reversal), so the proxy's mean would not be a harmless
  number, which is why it is not emitted.

## 9. Request list, budget and behavior of a run

| Class | Name                 | Endpoint               | Requests | Response cap | Class cap | Go attempt ceiling | Go byte cap |
| ----- | -------------------- | ---------------------- | -------: | -----------: | --------: | -----------------: | ----------: |
| `P1`  | `proxy_hourly_trade` | `GET /v5/market/kline` |      228 |       64 KiB |    48 MiB |                684 |      48 MiB |

No other class: no catalog, archive, mark-price, funding, order-book or minute-bar request is in
this contract, and the code has no code path to one (test: the mock transport raises on any
request that is not the kline endpoint; the only `httpx.Client` is built in the `fetch` command).

- **Attempts.** Expected 228 (no retry). Ceiling `3 * 228 = 684`: every request may be sent at
  most three times (stage 1 `ATTEMPTS_PER_REQUEST`), so 684 is also the worst case, and the go
  cap equals it.
- **Bytes.** Expected about 3 MB (stage 1 D5 averaged 34.6 KB for up to 433 rows; this window
  holds 145). Worst case `684 * 64 KiB = 42.75 MiB` of response bodies; the go and class caps
  are 48 MiB, so the last of the 684 attempts still starts. A request starts only if
  `used + response cap + 64 KiB read reserve` fits both the go and the class cap; a declared
  length over the cap reads nothing; a transport chunk above the 64 KiB reserve stops the run.
- **Time at one request per second.** Expected 228 s (3.8 min). If every request needed all three
  sends, with the stage 1 backoff of 10 s and 30 s before the second and third send:
  `684 * 1 s + 228 * (10 s + 30 s) = 9,804 s` (2.7 hours). A `Retry-After` of up to 300 s
  replaces the backoff of that request. After an HTTP 429 or a rate-limit `retCode` the spacing
  doubles, up to 8 s.
- **Bybit error envelope, as already fixed in discovery.** An HTTP 200 body with `retCode`
  other than 0 stores only the integer `retCode` and `retMsg` cut to 200 characters. `10006` and
  `10018` are rate limits (retried; the spacing doubles), `10000` and `10016` are transient
  (retried), every other code stops the run and is named in `stop.json`. `retCode` must be an
  integer: a boolean, float, string or null is a schema mismatch and stops the run. Tests drive
  every code through the real fetcher (`test_retryable_ret_codes_are_retried_...`,
  `test_any_other_ret_code_stops_the_run_naming_it_...`,
  `test_a_boolean_or_float_ret_code_is_a_schema_mismatch_...`). Three failed sends of one
  request make its proxy undefined (it counts against the 95% bar), not a stop.
- **Persisted stop state.** A fatal event writes `stop.json` after its receipt; a crash between
  the two is recovered from the receipt. A root with a stop never continues, and the report of
  such a root is `stage3_stopped` with counts only.
- **Fresh root per contract, no resume under different code.** The root is created 0700, must lie
  outside the repository, and must be empty or already carry `run.json`. `run.json` holds the contract hash, the code revision
  and the SHA-256 of every published file (`PUBLISHED`, which includes this document, the
  contract, this module and the stage 1 modules it imports); a resume with any difference is
  refused. The cohort file is written before the first request and a root that holds another
  cohort is refused.
- **One go, not two.** Stage 1 needed two gos because its second go cost 8.5 GiB and depended on
  a reviewed manifest. Here the whole read is 228 requests and about 3 MB, the endpoint and
  response class are the ones Stage 1 already proved, and any surprise (a changed envelope, a
  row outside the range) stops the run fail-closed. A one-symbol pilot would prove nothing the
  stop rules do not (question Q6).
- **Run procedure, after the go and not now.** Merge, clean `main`, `plan`, `write-contract
--check`, then `cohort --stage1-root <sealed root>` (the owner approves the printed cohort SHA-256),
  `fetch --approved-sha256 <go_sha256["1"]> --expected-cohort-sha256 <cohort SHA-256>
--stage1-root <sealed root> --root <fresh 0700 root>`, then `report --root <root> --stage1-root
<sealed root>`; commit only the report. `report` refuses a root whose contract hash or code
  binding differs from the executing implementation or whose sources changed, and stores the first
  report of a finished or stopped root immutably (section 10). I run none of these.

## 10. The report: allowlist

Top-level keys: `version`, `verdict`, `stop_code`, `returns_read`, `cohort`, `window`,
`dispersion`, `dependence`, `power`, `variants`, `portfolio`, `reason`, `run`. `reason` is present
only for `not_estimable` and holds one fixed code. Inside them, only: episode,
week, slot and defined/undefined counts; the share of hour rows missing inside windows (a time
count, not a price) and the count of windows of 143 and 144 hours; `sd_bps`, `sd_log_7d`,
`top5_share_of_squared_deviations`; per block length the two bootstrap variances' ratios
(`deff_cluster_weighted`, `deff_episode_weighted`) and `cluster_weighted_variance`;
`deff_largest`, `deff_used`, `deff_floor_applied`, `n_eff`; `se_bps`, `multiplier`,
`mde_gate_bps`, `limit_bps`, `passes`, `sensitivity_bps`; the variant and portfolio tables of
6.4 and 6.5; and the run block (request-list hash, cohort SHA-256, attempts, bytes, finished
requests, results by outcome, error codes seen, the source fingerprint, and the binding: rules
version, code revision, publication hashes and whether the tree was clean at the run). The V2 and
V3 entries carry `basis: conditional_extrapolation_from_v1_se`. No cost model applies (no net is
formed).

## 11. Protocol change: proposed Amendment 7

Stage 3 departs from the protocol text in six places, each recorded as **Proposed Amendment 7**
in section 7.4 of the protocol (a paragraph, not a change of any rule that was fixed for stage
4), and the stage column of section 8 and the stage 3 row of section 10 are aligned:

1. **Hourly bars, not 1-minute bars.** Section 10 says "1-minute trade bars for the pre-entry
   proxy window, about 263 symbols x 1 request"; those two statements cannot both hold (six days
   of 1-minute bars are 8,640 rows, nine requests of 1,000 per symbol, about 2,050 requests and
   200 MB, and many more prices parsed). The dispersion of a 6-day return needs two prices per
   launch. Hourly bars give it in one request of 145 rows. Stage 4 still reads minute bars.
2. **228 requests, not 263.** The cohort is the stage 1 `n_primary` (section 2).
3. **A window on the hour grid inside `[T0 + 1 d, E]`.** The protocol's window is the
   continuous `[T0 + 1 d, E)`; the right bound is included in the sense that `B = E` when `E` is on
   the hour, and the last bar ends at `B` (no bar starting at or after `E` is read); the hourly grid makes it `[A, B]` with `h` 143 or 144 hours
   (section 3).
4. **Exact variance scaling.** The variance factor of a log return is `168 / h`, which replaces
   the flat `7/6` (equal at `h = 144`); the standard-deviation factor is `sqrt(168 / h)`, applied
   to `L` per episode. After the exponential there is no exact linear scaling of the
   simple-return variance.
5. **`DEFF` floor of 1.0 and a statistic named in advance** (cluster mean, section 6.2); the
   protocol said only "the largest of the block lengths".
6. **Stop-code reach.** The calibration bullet answers a defined-proxy share below 95%, fewer
   than 100 episodes or fewer than 26 weeks with `not_estimable`; the contract instead ends them
   in the codes section 8 already names, `incomplete_coverage` (order 2) and
   `insufficient_events` (order 4), which now also reach stage 3. `not_estimable` stays for a
   calibration that cannot be computed and has no code.

## 12. Registry row (mirror of the registry section)

- **Level: features, discovery year only (2024-10-01 to 2025-09-30), the 228 primary V1
  episodes, days 1 to 7 after each first trade.** What is read: 228 hourly trade-bar pages;
  parsed: bar start times and two closes per page, reduced to one log return per episode kept
  in a sealed root; published: aggregates only.
- **Not read:** any bar at or after any entry minute, any bar from 2025-10-01 or 2026-09-29, the
  backward holdout, mark prices, funding, order books, minute bars.
- **Discovery ledger count unchanged at stage 3:** no variant outcome is read; the three variants
  of protocol 5.1 are counted when stage 4 reads returns.
- **Overlap with seen windows, stated:** the discovery year's hourly trade klines of live and
  delisted USDT perps were read at features level by HYP-032 step 1, and 2026-11-11 exploratory
  reads (another agent) touched the same year at outcome level for other hypotheses. Whether each
  of these 228 launches' days 1 to 7 are among those reads is not verified here. A laboratory
  result on this year is a hypothesis (protocol 9), and stage 3 adds one more features-level
  read of closes of these coins.

## 13. Limits, stated plainly

- **A proxy, not the outcome.** The dispersion is measured on days 1 to 7; the hold is days 7 to 14. The transfer of `SD` and `DEFF` to the post-entry net is a **planning assumption, not an
  upper bound**. The risk is not that volatility fails to decay; it is that post-entry dispersion
  or dependence is larger than the pre-entry proxy, or that another assumption breaks. The
  168/h scale accounts for window length only.
- **What the proxy leaves out.** Stops and liquidation (they depend on the path, and two paths
  with the same terminal return give different stopped results; the protocol promises no loss
  ceiling), gaps and fills beyond the stop, funding, dynamic costs, and unresolved episodes. None
  of these is a guaranteed over- or understatement; their net effect on the real variance is
  unknown before stage 4.
- **Dependence transfers by assumption.** Pre-entry dependence (a common market factor across
  neighboring weeks) is taken to describe post-entry dependence. HYP-037's `DEFF` of 111 for one
  candidate shows how large that factor can be. 48 weeks give a noisy estimate of it; the largest
  of three block lengths is used for that reason.
- **Endpoint prices only.** Within-window path (a spike and a retrace) is invisible in
  `L`; it is also invisible in the net's price part except through the stop and liquidation
  rules, which stage 4 handles.
- **Already-seen coins.** Section 12: the exploratory series touched this year and possibly these
  coins at outcome level. Nothing here is a confirmation.
- **Hour-grid endpoints.** If a coin has no trade in the hour before `A` or before `B` the proxy
  is undefined (no imputation). Stage 1 showed zero missing hours inside `[E, X]` for all 228;
  days 1 to 7 are unverified until this read.

## 14. Open questions, each with my recommendation

| Q   | Question                                                                                                                                                                            | Recommendation                                                                                                                                                                                                                                                                   |
| --- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Q1  | Accept the six deviations of Amendment 7 (hourly bars, 228 requests, hour-grid window, exact scaling, floor and statistic named, stop-code reach)?                                  | Yes. The alternative, 1-minute bars, is about nine times the requests and parses far more prices to estimate one dispersion                                                                                                                                                      |
| Q2  | The protocol multiplier 2.80 gates; 2.487 and 2.123 are reported only. Should the gate use 2.123, the multiplier matching stage 4's 90% bound?                                      | Keep 2.80. It was fixed with the 400 bps limit before any price; changing it after a design discussion about its effect would be tuning, and the other two are in the report for the reader                                                                                      |
| Q3  | The 400 bps limit is about 100% a year on reserved capital and about 20 times the 5% hurdle's requirement (section 6.5). Is it the intended economic requirement?                   | Keep 400 bps only as the pre-chosen spending rule for the next read; the report states detectability through the achieved MDE (for example about 111 bps at an effective SD of 600 bps and n = 228), and the cheap stop is the protocol's declared outcome                       |
| Q4  | `DEFF` is for the cluster-weighted mean (the stage 4 statistic) and so includes the effect of equal cluster weights; the episode-weighted ratio is reported beside it. Which gates? | Cluster-weighted, as written in protocol 7.2 and 7.4                                                                                                                                                                                                                             |
| Q5  | `sd_bps` is the dispersion of the simple 7-day return, not of a stop-truncated net and not of the log return                                                                        | Simple return; the log version is reported. A short's net is linear in it; it is a planning scale, not an upper bound                                                                                                                                                            |
| Q6  | One go or two (a pilot first)?                                                                                                                                                      | One. 228 requests, about 3 MB, a class already proven by Stage 1, fail-closed stops                                                                                                                                                                                              |
| Q7  | V2 and V3 use V1's calibrated `SE` scaled by their own count, with `n_V` counted on V1-primary episodes (a lower bound). Instead read their own proxy windows (2 days and 13 days)? | No. They decide nothing and their MDE is labelled a conditional extrapolation; a second window per variant would add reads for statements nobody acts on                                                                                                                         |
| Q8  | Week of an episode: `launchTime` week (as Stage 1's 48 weeks), not the week of `T0`. A `T0` up to 24 hours after `launchTime` can fall in the next week                             | `launchTime`, for consistency with the stage 1 count and the protocol's "week of the launch". Blocks of 2 to 8 weeks make the boundary case likely immaterial                                                                                                                    |
| Q9  | `DEFF_used` is never below 1.0 (HYP-037 used a floor of 1.2). Floor at 1.0?                                                                                                         | Yes: a measured `DEFF` below one on a pre-entry window is not evidence that stage 4 returns are negatively dependent                                                                                                                                                             |
| Q10 | A calendar-day (market-wide day) clustering diagnostic beside the weeks?                                                                                                            | No. Windows of six days overlap across weeks; blocks of up to 8 weeks are the protocol's treatment of that                                                                                                                                                                       |
| Q11 | The `not_estimable` status has no stop code and, as designed, closes stage 4 under this contract. Instead give it a section-8 code?                                                 | Keep as is: it is a computation failure, not a finding about the market. The report now carries a fixed reason code (`zero_variance`, `nonfinite_variance`, `empty_draw`, `block_longer_than_weeks`, `nonfinite_bootstrap_variance`) and a new design needs its own registration |
| Q12 | Should stage 4's `k` be frozen from the value written here, or recomputed after stage 4's own cohort checks?                                                                        | Frozen from here if the cohort is unchanged; the stage 4 contract states it and any difference with its reason. A change of `k` after any outcome is read needs a pre-registered amendment                                                                                       |

## 15. Tests: regression table

89 tests, all synthetic (a real Stage 1 run against the synthetic Bybit of `new_perp_world`, and a
mock hourly kline server in the test file). Each row names what a failing test would mean.

| Area                | Tests (names abbreviated)                                                                                                                                                                                                                                                                                                                                                                                               | What would break                                                                      |
| ------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------- |
| Contract            | deterministic and checked in; every pre-registered rule moves both hashes; one go, one class, budget arithmetic; decision table order and thresholds                                                                                                                                                                                                                                                                    | a rule changed without a new hash; a budget number that disagrees with its derivation |
| Window              | 4,000 start times: A, B, `h` in 143 or 144, last bar ends by `E`; entry on the hour does not request the bar that starts at `E`; refusal of late, early, wrong-clock episodes; boundary at `X == cut`                                                                                                                                                                                                                   | a bar at or after `E` read; an episode priced although its path leaves the year       |
| Reducer             | only two closes parsed (garbage elsewhere); missing endpoint is undefined; a row at `E`, before the first bar, off the grid, a duplicate, 200 rows, a short row, wrong symbol, bad envelope, nine bad closes                                                                                                                                                                                                            | a post-entry bar parsed; a bad number accepted; a close or start time kept            |
| Cohort              | primary episodes only, censored launches absent; mismatch with the reviewed report, wrong manifest hash, wrong size, incomplete Stage 1 run all refused; repository evidence matches its pins                                                                                                                                                                                                                           | a cohort that differs from the reviewed 228 reaching the fetcher                      |
| Run                 | 228 requests on the kline endpoint, each ending before its entry and 7 days before the cut; no closing price in any sealed file; stored log return exact                                                                                                                                                                                                                                                                | a request outside the plan; a price persisted                                         |
| Error envelope      | every retryable code retried with only code and message stored (truncated to 200); other code stops, named, persisted, no retry; boolean, float, string, null `retCode` is a schema mismatch; three failed sends are undefined not a stop; a row after `E` stops the run unread                                                                                                                                         | the discovery fixes lost; a price or message body persisted                           |
| Root                | fresh root is 0700 and outside the repository, stray files refused; resume under another revision or contract refused, same code continues (229 sends after one crash); other cohort or size refused                                                                                                                                                                                                                    | resume under different code; a silent second cohort                                   |
| Report binding      | a root is refused by `evaluate` when the stored contract hash differs from the executing implementation (the changed-MDE-limit flip), when the stored code binding differs, or when the Stage 1 sources differ from the recorded fingerprint; the report exports revision, publication hashes and clean-tree state; the first report of a finished root is stored once and returned unchanged; a running root gets none | a root re-analysed by other code under its old hash                                   |
| Cohort inputs       | the fingerprint of the Stage 1 source files is stable, changes when one receipt moves by a second (same counts), is refused with a stop file, and the owner's expected cohort SHA-256 is enforced                                                                                                                                                                                                                       | counts that match while inputs differ                                                 |
| Reserve violation   | a receipt with `reserve_exceeded` and no stop file leaves the root stopped for `evaluate` and refuses further requests (Stage 3's own safeguard, plus the Stage 1 fetcher's own recovery from #630, each with a test)                                                                                                                                                                                                   | a transport-limit violation forgotten after a crash                                   |
| Statistics          | by-hand scaling; variance and top-5 share; `DEFF` near 1 for independent weeks and large for a common factor; largest block decides; MDE identity; floor at 1.0; degenerate variance; gate at exactly 400 and one ulp over; shift invariance; empty weeks kept                                                                                                                                                          | a wrong MDE, a favorable block chosen, a mean-dependent statistic                     |
| Decision table      | nine cases including 217 versus 216 of 228 and 100 versus 99 episodes, 26 versus 25 weeks, and the order of rows                                                                                                                                                                                                                                                                                                        | a boundary moved                                                                      |
| Variants, slots     | each variant's own cuts and floors; slot count against a plain reference and shuffled; hurdle arithmetic and dollars                                                                                                                                                                                                                                                                                                    | a wrong `k`, hurdle or variant count                                                  |
| Verdicts end to end | insufficient power, pass (recomputed independently), coverage 216 and 217, one-valued cohort, fatal code mid-run, partial run, tampered cohort file; the firewall (keys, symbols, closes, timestamps) on every report                                                                                                                                                                                                   | a statistic emitted from a failed cohort; a symbol, close or timestamp in a report    |
| Wiring              | the only HTTP client is built in `fetch`; `plan`, `--check` and the `cohort` summary (aggregates only) make no request; a wrong approved hash raises before anything is created; document, registry and protocol carry the hashes and numbers; no em dash                                                                                                                                                               | document drift; a request without the exact hash                                      |

## 16. Verification performed

Recorded in the pull request body after the commands are run: focused `pytest` of the new test
file, `ruff check` and `ruff format --check`, `mypy` (strict, the `make typecheck` surface),
`prettier --check` of the new Markdown and JSON, and `make verify`. No request to any exchange or
data service was made while writing this contract, and no price was read.
