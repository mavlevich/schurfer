# CCXT-001: Add XT `fetchOpenInterest` upstream

> Status: planned
> Depends on: none
> Produces: one atomic XT-only CCXT pull request with reproduction, implementation,
> static fixtures, and cross-language validation

## Goal

Add CCXT's unified `fetchOpenInterest(symbol, params)` implementation for XT contract
markets. The change belongs upstream because CCXT already knows the raw XT endpoint
but does not expose it through the unified API, forcing every consumer to implement
the same normalization independently.

This is one task and one upstream pull request. Research, implementation, tests, and
review are phases of the same deliverable rather than separate project tasks.

## Why this is a strong upstream candidate

As of 2026-07-23:

- CCXT's XT adapter advertises `fetchOpenInterest: false`.
- The adapter already declares
  `future/market/v1/public/contract/open-interest` in both its linear and inverse
  public API namespaces.
- Schurfer successfully calls the generated raw linear method with
  `{"symbol": market["id"]}`.
- The endpoint is public and does not require credentials.
- Production has persisted valid XT OI rows for `ON/USDT:USDT` and
  `BANK/USDT:USDT`.
- The missing method benefits every CCXT XT-derivatives user and requires no
  Schurfer-specific abstraction.

CCXT generates JavaScript, Python, PHP, C#, Java, and Go exchange clients from its
TypeScript source. Only the TypeScript adapter and permitted static fixtures should
be committed; generated language files must not be edited manually.

## Existing Schurfer evidence

The production fallback is implemented and tested in:

- [`apps/analytics/schurfer_analytics/oi.py`](../../../apps/analytics/schurfer_analytics/oi.py)
- [`apps/analytics/tests/test_oi.py`](../../../apps/analytics/tests/test_oi.py)

Observed successful response shape:

```json
{
  "returnCode": "0",
  "msgInfo": "success",
  "error": null,
  "result": {
    "symbol": "on_usdt",
    "openInterest": "102204200",
    "openInterestUsd": "16938265.9457",
    "time": "1800000000000"
  }
}
```

Schurfer currently:

- prefers native `fetch_open_interest` when CCXT advertises it;
- otherwise calls XT's raw public linear endpoint;
- maps `openInterest` to `openInterestAmount`;
- maps `openInterestUsd` to `openInterestValue`;
- reads `time` as a millisecond timestamp;
- validates response shape and exchange-level return code;
- rejects timestamps older than 15 minutes or more than 5 minutes in the future;
- stores the USD value in `app.oi_snapshots`.

This is evidence and a working reference, not source to copy verbatim. The
freshness/future-skew policy protects Schurfer's live dataset and does not belong in
a general-purpose CCXT response parser.

## Unknowns to resolve before coding

- Is `openInterest` expressed in contracts, base units, or another XT-specific unit?
- Must it be multiplied by `contractSize` to satisfy CCXT's
  `openInterestAmount` contract?
- Is `openInterestUsd` always a USD notional for USDT contracts?
- Is `time` always milliseconds?
- Does the inverse endpoint return the same keys and units?
- Does XT ever encode numeric fields as JSON numbers rather than strings?
- What error response is returned for:
  - a spot symbol;
  - an inactive derivative;
  - an unknown symbol;
  - a unified symbol sent instead of XT's market id?
- Should a response containing only amount or only USD value be accepted as a
  partially populated structure, following CCXT precedent?

Do not infer these answers from one production sample. Confirm them from official XT
documentation, multiple public calls, CCXT conventions, and—where necessary—the XT
UI or another documented XT endpoint.

## Repository setup

Work in a separate checkout next to Schurfer:

1. Fork `ccxt/ccxt` into the maintainer's GitHub account.
2. Clone the fork as a sibling directory, not inside Schurfer.
3. Configure:
   - `origin` as the fork;
   - `upstream` as `https://github.com/ccxt/ccxt.git`.
4. Fetch `upstream/master`.
5. Create a branch such as `feat/xt-fetch-open-interest`.
6. Configure CCXT's hooks:

   ```bash
   git config core.hooksPath .git-templates/hooks
   ```

7. Install dependencies or use CCXT's documented Docker environment.
8. Confirm `npm run build` succeeds before modifying the adapter.

Before implementation, search current CCXT issues and pull requests for duplicate XT
open-interest work.

## Reproduction and field verification

1. Confirm the current unified call is unsupported:

   ```text
   xt.fetchOpenInterest("BTC/USDT:USDT")
   ```

2. Call the raw endpoint with `market.id` for:
   - `BTC/USDT:USDT`;
   - `ETH/USDT:USDT`;
   - one low-liquidity linear perpetual;
   - one inverse perpetual, if XT currently lists one.
3. Compare amount/value against XT's documented semantics or UI.
4. Confirm timestamp precision and proximity to XT server time.
5. Capture sanitized successful and failure responses for static fixtures.
6. Complete this field-mapping table for the PR:

   | XT field          | Meaning/unit | CCXT field           | Conversion |
   | ----------------- | ------------ | -------------------- | ---------- |
   | `openInterest`    | Verify       | `openInterestAmount` | Verify     |
   | `openInterestUsd` | Verify       | `openInterestValue`  | Verify     |
   | `time`            | Verify       | `timestamp`          | Verify     |

No API key, cookie, production URL, or Schurfer data should appear in fixtures or the
upstream report.

## TypeScript implementation

Expected primary source:

```text
ts/src/xt.ts
```

Required behavior:

