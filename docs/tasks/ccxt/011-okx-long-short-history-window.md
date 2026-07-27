# CCXT-011: Research OKX long/short historical-window behavior

> Status: backlog; exchange limitation suspected
> Depends on: none
> Produces: a pagination/routing fix or a documented non-recoverable source

## Goal

Determine whether OKX long/short ratio history can honor a point-in-time `since`
window through the official API and CCXT unified method.

## Production evidence

The Schurfer v2 probe requested a completed 12-hour window around pump event 846.
CCXT returned 100 valid 5-minute rows, but none overlapped the requested window.
This is consistent with an endpoint returning only its most recent rolling page and
ignoring or not supporting the unified `since` cursor.

## Questions to answer

- Does the official OKX endpoint accept start/end timestamps, a cursor, or only a
  rolling recent window?
- Does CCXT translate unified `since` into the correct OKX parameter?
- Can older pages be reached without private credentials?
- Does behavior differ by ratio type, period, or contract family?
- Is a live collector the only reliable way to retain this signal?

## No-go rule

If the official endpoint only returns a recent rolling window, document the source as
non-recoverable and collect it live in Schurfer. Do not fabricate historical ratios
or label recent rows as coverage of an older window.

## Acceptance criteria

- Current-master behavior is reproduced on at least two liquid swaps.
- Raw request parameters and response timestamps are recorded without credentials.
- Any fix proves overlap with a historical bounded window.
- Otherwise the exchange retention limitation is documented explicitly.

## References

- [CCXT OKX adapter](https://github.com/ccxt/ccxt/blob/master/ts/src/okx.ts)
- [CCXT long/short ratio contract](https://github.com/ccxt/ccxt/wiki/Manual#long-short-ratio)
- [OKX API documentation](https://www.okx.com/docs-v5/en/)
