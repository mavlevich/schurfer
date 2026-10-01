# Cost and power planning v1

Status: **conditional planning calculation, not a forecast and not a registration.**
It registers no signal, reads no active cohort, and changes nothing in HYP-012 v2 or
HYP-015. It answers four planning questions before a new line collects data: what net
effect is worth testing, how many events and how much calendar time a test of that
effect needs, which costs that justifies, and which unknowns still block the decision.
The administrative stop rule is a separate PR.

## Inputs

The calculator (`schurfer_analytics.research_cost_power_planning`) runs offline on saved
artifacts only. No database, exchange or production query is made. Each file must match
its `.sha256` sidecar and the digest published in the discovery ledger or the pre-blind
readout, and each result must pin the inputs file it was computed from. A present but
corrupt file is refused; a missing file is reported as a missing measurement.

| Use        | Artifact                                  | Published in                                                                         |
| ---------- | ----------------------------------------- | ------------------------------------------------------------------------------------ |
| Costs      | `preblind-book-cost-baseline/result.json` | [pre-blind readout](preblind-book-cost-baseline-v1-readout.md)                       |
| Dispersion | `hyp012b/discovery/{inputs,result}.json`  | [discovery ledger](discovery-ledger.md), HYP-012b                                    |
| Dispersion | `hyp012c/holdout/{inputs,result}.json`    | discovery ledger, HYP-012c                                                           |
| Dispersion | `hyp029/{inputs,result}.json`             | discovery ledger, HYP-029                                                            |
| Flow       | the v2 accrual counters                   | [v2 identity and accrual audit](source-lead-v2-identity-accrual-audit-2026-10-01.md) |

The flow input is the audit's published row for the v4 candidate window: 42
source-eligible captures on registered Gate to Bybit routes over 22.83 days, about 1.84
per day **before** target sampling, book and qualification checks. The calculator checks
that the audit still carries that exact row and records the document's SHA-256. It is an
illustration of a past universe, not the frequency of a future one.

## Method

### 1. Trading break-even

The threshold per registered pre-blind group is the stored distribution of
`break_even_mid_move_bps` at fee scenarios of 0, 5.5 and 10 bps per side: the entry ask
VWAP impact and the exit bid VWAP impact, which already contain the half-spreads, plus
the fee on both sides. The spread only labels the bucket and is never added again
(tested). Paper entry/exit books and the source-lead same-book cross stay separate, as
do version, venue, quote freshness and spread bucket.

Covered by the observed quotes: half-spread and depth impact at USD 50 on both sides,
plus the fee scenario. Kept as additive scenarios because nothing measures them: funding
(0 or 5 bps per 8 h, prorated over a 30 or 60 minute hold) and a quote-to-fill
difference of 0, 15 or 30 bps per round trip (the v2 exit-slippage grid). Not measured
at all: fills, signal-to-order latency, adverse selection, maker non-fill, and any size
above USD 50. USD 500 and USD 5,000 are reported as `capacity_not_measured`.

### 2. Two thresholds

- **Trading threshold:** the gross move that covers entry, exit and the scenarios above.
  The effect grid below is **net of it**: a net effect of 25 bps means a mean gross move
  of the trading threshold plus 25 bps.
- **Economic threshold:** the mean net bps per executed trade that also covers a monthly
  operating cost and a monthly target result, at USD 50 per position:
  `(cost + target) / (entries per month x 50) x 10,000`. Cost (0, 10, 25 USD) and target
  (0, 10, 50 USD) are not agreed, so every combination is shown and none is chosen.
  Entries per month apply the resolved fraction, a rejection/miss fraction (0 or 20%)
  and concurrency loss: with at most `c` open positions, a signal arriving while all
  slots are busy is lost, which is the Erlang B blocking probability of an M/G/c/c
  system with offered load `rate x hold`.

### 3. Power

The grid was fixed in code before any simulation ran: net effects of 10, 25, 50 and 100
bps, power targets of 80% and 90%, and a test that passes when the mean is positive and
the lower bound of a two-sided 95% interval is above zero (one-sided 2.5%, the HYP-029
and v2 style). Sample sizes below the 100-resolved evidence floor are never reported as
sufficient.