1. Change XT's `has.fetchOpenInterest` capability from `false` to `true`.
2. Add `fetchOpenInterest(symbol, params = {})`.
3. Add or reuse an XT open-interest parser.
4. Load markets before resolving the symbol.
5. Require a symbol, matching CCXT's unified method contract.
6. Resolve the market with `this.market(symbol)`.
7. Reject spot markets using the standard CCXT contract-market error pattern.
8. Send `market['id']`, never the unified symbol, to XT.
9. Choose linear or inverse public methods from market metadata.
10. Merge caller-provided `params` instead of discarding or mutating them.
11. Validate XT's exchange-level return code.
12. Use CCXT safe accessors and string-based numeric handling required by its
    transpiler conventions.
13. Return the unified structure:

    ```text
    info
    symbol
    openInterestAmount
    openInterestValue
    timestamp
    datetime
    ```

14. Leave genuinely unavailable fields undefined rather than inventing zero values.

### Implementation constraints

- Copy the flow and style of a current certified-exchange implementation.
- Make at most one HTTP request.
- Avoid closures, comprehensions, language-specific syntax, heavy ternaries, and
  unsupported array helpers in derived exchange code.
- Do not mutate `params`.
- Do not add Schurfer logging, database, staleness, or retry policy.
- Do not modify generated exchange clients manually.
- Do not add XT open-interest history, funding changes, or another exchange to the
  same PR.

## Static and generated-client tests

### Static request coverage

- linear perpetual selects XT's linear endpoint;
- request contains XT `market.id`;
- unified symbol is not sent directly;
- caller `params` survive request construction;
- inverse perpetual selects the inverse endpoint if verified;
- spot market fails before an HTTP request.

### Static response coverage

- successful string-encoded response;
- numeric-encoded response if observed or intentionally supported;
- amount and USD value mappings;
- timestamp and datetime;
- unified symbol;
- raw response retained as `info`;
- non-zero `returnCode`;
- malformed or missing result;
- absent numeric fields according to the mapping decision.

CCXT documents `cli.js ... --report` for request fixtures and
`cli.js ... --response` for response fixtures. Use the syntax from the current
contributing guide at implementation time.

### Expected validation commands

Re-check the upstream guide because commands can change. As of 2026-07-23:

```bash
npm run build
npm run test-base
node run-tests xt --js
node run-tests xt --js --python-async
```

Also perform one read-only live call for a liquid contract and one lower-liquidity
contract. Live values must not be used as deterministic assertions.

After building:

```bash
git status --short
git diff --check
```

Inspect every generated modification and stage only files allowed by the current
CCXT guide.

## Upstream pull request

An issue is optional. Open one first only if field units or inverse-market behavior
remain ambiguous and maintainer direction is required. Otherwise, a small
reproducible PR with fixtures is the better starting point.

Suggested title:

```text
feat(xt): add fetchOpenInterest
```

Suggested description:

```markdown
## Summary

Adds unified `fetchOpenInterest` support for XT contract markets.

## Changes

- enables the XT `fetchOpenInterest` capability
- routes contract symbols through XT's public open-interest endpoint
- maps XT amount, USD value, and timestamp fields to the unified structure
- adds static request and response coverage

## Field mapping

| XT field          | CCXT field           | Unit/conversion             |
| ----------------- | -------------------- | --------------------------- |
| `openInterest`    | `openInterestAmount` | FILL FROM VERIFIED EVIDENCE |
| `openInterestUsd` | `openInterestValue`  | FILL FROM VERIFIED EVIDENCE |
| `time`            | `timestamp`          | FILL FROM VERIFIED EVIDENCE |

## Verification

- `npm run build`
- `npm run test-base`
- `node run-tests xt --js`
- `node run-tests xt --js --python-async`

The endpoint is public and no credentials are required.
```

Replace every placeholder with measured facts before submission.

## Review and release tracking

- Rebase onto current `upstream/master` before opening the PR.
- Keep the PR XT-only and atomic.
- Answer questions with official documentation or sanitized fixtures.
- Re-run tests after non-trivial review changes.
- Accept a maintainer-provided implementation if it satisfies the unified contract;
  correctness matters more than authorship.
- Record:
  - PR URL;
  - merge commit;
  - released CCXT version containing the method;
  - behavior differences introduced during review.

Schurfer retains its fallback until a published release is validated. An upstream
review delay is not a product blocker.

## Definition of Done

- Current unsupported behavior is reproduced.
- Amount, value, timestamp, and inverse semantics are verified.
- XT advertises native `fetchOpenInterest`.
- Linear routing uses the correct raw endpoint and `market.id`.
- Inverse behavior is implemented or explicitly scoped out with evidence.
- Unified response matches the CCXT Manual.
- Static request and response regressions pass.
- CCXT build and targeted JavaScript/Python tests pass.
- Final PR contains only XT source and permitted fixtures.
- CI and maintainer review are resolved.
- Merge commit and first released version are recorded for
  [CCXT-002](002-adopt-upstream-xt.md).

## Out of scope

- XT open-interest history.
- Private/authenticated endpoints.
- Funding-rate changes.
- Schurfer-specific freshness policy.
- LBank or any other exchange.
- Removing Schurfer's fallback before a released version is verified.

## References

- [CCXT contributing guide](https://github.com/ccxt/ccxt/blob/master/CONTRIBUTING.md)
- [CCXT derived-exchange rules](https://github.com/ccxt/ccxt/blob/master/CONTRIBUTING.md#derived-exchange-classes)
- [CCXT market-id rules](https://github.com/ccxt/ccxt/blob/master/CONTRIBUTING.md#sending-market-ids)
- [CCXT static test guide](https://github.com/ccxt/ccxt/blob/master/CONTRIBUTING.md#offline-tests)
- [CCXT XT adapter](https://github.com/ccxt/ccxt/blob/master/ts/src/xt.ts)
- [CCXT open-interest structure](https://github.com/ccxt/ccxt/wiki/Manual#open-interest)
- [XT API documentation](https://doc.xt.com/)
