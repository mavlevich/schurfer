# CCXT-008: Research LBank swap `fetchTrades`

> Status: backlog; reproduction required
> Depends on: none
> Produces: one focused LBank swap pull request, an upstream issue, or a documented
> no-go

## Goal

Determine why CCXT's unified `fetchTrades` rejects observed LBank perpetual symbols
as invalid trading pairs while other public swap endpoints accept the same markets.

This task is independent of the merged swap-ticker timestamp fix and the separate
historical OHLCV investigation.

## Observation that exposed the gap

During investigation of LBank perpetual ticker quality:

- `fetchOrderBook` worked for observed swap markets;
- the contract ticker endpoint returned those markets;
- `fetchTrades` returned `Invalid Trading Pair` for several swaps, including BTC;
- low-liquidity symbols sometimes reported zero ticker volume, but that separate
  exchange-reported value must not be changed in CCXT.

The exact failure must be reproduced against current `master` before opening an
issue. Historical observations are not enough because exchange routes and CCXT
market metadata can change.

## Questions to answer

- Does CCXT route LBank swap `fetchTrades` through a spot-only endpoint?
- Does the raw contract API expose a public unsigned recent-trades endpoint?
- What market id does that endpoint expect?
- Does LBank use different identifiers for linear contracts and spot pairs?
- Is the method capability advertised correctly for swap markets?
- Are timestamps, side, amount, price, cost, and trade ids defined well enough to
  satisfy CCXT's unified trade structure?
- Are pagination and limits documented?
- Does the endpoint work for BTC, one liquid altcoin, and one low-liquidity swap?

## Research procedure

1. Re-read current CCXT contribution and derived-code rules.
2. Search current LBank issues and pull requests for duplicate work.
3. Inspect `ts/src/lbank.ts`, its declared API namespaces, and `has` capabilities.
4. Verify the official LBank contract documentation and public endpoint without
   credentials.
5. Capture sanitized request and response fixtures for representative markets.
6. Compare raw contract trades with ticker and order-book timestamps.
7. Decide whether the defect is request routing, market-id normalization, response
   parsing, capability declaration, or unsupported exchange functionality.

## Candidate implementation boundary

If an official public endpoint exists:

- implement or correct the TypeScript source path only;
- use CCXT safe parsers and unified trade fields;
- add static request/response fixtures;
- cover spot and swap routing separately;
- run current LBank static and live tests plus the full required build.

Do not combine this work with:

- synthetic ticker volume;
- historical OHLCV;
- symbol-specific fallbacks;
- Schurfer presentation policy;
- the already merged `lastTime` timestamp normalization.

## No-go rule

If LBank provides no official, public, unsigned recent-trades endpoint for
perpetuals, or the response lacks enough stable fields for the unified contract,
document the limitation and keep Schurfer's scanner-derived observations. Do not
reverse-engineer private web endpoints for an upstream contribution.

## Acceptance criteria

- Current-master behavior is reproducible.
- The root cause is identified.
- Any proposed method uses an official public API.
- Unified trade units and timestamps are verified.
- Spot behavior has no regression.
- Static fixtures and targeted live tests cover the change.

## References

- [CCXT LBank adapter](https://github.com/ccxt/ccxt/blob/master/ts/src/lbank.ts)
- [CCXT trade structure](https://github.com/ccxt/ccxt/wiki/Manual#trades-executions-transactions)
- [LBank contract API documentation](https://www.lbank.com/en-US/docs/contract.html)
- [CCXT contributing guide](https://github.com/ccxt/ccxt/blob/master/CONTRIBUTING.md)
