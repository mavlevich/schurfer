# CCXT-010: Research HTX index-OHLCV capability by market subtype

> Status: backlog; reproduction required
> Depends on: none
> Produces: a capability/routing fix, documentation clarification, or a no-go

## Goal

Explain why CCXT advertises `fetchIndexOHLCV` for HTX while a linear USDT swap fails
with `htx swap has no api endpoint for index kline data`.

## Production evidence

The Schurfer derivatives probe used the exact recorded AKE linear-swap symbol on CCXT
4.5.68. Mark and premium-index OHLCV returned complete 5-minute windows, while
index-OHLCV failed before any rows were returned.

## Questions to answer

- Which HTX market subtypes actually expose index candles: linear swap, inverse swap,
  delivery futures, or only a subset?
- Is the global `has.fetchIndexOHLCV=true` correct because at least one subtype works?
- Can the unified method route linear swaps to an official endpoint that CCXT does
  not currently declare?
- If support is subtype-specific, can CCXT express that without regressing supported
  markets?
- Would a clearer local `NotSupported` message be the only safe improvement?

## No-go rule

Do not set the global capability to false if an officially supported HTX derivatives
subtype works. Do not synthesize index candles from mark or premium candles inside
CCXT.

## Acceptance criteria

- Current-master behavior is checked on linear and inverse swaps.
- Official HTX endpoints are mapped by subtype.
- Any capability or routing change has a static fixture for every affected subtype.
- A no-go records why the global capability cannot express per-market support.

## References

- [CCXT HTX adapter](https://github.com/ccxt/ccxt/blob/master/ts/src/htx.ts)
- [CCXT OHLCV contract](https://github.com/ccxt/ccxt/wiki/Manual#ohlcv-candlestick-charts)
