# CCXT-009: Harden HTX derivatives-history limits

> Status: backlog; strong candidate, reproduce against current master
> Depends on: none
> Produces: one focused HTX pull request or a documented caller responsibility

## Goal

Determine whether CCXT should clamp or validate oversized unified `limit` values for
HTX linear funding-rate and liquidation history before sending the request.

## Production evidence

Schurfer's read-only derivatives probe ran against CCXT 4.5.68 on 2026-07-27:

- `limit=200` made both HTX methods fail with exchange error `1067`, reporting an
  invalid limit;
- `limit=100` returned three funding-rate rows and eight liquidation rows inside the
  same bounded episode window;
- the official HTX funding-history contract documents 100 as the maximum page size;
- the generated CCXT adapter currently forwards the supplied limit to the linear V5
  endpoints.

No credentials or private endpoints were involved.

## Current Schurfer containment

Schurfer applies a method-scoped request policy before calling CCXT:

- HTX `funding_rate_history`: effective page limit 100;
- HTX `liquidations`: effective page limit 100;
- every other venue/method pair: retain the caller's bounded limit.

The coverage report prints the effective limit, and the durable resolver stores it
with each run. Keep this policy while Schurfer pins CCXT 4.5.68 and until a released
upstream version has been verified in production.

## Questions to answer

- Does the official linear liquidation endpoint also document 100 as its maximum?
- Do inverse swaps and futures use different endpoints or limits?
- Does current CCXT master still forward an oversized limit unchanged?
- Do other HTX methods normally clamp with `min(limit, venueMaximum)` or reject
  locally with a clear `BadRequest`?
- Does CCXT's built-in paginated path already apply a smaller effective page size?

## Candidate implementation boundary

If current master reproduces the failure and both official contracts confirm the
limit:

- change only `ts/src/htx.ts`;
- cap the linear V5 request page size at the documented maximum, or follow the
  maintainers' established local-validation convention;
- keep the unified result and pagination semantics unchanged;
- add request and response coverage for funding history and liquidations;
- run the required HTX static tests and generated-language build.

Schurfer must keep its own request-policy override even if this is accepted upstream,
because production uses a pinned CCXT release.

## Acceptance criteria

- Oversized-limit behavior is reproduced on current master.
- Official limits are recorded for every changed endpoint and market subtype.
- A request for 200 no longer produces the opaque exchange error.
- Requests at and below 100 retain their existing behavior.
- No unrelated HTX parser or capability changes are included.

## References

- [CCXT HTX adapter](https://github.com/ccxt/ccxt/blob/master/ts/src/htx.ts)
- [CCXT unified pagination](https://github.com/ccxt/ccxt/wiki/Manual#pagination)
- [HTX funding-rate history](https://www.htx.com/en-us/opend/newApiPages/?id=8cb89359-77b5-11ed-9966-19b97ea5941)
- [HTX liquidation history](https://www.htx.com/en-us/opend/newApiPages/?id=8cb89359-77b5-11ed-9966-19b975edf5a)