Each historical dataset is replayed with that study's own evaluation code. The replay
must reproduce the published resolved counts and means to 1e-9 percent or the input is
refused. HYP-012b uses the five formal venues of its discovery read, pooled. HYP-012c
uses only the in-band candidates its formal read evaluated: every other holdout
candidate is skipped before any bar becomes a return (tested), so ISO weeks 36-39 stay
as unread as before. HYP-029 uses its 81 September legs. Returns are centered on their
own mean, which removes the level (and the constant per-trade cost) and keeps the shape,
tails and dependence. Each dataset is a separate **dispersion scenario**; none is the
distribution of a future signal.

Two calculations:

1. **Independent baseline:** `n = ((z_0.975 + z_power) x SD / effect)^2`, then inflated
   by the asset and UTC-day design effects. A repeat sensitivity uses
   `Deff = 1 + (m - 1) x ICC` for 1, 2, 4 and 8 episodes per asset.
2. **Simulation (primary):** whole clusters of centered returns are drawn with
   replacement until the cohort reaches the target size, the effect is added, and the
   test is applied. Clusters are assets (repeats and asset concentration preserved) and,
   as a time-dependence check, UTC days. The reported requirement is the larger of the
   two schemes. 1,000 replicates per grid size; seeds derive from `20261001` and the
   dataset, scheme and size labels; the Monte Carlo SE is reported with every rate.

The fast test uses the linearized cluster-robust (CR1) standard error, because the
registered `cluster_bootstrap_mean` is too slow to run inside every replicate. On the
same simulated cohorts, at the 100-episode floor and at the 50 bps / 80% size (capped at
1,200), the registered bootstrap is run as well (200 replicates, 1,000 iterations) at
zero and at 50 bps, and the agreement of the two decisions is reported. At zero effect
the pass rate must stay near 2.5%; a rate above 2.5% plus three Monte Carlo SE plus 0.5
points is flagged as anticonservative.

Reliability limits: fewer than 20 clusters in a scheme gives no simulated estimate for
it; fewer than 8 UTC weeks makes week-level dependence not estimable (every dataset here
has 3 or 4 weeks); a requirement that draws more distinct assets than the dataset has is
flagged, because the resampling then repeats observed assets as if they were new ones.

### 4. Calendar

Days to the required resolved episodes are `episodes / (events per day x resolved
fraction)` for flows of 0, 0.5, 1, 1.84, 3 and 5 per day and resolved fractions of 1.0,
0.9 and 0.7. Zero flow never completes. The inverse gives the event flow needed to
finish within 91 or 183 days.

## Run

| Item                   | Value                                                                                                                                                                    |
| ---------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Code revision          | `68fe658`, clean tree                                                                                                                                                    |
| Result                 | [`evidence/cost-power-planning-v1/result.json`](evidence/cost-power-planning-v1/result.json), SHA-256 `cf23468413ac9b846db139ea676fec78990d836b20b202123a3d686591215123` |
| Parameters SHA-256     | `21c577ec3d5cd0f212bef6d6944b140352282cd3d58298cbb354f29f9d4c8d40`                                                                                                       |
| Generated tables       | [`evidence/cost-power-planning-v1/tables.md`](evidence/cost-power-planning-v1/tables.md)                                                                                 |
| Audit document SHA-256 | recorded in the result (`accrual_reference.sha256`)                                                                                                                      |

All seven artifacts were verified against their sidecars and the published digests,
and every replay reproduced its published counts and means. The HYP-012b inputs digest
(`539894aa...`) is not in the ledger; it is accepted because the published discovery
result (`8a6e0865...`) pins it.

## Results

### Trading threshold

