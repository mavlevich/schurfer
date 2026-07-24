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

| Task                                                  | Outcome                                                                 | Dependency                         |
| ----------------------------------------------------- | ----------------------------------------------------------------------- | ---------------------------------- |
| [CCXT-001](001-xt-fetch-open-interest.md)             | Research, implement, test, and submit XT `fetchOpenInterest`            | None                               |
| [CCXT-002](002-adopt-upstream-xt.md)                  | Upgrade Schurfer and remove the local fallback safely                   | Released CCXT version              |
| [CCXT-003](003-lbank-perpetual-ohlcv-research.md)     | Decide whether a supported LBank perpetual OHLCV contribution is viable | Independent; separate PR if viable |
| [CCXT-004](004-lbank-swap-ticker-timestamp.md)        | Normalize LBank swap `lastTime` into the unified ticker timestamp       | Completed and merged upstream      |
| [CCXT-005](005-apple-silicon-development-image.md)    | Restore the development Docker image on Apple Silicon                   | Issue confirmed; focused PR ready  |
| [CCXT-006](006-docker-image-optimization-research.md) | Measure Docker image size and build time before proposing improvements  | After CCXT-005; research first     |

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

## Primary references

- [CCXT contributing guide](https://github.com/ccxt/ccxt/blob/master/CONTRIBUTING.md)
- [CCXT XT adapter](https://github.com/ccxt/ccxt/blob/master/ts/src/xt.ts)
- [CCXT LBank adapter](https://github.com/ccxt/ccxt/blob/master/ts/src/lbank.ts)
- [CCXT open-interest contract](https://github.com/ccxt/ccxt/wiki/Manual#open-interest)
- [XT API documentation](https://doc.xt.com/)
- [LBank spot API documentation](https://www.lbank.com/docs/index.html)
- [LBank contract API documentation](https://www.lbank.com/en-US/docs/contract.html)
