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

Baseline (P95 of max(funding_rate, 0), long-only conservative, per venue):

- Binance P95 = 2.27e-4 (~2.27 bps/settlement); P99 = 5.36e-4 (~5.36 bps).
- Bybit P95 = 2.05e-4 (~2.05 bps/settlement); P99 = 6.55e-4 (~6.55 bps).

Freeze rule: charge the MAX settlement boundaries crossable in the 720m hold. 8h funding
intervals -> a 720m window crosses at most 2 boundaries, so the primary conservative
funding = 2 x P95_venue (Binance ~4.5 bps, Bybit ~4.1 bps over the hold). P99 is the
mandatory sensitivity input. This unconditional per-venue P95 (~2.1-2.3 bps) is far below
the pump-anchored stress source (~7.7 bps), so a flat stress constant would understate EV;
the captured DB tables stay stress/diagnostic only. The primary calibration rows are
stored beside this summary rather than left in session scratch.
