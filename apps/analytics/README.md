# Analytics

Python workspace package for pump scanning, point-in-time market measurements, and
strategy-agnostic forward outcomes.

Entrypoints:

- `pump-scanner` — scans supported exchanges and maintains pump episodes/snapshots.
- `outcome-resolver` — idempotently resolves forward price, MAE, and MFE windows and
  drains bounded derivatives-context recovery work without adding another service.
- `measurement-report` — read-only dataset-health and raw-outcome report. It aggregates
  in PostgreSQL and prints Markdown by default or JSON with `--format json`.
- `episode-replay` — read-only replay-input validator. It groups complete chronological
  decision paths by direct `pump_event_id`, fails closed on partial paths, reports
  exclusion/concentration diagnostics, and emits a code/data provenance manifest.
- `virtual-strategy-report` — replays the recorded pump-short v1 decision once per
  eligible episode over exact-anchor 5-minute OHLCV. It applies the production exit
  bands, conservative within-bar ordering, fixed fees/funding reserve, and the
  decision-time bid/ask impact snapshot, then emits Markdown or JSON.
- `virtual-entry-challenger-report` — compares the baseline with the pre-registered
  red-candle, 1.5% retrace, and combined entry-confirmation variants on identical
  eligible episodes. It waits at most 60 minutes, uses only fully closed candles, and
  treats an untriggered entry as a zero-return cash episode. Once its locked formal
  sample is ready, it also emits deterministic asset-cluster bootstrap inference,
  Holm-corrected paired tests, familywise bounds, and concentration sensitivity.
- `candle-anomaly-report` — derives the pre-registered HYP-005 blow-off concentration
  and reversal-strength features from fully closed exact-venue 5-minute candles, joins
  them to the locked baseline virtual replay, and emits descriptive 2x2 bucket results.
- `derivatives-context-report` — runs a bounded, read-only CCXT conformance and
  recoverability probe for funding, open interest, mark/index/premium candles,
  long/short ratios, and liquidations around recent pump episodes.

Run against the local development database:

```bash
make measurement-report
make measurement-report ARGS="--since 2026-07-22 --exchange-horizon 240"
make episode-replay
make episode-replay ARGS="--since 2026-07-22 --horizon 240 --horizon 480"
make virtual-strategy-report
make virtual-strategy-report ARGS="--since 2026-07-26 --format json"
make virtual-entry-challenger-report
make virtual-entry-challenger-report ARGS="--until 2026-07-28 --format json"
make candle-anomaly-report
make candle-anomaly-report ARGS="--until 2026-08-05 --format json"
make derivatives-context-report
make derivatives-context-report ARGS="--exchange binance --method funding_rate_history --format json"
```

The measurement report shows both decision count and distinct pump-episode count. Its
return and win-rate tables are descriptive: repeated decisions inside an episode are
correlated. The baseline virtual-strategy report is also descriptive. Formal inference
is confined to the pre-registered entry-challenger report and never changes production
configuration.

The replay command defaults to the pre-registered confirmation cohort and requires
exact anchor-venue 8-hour outcomes. `--allow-fallback` exists only for an explicitly
identified sensitivity run. The Make target records the current Git revision and
working-tree dirty state automatically.

Virtual entry is the next complete 5-minute bar open after a decision. This avoids
using a candle that was still forming at decision time. The report fingerprints the
downloaded path and retains it in JSON output, fails closed on missing bars or missing
liquidity-cost inputs, and assumes the adverse stop fires first when a 5-minute bar
cannot establish event order.
The default 10 bps taker fee per side and 5 bps per 8-hour funding cost are explicit
conservative model inputs and can be overridden for a sensitivity run.

The entry-challenger report defaults to the pre-registered cohort beginning
`2026-07-29T00:00:00Z`. At each possible entry it examines six 5-minute candles whose
close was already available before the entry bar, then enters at the following bar
open. Formal inference is withheld until the exact first 100 chronological eligible
episodes are completely resolved and contain at least 30 asset clusters. It then uses
10,000 deterministic whole-cluster bootstrap iterations, ordinary 95% expectancy
intervals, null-centered paired tests with Holm correction across the three variants,
conservative 98.333...% Bonferroni paired intervals, and top-five-cluster leave-one-out
sensitivity. Decision-time liquidity impact is held constant across variants because
the later historical order book cannot be reconstructed; live shadow measurement is
required before promotion. The replay also holds the baseline episode's eligibility
constant during the wait instead of reconstructing future score and market-quality
gates, so it isolates entry timing rather than claiming to mirror a deployable
strategy end to end.

The candle-anomaly report defaults to the prospective cohort beginning
`2026-07-29T00:00:00Z`. It derives only from candles that had fully closed by each
selected decision, uses one exact-venue fetch for both the 24-hour formation window
and the baseline exit replay, and fingerprints that path. Its blow-off and reversal
thresholds are locked in the research protocol. Results are descriptive: even a strong
bucket separation must be validated in a new out-of-sample live-shadow cohort before
it can affect scoring or entry eligibility.

The derivatives-context report selects at most one recent completed-window episode per
configured exchange. It loads the recorded exact unified symbol, never substitutes a
different venue or symbol, and reuses one rate-limited CCXT client per exchange. The
default request covers four hours before through eight hours after the episode anchor.
Declared CCXT support is not counted as evidence until the endpoint returns valid
millisecond timestamps inside that window; native and emulated capabilities remain
distinct in the output. Regular 5-minute series are paginated until the requested
window is complete or a bounded failure is visible; their coverage ratio, boundary
coverage, missing rows, duplicates, and maximum gap are reported. Event series such as
funding and liquidations remain sparse by definition and are not judged against a
fixed row count. Venue-specific request policies are explicit in the output (`htx`
open interest uses `1h`; HTX funding and liquidations cap pages at 100 rows). Either
side of the requested window is capped at seven days. The probe remains read-only and
does not affect execution.

The long-running outcome resolver uses the same fetch and coverage primitives to
persist only validated high-value methods: funding and OI on proven venues, Binance
long/short ratios, and HTX liquidations. Work begins after the complete eight-hour
window is available. `app.pump_derivatives_context_runs` records target identity,
request policy, coverage, retry state, CCXT version, and resolver version;
`app.pump_derivatives_context_samples` stores idempotent in-window public rows.
The resolver revalidates the loaded exchange market against the recorded market id
and identity key before fetching. Price-like mark/index/premium candles remain
on-demand inputs rather than duplicated storage. Resolver version
`derivatives_context_v2` explicitly bounds both sides of Binance long/short-ratio and
Bybit open-interest requests; this prevents their 200-row latest-page cap from
silently dropping the beginning of an older episode window.