Fresh Bybit paper books at USD 50 need a mean midpoint move of about 16-17 bps with
spread below 5 bps, 23 bps with spread 5 to below 20 bps and 35-36 bps with spread 20
to below 50 bps, at 5.5 bps fee per side; the 10 bps fee scenario adds about 9 bps. The
p90 is 4-10 bps above the mean. Source-lead same-book crossings below 20 bps spread are
lower (15-23 bps at 5.5 bps) but are an immediate cross on a book of unknown age, not an observed exit.
Funding at 5 bps per 8 h adds at most 0.6 bps over 60 minutes. The unmeasured
quote-to-fill difference dominates the unobserved part: with the v2 scenario of 15 bps
the round trip is about **39 bps** for the common 5-20 bps spread bucket, and about
54 bps at 30 bps.

### Dispersion scenarios

| Dataset                           | Hold   | Episodes | SD of net, bps | Assets | Asset Deff | UTC days | Day Deff | Weeks |
| --------------------------------- | ------ | -------: | -------------: | -----: | ---------: | -------: | -------: | ----: |
| HYP-012b discovery, formal venues | 30 min |    1,528 |            400 |    258 |       0.94 |       21 |     1.20 |     3 |
| HYP-012c holdout, in band         | 30 min |      334 |            471 |    150 |       1.02 |       28 |     0.94 |     4 |
| HYP-029 September legs            | 60 min |       81 |          1,225 |     46 |       0.92 |       24 |     0.85 |     4 |

Repeats within an asset matter little at the observed ICC (0.08 and 0.04), but they
grow with collection length: at 8 episodes per asset the HYP-012b requirement rises by
59% and the HYP-012c one by 28%. HYP-012b shows same-day dependence (day Deff 1.20,
lag-1 autocorrelation 0.11) that the day simulation can only size from 1,500 episodes,
because that dataset has about 73 events per day. Week-level dependence is not
estimable from 3-4 weeks in any dataset.

### Method checks

At zero effect the largest simulated pass rate over the evaluable sizes is 2.7% to 4.4%
per dataset and scheme, against the nominal 2.5%, so the rule is close to its size. On the same simulated cohorts the registered
asset-cluster bootstrap gives the same decision in 95-100% of cases, and its pass rates
differ from the linearized rule by at most 1.5 points. The linearized rule is therefore
a usable stand-in for planning; a formal read still uses the registered bootstrap.

### Main table

Required resolved episodes for 80% / 90% power (the larger requirement over the
cluster schemes that identify it), the calendar at the audit's illustrative
1.84 events per day with 90% resolved, and the event flow needed to finish in about
3 or 6 months. "$ over the test" is the net result of all test trades at USD 50 if the
effect is real.

| Net effect | Dispersion | Episodes 80% / 90% | Days at 1.84/day | Flow/day for 91 / 183 days | $ over the test | Main limitation                 |
| ---------: | ---------- | ------------------ | ---------------: | -------------------------- | --------------: | ------------------------------- |
|    100 bps | HYP-012b   | <=125 / 150        |               76 | 1.5 / 0.8                  |              62 | 3 weeks; same-day dependence    |
|    100 bps | HYP-012c   | 200 / 250          |              121 | 2.4 / 1.2                  |             100 | 4 weeks                         |
|    100 bps | HYP-029    | 1,200 / 1,500      |              725 | 14.7 / 7.3                 |             600 | 81 legs, 46 assets              |
|     50 bps | HYP-012b   | 500 / 800          |              302 | 6.1 / 3.0                  |             125 | same-day dependence not sized   |
|     50 bps | HYP-012c   | 800 / 1,000        |              483 | 9.8 / 4.9                  |             200 | needs more assets than observed |
|     50 bps | HYP-029    | 5,000 / 8,000      |            3,020 | 61.1 / 30.4                |           1,250 | needs more assets than observed |
|     25 bps | HYP-012b   | 2,500 / 4,000      |            1,510 | 30.5 / 15.2                |             312 | day scheme binds                |
|     25 bps | HYP-012c   | 3,000 / 4,000      |            1,812 | 36.6 / 18.2                |             375 | needs more assets than observed |
|     25 bps | HYP-029    | 20,000 / >20,000   |           12,081 | 244 / 121                  |           2,500 | beyond the grid at 90%          |
|     10 bps | HYP-012b   | 15,000 / 20,000    |            9,061 | 183 / 91                   |             750 | day scheme binds                |
|     10 bps | HYP-012c   | 20,000 / >20,000   |           12,081 | 244 / 121                  |           1,000 | beyond the grid at 90%          |
|     10 bps | HYP-029    | >20,000            |              n/a | n/a                        |             n/a | beyond the grid                 |

