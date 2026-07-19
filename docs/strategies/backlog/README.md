# Strategy Backlog

Ideas under consideration. Not implemented yet.

Each entry has a rough priority and notes on what infrastructure it requires.
All strategies share the same journal, NATS bus, and web dashboard - they are
just additional collectors/analyzers/executors within the Schurfer monorepo.

---

## Priority tiers

| Tier     | Meaning                                     |
| -------- | ------------------------------------------- |
| **P1**   | Natural next step, minimal new infra        |
| **P2**   | Solid edge, requires some new data source   |
| **P3**   | Interesting, requires significant new infra |
| **Idea** | Worth revisiting later, no timeline         |

---

## P1 - Next after pump_short_v1

### `funding_rate_fade`

Short when funding is extremely positive (crowded long), long when extremely
negative. Same Bybit WebSocket data already collected for pump_short_v1.
Minimal new work - just a different signal filter in analytics.

**Edge:** Extreme funding = crowded trade = mean reversion tendency.
**Infra:** No new infra. Bybit funding data already in pipeline.
**Risk:** Can stay extreme longer than expected. Use stop.

---

### `token_unlock_fade`

Short tokens before large unlock events (>5% of supply). Unlocks are
scheduled and public - rare predictability in crypto.

**Signal source:** TokenUnlocks API, CryptoRank
**Edge:** Pre-unlock sell pressure is statistically consistent.
**Infra:** New collector: calendar poller (runs daily, not real-time).
**Risk:** Market can price it in early or ignore it entirely.

---

### `extreme_gainer_fade`

Short tokens that pumped 1,000-10,000%+ with no fundamental catalyst.
Screener checks top gainers on CoinGecko/CoinMarketCap daily.
Different from pump_short_v1 (which catches 50-100% pumps via OI/funding
in real-time) - this targets already-completed extreme moves.

**Signal source:** CoinGecko /coins/top_gainers or CMC gainers endpoint
**Edge:** Extreme pumps without fundamental = high reversion probability.
**Infra:** New collector: REST poller (daily/hourly). Low complexity.
**Risk:** Tokens can keep pumping. Position sizing critical.

---

## P2 - Solid edge, new data source

### `dump_long_v1`

Mirror of pump_short_v1. Long tokens that dumped 40-60%+ with capitulation
signs (volume spike, funding deeply negative, OI drop).

**Infra:** Same as pump_short_v1, opposite signal direction.

---

### `inter_exchange_funding_arb`

When funding rate for the same pair differs significantly between exchanges
(e.g. Bybit +0.05% vs OKX -0.01%) - long where you receive, short where
you pay. Delta-neutral, captures rate differential.

**Infra:** Multi-exchange collector (OKX, Bybit simultaneously).
**Capital:** Requires capital on both exchanges.
**Risk:** Low directional risk. Main risk: exchange-specific events.

---

### `listing_pump_fade`

New listing on Binance/Coinbase → pump on announcement → statistically
most new listings dump in the first 1-7 days.

Two variants:

- Fast: parse listing announcement page → buy spot before pump
- Slow: fade the pump 1-3 days after listing

**Infra:** Fast variant requires sub-second announcement parser (hard, crowded).
Slow variant is straightforward - just another signal.

---

### `pairs_trading`

Two correlated tokens (e.g. two L2s, SOL/AVAX). When spread exceeds N
standard deviations → long the laggard, short the leader.
Delta-neutral to market direction.

`setup_context` JSONB naturally stores z-score, correlation window,
spread at entry.

**Infra:** Historical data for correlation calculation. TimescaleDB already
handles time-series storage.

---

### `time_seasonality`

Statistical edge from timestamps already in the journal:

- Funding windows (every 8h)
- Weekend vs weekday behaviour
- Asian session vs US session

Not a standalone strategy - a filter/multiplier for other strategies.
Research task: query the journal after 3-6 months of data.

---

## P3 - Real edge, significant new infra

### `on_chain_insider_detection`

