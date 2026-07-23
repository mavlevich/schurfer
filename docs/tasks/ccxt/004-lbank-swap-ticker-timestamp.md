# CCXT-004: Normalize LBank swap ticker timestamps

> Status: research and upstream fix planned
> Depends on: none
> Produces: one focused CCXT LBank pull request, or a documented no-go result

## Goal

Preserve LBank's raw swap-ticker update time in CCXT's unified `timestamp` and
`datetime` fields, with the correct unit and without changing unrelated ticker
semantics.

This task is intentionally narrower than “fix LBank volume”. The observed zero
volume is present in LBank's own response and must not be replaced with invented
data inside CCXT.

## Incident that exposed the gap

On 2026-07-23 Schurfer sent:

```text
🔥 GME1 +63.2%
vol $0
lbank +63.2%
```

The LBank perpetual ticker was active and had a valid order book, but the unified
CCXT ticker contained:

- `timestamp: None`;
- `baseVolume: 0`;
- `quoteVolume: 0`.

Schurfer had previously interpreted missing or zero quote volume as a real USD
volume of zero. The local fix now preserves unavailable volume as `null`, labels its
provenance, and uses LBank's raw `lastTime` for scanner freshness checks.

## Sanitized evidence

The public LBank contract ticker response for the observed market contained the
following fields:

```json
{
  "symbol": "GME1USDT",
  "openPrice": "0.0001151",
  "lastPrice": "0.0001879",
  "highestPrice": "0",
  "lowestPrice": "0",
  "volume": "0",
  "turnover": "0",
  "lastTime": 1784790000
}
```

The exact value above is illustrative and sanitized; preserve the response shape,
not production event data, in upstream fixtures.

Additional validation during the incident:

- BRIAN, OROCHI, and BTC returned positive `volume` and `turnover` through the same
  endpoint;
- GME1 had a non-empty order book despite zero reported ticker volume;
- its last trade price was materially above the best ask, so the ticker itself
  should not be treated as execution-quality evidence;
- CCXT 4.5.58 maps swap `volume` to `baseVolume` and `turnover` to `quoteVolume`,
  but reads only the raw `timestamp` key for the unified timestamp;
- the observed swap response used `lastTime`, expressed as Unix seconds.

## What must be verified

- Confirm the current official LBank contract documentation defines `lastTime`.
- Capture fresh public responses for:
  - BTC;
  - one liquid altcoin;
  - one low-liquidity perpetual.
- Confirm whether `lastTime` is always Unix seconds and whether any LBank response
  variant returns milliseconds.
- Determine whether `lastTime` is the ticker calculation time, last-trade time, or
  server response time.
- Check current CCXT issues and pull requests for an existing LBank timestamp fix.
- Confirm spot ticker timestamp parsing remains unchanged.
- Verify all generated language bindings receive the unified timestamp through the
  normal CCXT transpilation workflow.

## Proposed implementation shape

Work in `ts/src/lbank.ts`, inside `parseTicker`:

1. Keep the existing `timestamp` field for spot responses.
2. For contract responses, fall back to `lastTime` only when `timestamp` is absent.
3. Convert Unix seconds to milliseconds exactly once using the established CCXT
   helper for second-based timestamps.
4. Pass the normalized value to both `timestamp` and `datetime`.
5. Do not infer volume from order-book depth, open interest, or a different endpoint.

The exact helper must be chosen after reading the current TypeScript base classes;
do not hand-roll multiplication if CCXT already provides a safe timestamp parser.

## Tests

Add static parser fixtures covering:

- swap response with second-based `lastTime`;
- spot response with millisecond `timestamp`;
- missing time remains `undefined`;
- malformed time does not produce a plausible date;
- volume and turnover parsing are unchanged, including explicit zero.

Run the current commands from CCXT's contributing guide plus the focused LBank
exchange tests. Do not commit generated language artifacts unless the guide requires
them.

## Acceptance criteria

- The unified LBank swap ticker exposes the expected millisecond timestamp.
- `datetime` matches that timestamp.
- Spot parsing has no regression.
- Explicit exchange-reported zero volume stays zero in CCXT.
- No symbol-specific logic or Schurfer-specific policy is introduced.
- A static request/response fixture proves the behavior.
- The pull request describes the field and unit, not the trading incident.

## Separate observation: swap trades

During diagnosis, CCXT `fetchTrades` returned `Invalid Trading Pair` for several
LBank swaps, including BTC, while public order books worked. This is not part of the
timestamp PR. Research it separately only if Schurfer needs trade-derived volume or
scanner-derived candles; combining both fixes would make upstream review harder.

## References

- [CCXT LBank adapter](https://github.com/ccxt/ccxt/blob/master/ts/src/lbank.ts)
- [CCXT ticker structure](https://github.com/ccxt/ccxt/wiki/Manual#price-tickers)
- [CCXT contributing guide](https://github.com/ccxt/ccxt/blob/master/CONTRIBUTING.md)
- [LBank contract API documentation](https://www.lbank.com/en-US/docs/contract.html)
