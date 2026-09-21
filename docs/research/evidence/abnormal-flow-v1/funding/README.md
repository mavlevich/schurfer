# Funding snapshot (primary conservative proxy) — calibration window

Outcome-blind: funding RATES only, no forward price/PnL. Durable snapshot for the freeze
contract; `funding_snapshot.json` fixes source (CCXT fetchFundingRateHistory), version,
window, per-venue coverage, and a content hash of the raw settlements.

- Window: 2026-08-16 .. 2026-08-30 (the 14-day calibration slice), per venue.
- Universe: exact eligible universe from the calibration-slice bars (Bybit 517, Binance 525).
- Coverage: Binance 525/525; Bybit 508/517 (6 symbols not resolvable in CCXT markets +
  3 with no settlements: B3USDT, ONGUSDT, SKRUSDT and similar delisted/illiquid names).
- Total settlements: 75,810 (Binance 39,886; Bybit 35,924).
- Raw settlement rows are stored as `funding_settlements.json.gz` (75,810 rows,
  SHA-256 `66e694d35283fbff558d1084eb1c08333a12c040b9189a5cf1783bdeccd6b8aa`).
  The summary also pins the canonical logical-row `content_hash`, so both the
  compressed artifact and its decoded observations are integrity-checkable.

Baseline (P95 and P99 of sum(max(funding_rate, 0)) over a sliding 720m hold window):

- Binance P95 = ~6.43 bps / 720m; P99 = ~17.8 bps / 720m.
- Bybit P95 = ~5.90 bps / 720m; P99 = ~25.2 bps / 720m.

Freeze rule: charge the cadence-aware P95 per-venue funding sum over a 720m hold window.
Instead of multiplying a flat rate by 2 (assuming 8h cadence), this model calculates the
exact sum of positive funding events that fall within every possible 720m window across
a continuous 60m grid. This correctly models mixed 8h, 4h, and 1h cadences found in the raw evidence.
Incomplete or unframed windows are excluded rather than zero-padded. P99 is the
mandatory sensitivity input. The primary calibration rows and reproducible inputs are
stored beside this summary.
