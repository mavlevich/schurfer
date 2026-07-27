# CCXT-012: Classify empty derivatives-history responses

> Status: backlog; more production targets required
> Depends on: additional exact-symbol pump episodes
> Produces: venue-specific fixes, capability corrections, or documented sparse data

## Goal

Separate legitimate symbol-specific absence of derivatives history from CCXT routing,
parameter, parser, or capability defects.

## Production evidence

The 2026-07-27 probe observed:

- Bybit long/short history returned an empty list for SAFE;
- Gate open-interest history and liquidation history returned empty lists for DIA;
- Bitget long/short history returned exchange code `40054` with `data=null` for SAFE.

One symbol is insufficient evidence for an upstream defect. These markets may lack
history because of listing age, activity, retention, contract type, or venue policy.

## Research procedure

1. Re-run the same methods on at least one major liquid swap and two observed
   low-cap pump symbols.
2. Compare CCXT unified requests with official public endpoint responses.
3. Verify symbol ids, contract subtype, period, since/until support, and retention.
4. Classify each result as supported, symbol-empty, window-limited, parser-invalid,
   routing-invalid, or capability-invalid.
5. Open separate exchange PRs only for independently reproducible adapter defects.

## Acceptance criteria

- Empty history is reproduced across enough markets to distinguish symbol behavior.
- No capability is disabled from one sparse market.
- Any upstream proposal is limited to one exchange and one root cause.
- Schurfer records absence as data quality, not as a zero-valued market signal.

## References

- [CCXT contributing guide](https://github.com/ccxt/ccxt/blob/master/CONTRIBUTING.md)
- [CCXT unified derivatives methods](https://github.com/ccxt/ccxt/wiki/Manual)
