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
- **Economic threshold:** the mean net bps per **opened** trade that also covers a
  monthly operating cost and a monthly target result, at USD 50 per position:
  `(cost + target) / (opened trades per month x 50) x 10,000`. Cost (0, 10, 25 USD) and
  target (0, 10, 50 USD) are not agreed, so every combination is shown and none is
  chosen. Opened trades follow the execution funnel in section 4. A trade whose outcome
  is later not recovered was still opened, occupied a slot and carried its economics; it
  is reported separately as "opened, outcome unknown", never removed before entry.

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
   as a time-dependence check, UTC days. 1,000 replicates per grid size; seeds derive
   from `20261001` and the dataset, scheme and size labels; the Monte Carlo SE is
   reported with every rate.

A grid size counts for a scheme only when its simulated cohorts hold at least 20
clusters on average; with fewer, the normal interval is anticonservative. Each scheme
therefore gives **bounds** `lower <= R <= upper` on its requirement R, not a point:

- `identified`: R lies within one grid step, between the last evaluable size below the
  target power and the first one reaching it;
- `censored`: smaller sizes were not evaluable and the first evaluable size already
  reaches the target, so only `R <= upper` is known (the floor of 100 is the lower bound);
- `not_reached` or `not_evaluable`: no upper bound within the grid.

A future cohort must satisfy every scheme, so the combined requirement has
`lower = max(scheme lowers)` and `upper = max(scheme uppers)`, unknown if any scheme has
no upper bound. A censored scheme still bounds the result: if assets need 401-500 and
days are only known to need at most 1,500, the combined requirement is 401-1,500, not 500. Every scheme's own bounds stay in the result (`required.by_scheme`).

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

### 4. Funnel and calendars

One funnel feeds both the calendars and the economics, in this order:

1. eligible events per day, counted before any pre-entry check (0, 0.5, 1, 1.84, 3, 5);
2. refusals and misses before entry (no route, timeout, book or qualification): 0 or
   20%, leaving the accepted events;
3. **research collection** (shadow or paper) is not capital-limited, so every accepted
   event is observed; its resolved share (100%, 90%, 70%) gives research outcomes;
4. **live execution** also loses accepted events that arrive while every slot is busy:
   with at most `c` open positions (1 or 3) and a 30 or 60 minute hold this is the Erlang
   B blocking of an M/G/c/c system with offered load `accepted rate x hold`. Every
   opened position occupies its slot whether or not its outcome is later recovered, so
   the resolved share applies only after the slots.

Two calendars follow, each at both bounds of the requirement: the research calendar
(steps 1-3) and the executable calendar (steps 1-4). Zero flow never completes. The
inverse gives the eligible flow needed to finish the research collection within 91 or
183 days.

## Run

| Item                   | Value                                                                                                                                                                    |
| ---------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Code revision          | `1bbdd27`, clean tree                                                                                                                                                    |
| Result                 | [`evidence/cost-power-planning-v1/result.json`](evidence/cost-power-planning-v1/result.json), SHA-256 `add61b989fc2dbc0c66f241a89d5bbdc900ca39e79ad23e5ce67262c9ae10e3b` |
| Parameters SHA-256     | `21c577ec3d5cd0f212bef6d6944b140352282cd3d58298cbb354f29f9d4c8d40`                                                                                                       |
| Generated tables       | [`evidence/cost-power-planning-v1/tables.md`](evidence/cost-power-planning-v1/tables.md)                                                                                 |
| Audit document SHA-256 | `57b9822fec254ff079f13a60e551ab54e6c6a665dee61e67a6a1c80a3da63dd9` (`accrual_reference` in the result)                                                                   |

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
because that dataset has about 73 events per day. Its day ICC is 0.003; if dependence
were purely within a day, a future flow of 1-2 events per day would carry almost none
of it. That is a model assumption the data cannot check, so it does not narrow the
reported bounds. Week-level dependence is not estimable from 3-4 weeks in any dataset.

### Method checks