Track wallets that consistently buy tokens before exchange listing
announcements. Detect unusual inflows of fresh wallets into illiquid tokens.
Also: team/deployer wallet movements to exchanges (sell signal).

**Data sources:** Etherscan API, Arkham, Transpose, or self-hosted node.
**Infra:** On-chain indexer (new collector), wallet scoring system.
**Edge:** Genuine informational edge. Pattern documented by researchers.
**Note:** Goes in the same journal - just a different signal source.

---

### `stop_hunt_reversal`

Liquidity sweep below/above obvious levels (equal highs/lows) followed
by immediate rejection → entry in opposite direction. Formalises the
SFP (Swing Failure Pattern).

**Infra:** Tick-level data from Bybit. More granular than OHLCV.
**Complexity:** High. Pattern detection requires careful implementation.

---

### `polymarket_arb`

**Negative risk arb:** Sum of all outcomes on a market sometimes > 100%,
sell the basket for guaranteed profit. Rare but risk-free when it appears.

**Cross-platform arb:** Same outcome priced differently on Polymarket vs
Kalshi vs bookmakers.

**Resolution speed:** Market resolves after event but price lags the news
by minutes → buy 0.95 for something that already happened.

**Infra:** Polymarket API (Polygon chain), separate executor module.
**Note:** Different asset class but same journal + alert pattern applies.

---

### `volatility_selling` (Deribit)

Crypto IV is chronically higher than realised volatility. Systematic
strangle selling with risk management.

**Infra:** Deribit API integration, options pricing model.
**Risk:** Tail risk - one blow-up erases months of premium collected.
Requires strict position sizing and defined max loss rules.

---

## Ideas (no timeline)

### MEV / DeFi liquidations

- DEX arbitrage between Uniswap/Curve/etc., atomic transactions.
- Liquidation bots on Aave/Compound/Morpho (5-10% bonus).
- Backrunning after large swaps.

**Note:** Requires Go, Ethereum mempool access, gas optimisation.
Lives in the same monorepo as a separate set of services.

---

### `alt_lag`

BTC makes a sharp move, correlated alt hasn't moved yet → trade the
catch-up. Works less well as markets mature. Still alive on less liquid tokens.

---

### `index_rebalancing`

Known rebalancing dates for CMC indices / ETF baskets → buy what gets
added before the rebalance. Works in TradFi, partially alive in crypto.

---

### Data as a product

Byproduct of running collectors: clean funding rate history, liquidation
data, OI across exchanges in unified format. Researchers and quant funds
buy this (see: Coinalyze, Laevitas). Adding API access costs almost
nothing once the pipeline exists.

---

### Trade journal as a product

After the platform proves itself: self-hosted journal with perp-specific
analytics (funding costs, slippage tracking, strategy comparison).
Edgewonk/Tradezella charge $30+/mo but don't handle perpetuals well.
Sprint 6+ territory.

---

### Copy-trading on Bybit

Bybit has a built-in copy-trading mechanism - master trader earns a share
of follower profits. Only makes sense after 6+ months of verified track record.

---

## Stocks / traditional markets

Out of scope for Schurfer. Documented for personal reference.

- **IKE/IKZE (Poland)** - removes 19% capital gains tax. First step before
  any investing strategy.
- **EDGAR insider tracking** - SEC Form 4 filings are public. Cluster buys
  (multiple insiders buying simultaneously) statistically predict returns.
  Engineering task: EDGAR scraper → cluster scoring → alerts.
- **13F cloning** - copy concentrated long-only hedge funds with 45-day lag.
- **Spin-offs** - institutions mechanically sell spun-off entities →
  systematic undervaluation in first months (Greenblatt classic).
- **Wheel strategy** - sell cash-secured puts on stocks you want to own,
  then covered calls. Requires IBKR and capital for 100-share lots.
- **Factor strategies** - momentum, quality, small-cap value. Backtestable
  on free data. Schurfer backtest framework transfers with minor changes.
