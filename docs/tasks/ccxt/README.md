# CCXT upstream contribution plan

> Status: planned. This is a non-blocking maintenance workstream. It must not delay
> Schurfer's measurement, outcome replay, or production reliability work.

## Why this exists

Schurfer currently carries exchange-specific compatibility code where CCXT exposes a
raw endpoint but does not implement the corresponding unified method. The first known
case is XT open interest: CCXT already declares XT's public contract open-interest
endpoint, but advertises `fetchOpenInterest: false`. Schurfer therefore calls the raw
endpoint and normalizes the response locally.

That fallback is tested and safe, but the fix belongs upstream:

- every CCXT user gets the unified method;
- generated Python, PHP, C#, Java, Go, and JavaScript clients stay consistent;
- Schurfer can eventually delete exchange-specific code;
- the implementation is maintained next to the exchange adapter and its API surface.

One file represents one independently completable task. Research, implementation,
tests, and review for the XT upstream change stay together because they produce one
pull request. Adoption of the released version and LBank research remain separate
because they have different dependencies and outcomes.

## Execution order

| Task                                                     | Outcome                                                                 | Dependency                         |
| -------------------------------------------------------- | ----------------------------------------------------------------------- | ---------------------------------- |
| [CCXT-001](001-xt-fetch-open-interest.md)                | Research, implement, test, and submit XT `fetchOpenInterest`            | None                               |
| [CCXT-002](002-adopt-upstream-xt.md)                     | Upgrade Schurfer and remove the local fallback safely                   | Released CCXT version              |
| [CCXT-003](003-lbank-perpetual-ohlcv-research.md)        | Decide whether a supported LBank perpetual OHLCV contribution is viable | Parked; production gap confirmed   |
| [CCXT-004](004-lbank-swap-ticker-timestamp.md)           | Normalize LBank swap `lastTime` into the unified ticker timestamp       | Completed and merged upstream      |
| [CCXT-005](005-apple-silicon-development-image.md)       | Restore the development Docker image on Apple Silicon                   | Completed and merged upstream      |
| [CCXT-006](006-docker-image-optimization-research.md)    | Measure Docker image size and build time before proposing improvements  | Ready for baseline measurements    |
| [CCXT-007](007-dotnet-installer-hardening-research.md)   | Decide whether the .NET installer should be pinned or integrity-checked | Low priority; research first       |
| [CCXT-008](008-lbank-swap-trades-research.md)            | Research LBank swap `fetchTrades` invalid-pair failures                 | Independent; separate PR if viable |
| [CCXT-009](009-htx-derivatives-history-limits.md)        | Clamp or validate HTX derivatives-history page limits                   | Strong candidate; reproduce master |
| [CCXT-010](010-htx-index-ohlcv-capability.md)            | Check HTX index-OHLCV support by derivatives market subtype             | Research before capability change  |
| [CCXT-011](011-okx-long-short-history-window.md)         | Check whether OKX long/short history can honor historical windows       | Research exchange/API limitation   |
| [CCXT-012](012-derivatives-empty-history-conformance.md) | Classify symbol-specific empty derivatives histories across venues      | More targets required              |
| [CCXT-013](013-bybit-open-interest-window-contract.md)   | Verify Bybit OI `since`/`until` behavior and correct its unified docs   | Reproduce against current master   |

## Rules for all tasks

- Work in a separate `ccxt` checkout, not inside the Schurfer repository.
- Use a human-readable branch name without tool or assistant attribution.
- Do not put Schurfer-specific policy into a general-purpose CCXT parser.
- Never commit generated `/js`, `/python`, `/php`, `/cs`, `/java`, `/build`, or
  bundled artifacts unless CCXT's current contributing guide explicitly asks for a
  particular generated artifact.
- Keep XT and LBank in separate issues, branches, and pull requests.
- Use only public read-only endpoints for fixtures and live validation.
- Do not include API keys, cookies, browser signatures, production URLs, or Schurfer
  data in upstream reports.
- Re-read the current upstream guide before starting because repository commands and
  generated targets can change.
- Record promising upstream improvements here when Schurfer exposes them, but create
  an issue or pull request only after reproducing the problem against current
  `master`.
- Keep correctness fixes separate from performance or image-size work. Optimization
  claims require before-and-after measurements.

## Recorded no-go boundaries

- An exchange-reported ticker volume of zero remains zero in CCXT. Do not infer
  volume from order-book depth, trades, open interest, or a different endpoint.
- Schurfer owns presentation policy for unavailable or partial volume. It must not be
  embedded in a general-purpose CCXT parser.
- Same-symbol asset identity cannot be repaired with name matching. Propose an
  upstream identity change only when an official exchange field or endpoint provides
  stable evidence; otherwise Schurfer retains its explicit conflict handling.
- A floating .NET 9 patch release and an HTTPS-downloaded official installer are not
  automatically bugs. CCXT-007 requires an upstream policy and threat-model decision
  before code.
- A method returning no rows for one low-liquidity symbol is not automatically an
  adapter bug. Reproduce on several active markets and compare the raw official
  endpoint before changing a capability flag or parser.

## Primary references

- [CCXT contributing guide](https://github.com/ccxt/ccxt/blob/master/CONTRIBUTING.md)
- [CCXT XT adapter](https://github.com/ccxt/ccxt/blob/master/ts/src/xt.ts)
- [CCXT LBank adapter](https://github.com/ccxt/ccxt/blob/master/ts/src/lbank.ts)
- [CCXT open-interest contract](https://github.com/ccxt/ccxt/wiki/Manual#open-interest)
- [XT API documentation](https://doc.xt.com/)
- [LBank spot API documentation](https://www.lbank.com/docs/index.html)
- [LBank contract API documentation](https://www.lbank.com/en-US/docs/contract.html)