At zero effect the largest simulated pass rate over the evaluable sizes is 2.7% to 4.4%
per dataset and scheme, against the nominal 2.5%, so the rule is close to its size. On the same simulated cohorts the registered
asset-cluster bootstrap gives the same decision in 95-100% of cases, and its pass rates
differ from the linearized rule by at most 1.5 points. The linearized rule is therefore
a usable stand-in for planning; a formal read still uses the registered bootstrap.

### Main table

Required resolved episodes for 80% power as combined bounds over both cluster schemes,
with the 90% bounds in the next column. The reference funnel is the audit's
illustrative 1.84 eligible events per day, 20% refused or missed before entry and 90%
of outcomes recovered; the executable calendar adds one open slot and a 60-minute hold
(5.8% slot loss). Every day and flow figure is given at both bounds. "Measured $" is
the net result of the resolved test trades at USD 50 if the effect is real.

| Net effect | Dispersion |  Episodes 80% |  Episodes 90% | Research days | Executable days | Eligible flow/day for 91 / 183 days |  Measured $ | Main limitation                                        |
| ---------: | ---------- | ------------: | ------------: | ------------: | --------------: | ----------------------------------- | ----------: | ------------------------------------------------------ |
|    100 bps | HYP-012b   |     100-1,500 |     126-1,500 |      76-1,133 |        80-1,202 | 1.5-22.9 / 0.8-11.4                 |      50-750 | asset scheme evaluable from 125, day scheme from 1,500 |
|    100 bps | HYP-012c   |       151-250 |       201-250 |       114-189 |         121-200 | 2.3-3.8 / 1.1-1.9                   |      76-125 | day scheme evaluable from 250                          |
|    100 bps | HYP-029    |   1,001-1,200 |   1,201-1,500 |       756-906 |         802-962 | 15.3-18.3 / 7.6-9.1                 |     500-600 | 81 legs, 46 assets                                     |
|     50 bps | HYP-012b   |     401-1,500 |     601-1,500 |     303-1,133 |       321-1,202 | 6.1-22.9 / 3.0-11.4                 |     100-375 | day scheme evaluable from 1,500                        |
|     50 bps | HYP-012c   |       601-800 |     801-1,000 |       454-604 |         482-641 | 9.2-12.2 / 4.6-6.1                  |     150-200 | needs more assets than observed                        |
|     50 bps | HYP-029    |   4,001-5,000 |   6,001-8,000 |   3,021-3,775 |     3,206-4,007 | 61-76 / 30-38                       | 1,000-1,250 | needs more assets than observed                        |
|     25 bps | HYP-012b   |   2,001-2,500 |   3,001-4,000 |   1,511-1,888 |     1,604-2,003 | 31-38 / 15-19                       |     250-312 | 3 weeks                                                |
|     25 bps | HYP-012c   |   2,501-3,000 |   3,001-4,000 |   1,888-2,265 |     2,004-2,404 | 38-46 / 19-23                       |     313-375 | needs more assets than observed                        |
|     25 bps | HYP-029    | 15,001-20,000 |       >20,000 | 11,327-15,101 |   12,021-16,027 | 229-305 / 114-152                   | 1,875-2,500 | beyond the grid at 90%                                 |
|     10 bps | HYP-012b   | 12,001-15,000 | 15,001-20,000 |  9,062-11,326 |    9,617-12,021 | 183-229 / 91-114                    |     600-750 | 3 weeks                                                |
|     10 bps | HYP-012c   | 15,001-20,000 |       >20,000 | 11,327-15,101 |   12,021-16,027 | 229-305 / 114-152                   |   750-1,000 | beyond the grid at 90%                                 |
|     10 bps | HYP-029    |       >20,000 |       >20,000 |       >15,102 |         >16,028 | >305 / >152                         |      >1,000 | beyond the grid                                        |

An interval with a lower bound of 100 is censored: no cohort smaller than the upper
bound had enough clusters to evaluate that scheme. Monte Carlo SE of the power at each
upper bound is at most 0.012. Each dataset also carries its 3-4 week limit.

### Economics at USD 50

