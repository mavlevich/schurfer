# CCXT-003: Research LBank perpetual OHLCV support

> Status: research required; no implementation commitment
> Depends on: none
> Produces: a go/no-go decision for a separate LBank upstream contribution

## Goal

Determine whether LBank exposes a stable, documented, public historical-kline
endpoint for perpetual contracts that can support a general-purpose CCXT
`fetchOHLCV` implementation.

This must remain separate from the XT open-interest contribution. It is a different
exchange, method, risk profile, and evidence set.

## Incident that exposed the gap

On 2026-07-23, Schurfer detected LBank-only pumps:

- `BRIAN`, which had both spot and perpetual markets;
- `OROCHI`, which appeared to have a perpetual market but no matching LBank spot
  market.

Both tokens were visible in the pump list, but the token chart was unavailable
because API Gateway did not support LBank OHLCV.

The Schurfer hotfix adds LBank's official public spot-kline endpoint. That should
cover BRIAN and other valid spot pairs, but it cannot cover a perpetual-only market
such as OROCHI.

## What we found

As of 2026-07-23:

- LBank's documented spot endpoint is `/v2/kline.do`.
- It expects a lower-case underscore symbol such as `brian_usdt`.
- The documented response rows contain:
  `[timestamp, open, high, low, close, volume]`.
- The endpoint works for BRIAN's LBank spot market.
- OROCHI did not have a corresponding LBank spot market in the observed CCXT market
  set, so the spot endpoint returns an invalid-pair response.
- CCXT's LBank `fetchOHLCV` path targets the spot kline API and therefore cannot
  provide historical candles for the observed perpetual-only OROCHI market.
- LBank's public contract documentation inspected during the incident did not expose
  a historical Kline REST method.
- LBank's website obtains futures chart data through frontend mechanisms that include
  undocumented endpoints or a binary WebSocket protocol.
- One discovered frontend REST path required dynamic signatures, headers, or browser
  state and rejected a plain server request.

These frontend mechanisms are evidence that data exists, not evidence of a supported
public API. They are not acceptable foundations for a CCXT contribution without
official documentation and stable public access.

## Questions to answer

- Is there a current official public perpetual historical-kline endpoint?
- Is it documented by LBank rather than inferred from minified frontend code?
- Does it work without login, cookies, private signatures, or browser headers?
- Does it support all active USDT perpetual market ids?
- What are:
  - interval identifiers;
  - maximum limit;
  - pagination semantics;
  - timestamp unit;
  - response order;
  - volume unit;
  - mark/index/last-price distinctions?
- Can historical candles be fetched for both BRIAN and OROCHI?
- Is the endpoint already declared in CCXT under another namespace?
- Is an existing CCXT issue or PR already tracking the gap?
- Does LBank explicitly permit this public API usage?

## Research procedure

1. Re-read current official LBank spot and contract documentation.
2. Search LBank's official API repositories and changelogs.
3. Inspect current CCXT LBank source, issues, and pull requests.
4. Ask LBank developer support for a public perpetual Kline endpoint if docs remain
   silent.
5. Test only documented candidate endpoints with:
   - a liquid perpetual;
   - BRIAN;
   - OROCHI.
6. Record sanitized request and response shapes.
7. Compare candle close with LBank's public ticker for the same market/time.
8. Confirm volume semantics and interval boundaries.
9. Make a decision:
   - **GO:** public, documented, stable endpoint exists;
   - **NO-GO:** only private/frontend/reverse-engineered access exists;
   - **WAIT:** LBank confirms a future public endpoint but it is not released.

## Acceptance criteria for a GO decision

- Official documentation names the endpoint and parameters.
- No authentication, browser session, or dynamic signature is required.
- BRIAN and OROCHI perpetual market ids both return valid historical candles.
- Candle timestamps, ordering, prices, and volumes can be normalized safely.
- Rate limits and pagination are known.
- A CCXT static request and response fixture can be created.
- The proposal benefits general LBank contract users and does not contain
  symbol-specific exceptions.

## NO-GO consequences

If no suitable endpoint exists:

- do not submit a fragile CCXT PR;
- do not rely on LBank's private website protocol in production;
- implement Schurfer's exchange-independent fallback:
  persist minute-level scanner price observations and aggregate them into OHLCV;
- label the source as scanner-derived and expose its shorter history/volume
  limitations;
- continue using official exchange OHLCV where available.

Scanner-derived history cannot reconstruct candles from before deployment, but it
will cover future perpetual-only pumps across any exchange lacking a usable public
history endpoint.

## References

- [CCXT LBank adapter](https://github.com/ccxt/ccxt/blob/master/ts/src/lbank.ts)
- [LBank spot API documentation](https://www.lbank.com/docs/index.html)
- [LBank contract API documentation](https://www.lbank.com/en-US/docs/contract.html)
- [CCXT contribution guide](https://github.com/ccxt/ccxt/blob/master/CONTRIBUTING.md)