`<=125` means both schemes first become evaluable at or above the requirement, so only
an upper bound is identified. Monte Carlo SE of each reported power is about 0.011.

### Economics at USD 50

With 90% resolved, 20% refused or missed, one open slot and a 60-minute hold, the
illustrative 1.84 events per day give about 38 executed trades a month. A real 50 bps
net effect then earns about USD 9.5 a month. The mean net effect needed per trade:

| Monthly cost + target              | 1 event/day | 1.84/day |   3/day |   5/day |
| ---------------------------------- | ----------: | -------: | ------: | ------: |
| USD 0 + 0 (trading threshold only) |         > 0 |      > 0 |     > 0 |     > 0 |
| USD 10 + 0                         |      94 bps |   52 bps |  33 bps |  21 bps |
| USD 10 + 10                        |     188 bps |  105 bps |  66 bps |  42 bps |
| USD 25 + 50                        |     705 bps |  393 bps | 249 bps | 157 bps |

USD 500 and USD 5,000 are `capacity_not_measured`; no economic figure is given for them.

## Answer

1. **Effect worth testing.** Only a net effect of about **50 bps or more per trade**,
   which means a mean gross move of roughly 90 bps on the common 5-20 bps spread books
   once the 15 bps quote-to-fill scenario is included, can be tested in 3-6 months with
   a plausible flow. Effects of 10-25 bps need 2,500-20,000 resolved episodes and are
   not testable on any flow seen so far. A line with HYP-029-like dispersion (60-minute
   pump legs) needs 100 bps or more.
2. **Flow that allows the test in 3-6 months.** With 30-minute source-lead-like
   dispersion and 90% of events resolving: about 1-2.5 eligible events per day for
   100 bps, and **3-10 per day** for 50 bps. The historical 1.84 per day, measured before target, book and
   qualification filters, supports only the 100 bps case. The v2 cohort's first 59
   hours produced two source-eligible registered captures, far below it.
3. **Costs justified.** At USD 50 a real 50 bps edge returns about USD 125-200 over the
   whole test and about USD 10 a month afterwards. So a new line is a validation
   expense, not a profit source: its incremental collection and operating cost should
   stay near zero until capacity above USD 50 is measured.
4. **The next collection stage is justified only if**, before any outcome is read, the
   candidate (a) states a mechanism for at least 50 bps net beyond the trading
   threshold, (b) shows from an outcome-blind funnel that its universe yields at least
   3 eligible events per day across at least several hundred assets, and (c) brings its
   own execution measurements (fills, latency, quote-to-fill) to replace the 15-30 bps
   scenarios.

## What still blocks a numerical decision

Each item is named in the result's `missing_measurements`:

- fills, signal-to-order latency and the quote-to-fill difference (only book quotes
  exist);
- adverse selection and maker non-fill;
- funding over the future holding period;
- impact and capacity above USD 50;
- the future universe's event frequency after its own filters;
- the future signal's own dispersion (the three datasets are scenarios);
- source-lead quote age (unknown for every source-lead group) and an observed
  source-lead exit;
- week-level dependence (3-4 weeks per dataset, at least 8 needed);
- the operating budget and target monthly result, which are not agreed.

## Reproduce

```bash
make research-cost-power-planning-report ARGS="--artifact-root runtime/research --output-dir /tmp/cost-power"
```

The artifacts must sit under `runtime/research` with their `.sha256` sidecars, and the
audit document at its default path (or pass `--audit-doc`). A full
run takes about 4.5 minutes. A second run from a separate clean checkout of the same
revision produced a byte-identical `result.json`.
