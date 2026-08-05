# Ideas: parked signal and feature catalog

> FROZEN. This is a backlog of unvalidated ideas, deliberately parked. Do not build
> any of it until the strategy's edge is proven on shadow data (see
> [ROADMAP.md](ROADMAP.md), Phase 0). Building more signal types on a strategy that
> may have no edge is building on sand. Moved out of ROADMAP.md on 2026-07-19 to keep
> the roadmap an executable plan rather than a wishlist.

When the freeze lifts, prioritize ideas that (a) are cheap to compute from data we
already store, and (b) directly move measured expectancy. Not the ones that are most
interesting to build. A cheap screen against an idea here is Discovery-level work and
does not need the freeze to lift — register it in
[docs/research/discovery-ledger.md](docs/research/discovery-ledger.md) whatever the
result. What the freeze blocks is building new production-facing signal
infrastructure before an existing lane clears Confirmation.

---

## Cross-market signals (CEX spot and DEX)

Catch pumps earlier by watching markets where they start before hitting perps.

- CEX spot scanner: Coinbase and Upbit for `/USDT` pumps as an early signal source.
- DEX scanner: DexScreener or GeckoTerminal API for Solana and EVM memecoins. This is
  a separate feed from the perp scanner and has a different risk profile (no perp
  hedge possible). Filters: min liquidity, age, security scan status. Signal: a
  spot or DEX pump, then find a correlated perp on Bybit or OKX to short.
- Hyperliquid perp support (DEX perps, different symbol format).
- UI: a "signal source" column showing where the pump started versus where to trade
  it.
- Correlation data: does an Upbit pump predict a Bybit perp retrace?

## Listing event signals

The listing pump pattern is announcement, spot pump, perp listing, second wave,
retrace. It is often stretched over 2 to 3 days, which gives more time to enter than
a regular pump.

- Monitor exchange listing announcement feeds (Binance, Bybit, OKX RSS or API).
- Flag new perp contracts the moment they appear (diff the current contract list
  against a cached one).
- Feed into the scanner with a "listing" tag on the signal.
- The delisting-short backtest itself is promoted to ROADMAP Phase 1. It has a known
  catalyst and clean historical data.
- Delisting-crash pattern (2026-08-05): after a delisting _announcement_, price looks
  volatile short-term, then tends to grind toward zero over roughly the following 7
  days as liquidity leaves and holders exit before the deadline — a candidate short,
  not a pump-reversion trade, so it needs its own selection/exit model rather than
  reusing the pump-short one. Magnitude plausibly depends on the delisting exchange's
  market-share/liquidity for that token — segment by exchange significance (major vs
  thin/secondary venue), not just delisting-yes/no, before pooling into one sample.
