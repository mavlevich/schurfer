# CCXT-013: verify the Bybit open-interest window contract

## Status

Research candidate. Do not open an upstream issue or pull request until the behavior
is reproduced against current CCXT `master`.

## Production evidence

Schurfer requested a fixed 12-hour, five-minute Bybit open-interest window with
`since` and `limit=200`. CCXT returned exactly the latest 200 rows available at fetch
time. Across several established markets, the first returned timestamp was the same
moving latest-page boundary even though the requested starts and instrument onboarding
dates were different.

The outer Bybit `fetchOpenInterestHistory` docstring says that `since` is not used.
The implementation nevertheless forwards `since` to
`fetchDerivativesOpenInterestHistory`, which maps it to `startTime`; it also maps the
unified `params.until` value to `endTime`. Schurfer now supplies both bounds to avoid
relying on the endpoint's latest-page default; production confirmation remains part of
the hotfix rollout.

This is not yet proof of a CCXT parser defect. It may be stale documentation, an
exchange rule when only one boundary is present, or a missing conformance test.

## Upstream research

1. Reproduce raw Bybit `/v5/market/open-interest` calls with:
   - only `startTime`;
   - `startTime` and `endTime`;
   - neither boundary.
2. Reproduce the same cases through current CCXT TypeScript
   `fetchOpenInterestHistory`.
3. Confirm whether cursor pagination preserves both time bounds.
4. Compare the TypeScript docstring and generated-language documentation with the
   observed unified contract.
5. If runtime behavior is already correct, submit only a focused documentation and
   static-response test change.
6. If `since` is genuinely ignored or lost, prepare a separate minimal adapter fix
   with a deterministic fixture.

## Acceptance criteria

- The meaning of unified `since` and `params.until` is documented accurately.
- A static response test pins the request fields and returned timestamp bounds.
- No Schurfer-specific retry or coverage policy enters the upstream adapter.
- Live evidence contains no production URLs, pump symbols, or private data.

## References

- [Bybit open-interest API](https://bybit-exchange.github.io/docs/v5/market/open-interest)
- [CCXT contributing guide](https://github.com/ccxt/ccxt/blob/master/CONTRIBUTING.md)
- [CCXT Bybit adapter](https://github.com/ccxt/ccxt/blob/master/ts/src/bybit.ts)