With 20% refused or missed before entry, one open slot and a 60-minute hold, the
illustrative 1.84 eligible events per day open about **42.2 positions a month**. 38.0 of
them have a recovered outcome and 4.2 do not; those 4.2 still occupied the slot and
carried about USD 210 of notional a month with unknown economics. A real 50 bps net
effect earns about USD 10.5 a month on all opened positions, of which USD 9.5 is
measured. The mean net effect needed per **opened** trade:

| Monthly cost + target              | 1 event/day | 1.84/day |   3/day |   5/day |
| ---------------------------------- | ----------: | -------: | ------: | ------: |
| USD 0 + 0 (trading threshold only) |         > 0 |      > 0 |     > 0 |     > 0 |
| USD 10 + 0                         |      85 bps |   47 bps |  30 bps |  19 bps |
| USD 10 + 10                        |     170 bps |   95 bps |  60 bps |  38 bps |
| USD 25 + 50                        |     636 bps |  355 bps | 226 bps | 144 bps |

USD 500 and USD 5,000 are `capacity_not_measured`; no economic figure is given for them.

## Answer

All figures are conditional on the dispersion scenarios and the funnel above.

1. **Effect worth testing.** Below 50 bps net nothing is testable on any flow seen so
   far: 25 bps already needs 2,001-3,000 resolved episodes (about 15-46 eligible events
   per day for 3-6 months). 50 bps net, a mean gross move of roughly 90 bps on the
   common 5-20 bps spread books with the 15 bps quote-to-fill scenario, is the smallest
   grid effect that some plausible flow could test in 3-6 months. It is not established
   that 50 bps suffices: its requirement is 601-800 episodes under HYP-012c dispersion
   but only bounded to 401-1,500 under HYP-012b, whose same-day dependence cannot be
   sized below 1,500. Under HYP-029 dispersion even 100 bps needs 1,001-1,200.
2. **Flow that allows the test.** For research collection within 3-6 months at 20%
   pre-entry refusal and 90% resolution, 50 bps needs about **4.6-12.2** eligible
   events per day under HYP-012c and **3.0-22.9** under the HYP-012b bounds; 100 bps
   needs 1.1-3.8 under HYP-012c. Executable trades with one slot and a 60-minute hold
   take about 6% longer. The historical 1.84 per day, counted before target, book and
   qualification filters, fits only the 100 bps case under HYP-012c (114-189 research
   days); 50 bps would take 303-1,133 days. The v2 cohort's first 59 hours produced two
   source-eligible registered captures, far below 1.84 per day.
3. **Costs justified.** At USD 50 a real 50 bps edge returns about USD 100-375 of
   measured result over the whole test and about USD 10 a month afterwards. A new line
   is a validation expense, not a profit source: its incremental collection and
   operating cost should stay near zero until capacity above USD 50 is measured.
4. **The next collection stage is justified only if**, before any outcome is read, the
   candidate (a) states a mechanism for at least 50 bps net beyond the trading
   threshold, (b) shows from an outcome-blind funnel that its universe yields enough
   eligible events after its own pre-entry filters (about 4.6-6.1 per day
   for 50 bps over 6 months under the identified HYP-012c scenario, up to 11.4 under
   the HYP-012b upper bound) across several hundred assets, and (c) brings its own
   execution measurements (fills, latency, quote-to-fill, refusal and resolution rates)
   to replace the scenarios used here.

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
- the refusal, resolution and slot-loss rates of a real funnel (scenarios here);
- same-day dependence at a low event rate (HYP-012b sizes it only from 1,500 episodes);
- the operating budget and target monthly result, which are not agreed.

## Reproduce

```bash
make research-cost-power-planning-report ARGS="--artifact-root runtime/research --output-dir /tmp/cost-power"
```

The artifacts must sit under `runtime/research` with their `.sha256` sidecars, and the
audit document at its default path (or pass `--audit-doc`). A full
run takes about 4.5 minutes. A second run from a separate clean checkout of the same
revision produced a byte-identical `result.json`.