- Detection-method comparison, for both listing and delisting events: measure actual
  lead time before building either path. Diffing the exchange's own market list
  (cheap, near-100% precision, but only as fast as the exchange's own list update) versus
  scraping official announcement feeds (potentially earlier — some exchanges
  pre-announce ahead of the actual market-list change — but fragile and
  per-exchange-bespoke to maintain). Measure both against the same historical events
  before committing engineering effort to either.

## Token risk profile module

For each token, answer "how much leverage is safe and what is the real cost of
holding?". This sits between signal detection and execution. Much of it overlaps with
the post-shadow risk-engine work in the exit roadmap.

Inputs already available: `pump_event_snapshots` (+1h, +4h, +24h as an MAE proxy),
`funding_rate_snapshots` (carry cost), `pump_events.exchanges` JSONB (volume and
liquidity proxy), `retrace_pct` per episode (win rate and magnitude). Missing until
OHLCV storage exists: ATR and volatility, max intraday wick.

- MAE calculator per token per pump-magnitude bucket (30 to 50, 50 to 100, over
  100%), p50, p75, and p95 adverse excursion from peak.
- Funding-drag calculator: cost per day and per week at leverage N and funding R.
- Leverage suggestion: `max_safe_leverage = liquidation_buffer / MAE_p95`.
- Historical base rate: for this token at this magnitude, how often did it retrace
  more than X% within Y hours.
- Risk rating (Low, Medium, High, Extreme) from volatility, MAE, funding, and
  liquidity.
- Kelly criterion (after real PnL exists):
  `f = (win_rate * avg_win - loss_rate * avg_loss) / avg_win`.

## Advanced signals

- News pipeline: CryptoPanic and RSS, a cheap model pre-filter, then Claude scoring.
- Smart-money tracker for Solana (Helius).
- Known-fund and large-trader positioning tracker (public 13F-style disclosures,
  exchange leaderboards, on-chain fund wallets): surface what's currently in favor
  among visibly successful players, then decide via our own backtested metrics
  whether to mirror a similar strategy or allocation — not copy-trade blindly.
- Pre-launch short detector (TGE-aware, low-float VC tokens).
- MM history database (DWF, Wintermute patterns).
- Investigator-based signals (ZachXBT, MetaSleuth).

## Market microstructure

- Liquidation heatmap: OI by price level (Binance `openInterestHist`, Bybit
  risk-limit). A pump heading toward a short-liquidation cluster suggests
  continuation.
- Spot versus perp divergence: perp above spot by more than 0.5% during a pump means
  leveraged demand and no real buyers. Divergence closing means a retrace is
  imminent.
- Volume anomaly: current 1h volume versus the 30-day rolling average. 1x is weak,
  5x or more is a real event.
- Order-book imbalance: bid/ask volume ratio in the top N levels. Over 80% on the ask
  side during a pump means distribution.
- Thin-book flag: if moving price 2% needs less than $100K, it is easy to manipulate.
  Put it in a separate risk tier.
- Taker/maker ratio: a rising taker-buy ratio means real demand. Price rising on
  maker bids means painted tape.

## Macro timing

- BTC dominance shift: BTC.D falling means alt season, rising sharply means risk-off.
  Avoid shorting alts into a bull market.
- Bitcoin-versus-altcoin regime classifier: combine BTC.D trend, altcoin volume
  share, and breadth (percent of top-N alts outperforming BTC) into one regime
  label, if it turns out to add anything beyond BTC.D alone — needs a cheap
  Discovery-level check before building.
- Aggregate funding-rate index: average funding across the top 20 perps. Over 0.08%
  per 8h plus Fear and Greed over 75 means macro crowded-long, the best window to
  fade pumps.
- Regulatory calendar: SEC or CFTC hearings, ETF dates, major unlocks. These create
  predictable volatility spikes.

## Overvaluation screening

- FDV to market-cap ratio: over 20x means most supply is not circulating and future
  dilution will suppress price.
- TVL efficiency (DeFi): TVL to market cap. Over 1:50 means severely overvalued
  relative to usage.
- Token velocity: volume to market cap per day. Context for pump significance.

## On-chain analytics

- Holder concentration (Gini): top-10 wallet percentage (Etherscan, Solscan). Over
  50% in a single address means a "whale trap".
- Token unlock and vesting calendar: a pump two weeks before a major unlock is likely
  an exit-liquidity setup, so a high-confidence short.
- Wallet clustering: group addresses funded by the same source transaction. Real
  concentration is worse than what is visible.
- Bridge flows: targeted bridging into a thin-liquidity chain before a pump means a
  coordinated move.
- Smart-money identification: addresses that bought more than 5 days before past +50%
  moves start accumulating now is a pre-pump signal.
- Wash-trading detection: round-trip A to B to A patterns. Flag exchanges with
  artificial pump volume.

## Squeeze protection

- Short-squeeze scanner: funding deeply negative plus price rising means a squeeze is
  in progress. Suppress the short_setup verdict.
- Squeeze magnitude: short OI times funding times time-since-negative is a pressure
  gauge.

## OI coiled spring

- OI spike without a price move: OI up 15% while price moves less than 2% means a
  silent large position. Fire a "coiled spring" alert (uses `oi_snapshots`).
- Spring direction hint: an OI spike during a slow grind up means long accumulation.
  Sideways is ambiguous.

## Funding-rate arbitrage (basis trade)

- Basis-trade alert: funding over 0.3% per 8h (about 328% APR) means short perp plus
  long spot captures funding with zero directional risk. Show estimated daily yield
  and size.
- Basis-trade tracker: log threshold crossings and duration to build a dataset of
  chronic crowded-long tokens (recurring short setups).

## Correlation break detector

- BTC-relative move: token percent versus BTC percent over 1h. If BTC is flat (within
  0.5%) and the token is up 10% or more, it is an isolated pump. This fires earlier
  than the pump scanner because there is no threshold breach needed.
- Correlation score: rolling 24h correlation coefficient. A drop from over 0.8 to
  under 0.3 is a decorrelation event, meaning the token is being specifically
  targeted.

## General backlog (no phase)

- Tokenized assets (stocks and metals on Bybit or OKX). Separate scanner filter, same
  ccxt fetch.
- Real-time correlation matrix. Five open shorts all correlated 0.9 with BTC is one
  5x position, not five independent ones. Cap by correlation-adjusted notional.
- Multi-exchange capital management (Treasury module).
- Polymarket CLOB integration.
- Meme-stock or short-squeeze scanner (GME-type events via Alpaca or IBKR). Reuses
  all the pump-signal logic.
- Weighted social sentiment. Adjust by source influence, not raw tweet volume.
- Toxic-flow detection. Wallets or keys that consistently trade against market makers
  and win.
- Cross-exchange arbitrage gaps. Spot divergence over 0.3% beyond the normal basis is
  an information-asymmetry window.
