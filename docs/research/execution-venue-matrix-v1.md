# Execution venue matrix v1

Status: static repository and production-configuration audit complete;
owner/account capability confirmation open. Outcome-blind: no strategy returns,
forward-cohort outcomes, balances, positions, or private exchange endpoints were read.

As-of: 2026-09-15 UTC. Repository revision inspected:
`820d212f8341729e1fd8d0cd5f0c4b6523081a97`.

## Decision this matrix supports

This matrix separates four facts that must not be collapsed into "the exchange is
supported":

1. Schurfer can observe a public market;
2. Schurfer continuously retains the inputs needed by a research contract;
3. the execution service can construct an authenticated client;
4. the owner account is permitted and operationally ready to trade the exact product.

Only item 4 plus a safe strategy-specific live path can make a venue an execution
target. Public coverage, a CCXT class, environment-variable slots, or a historical
proxy never establish that fact.

This document authorizes no API-key change, private API call, order, live mode,
strategy, deployment, or change to an existing forward cohort.

## Evidence classes

- **Confirmed in repository:** traced to current code or versioned documentation.
- **Confirmed in production configuration:** checked without printing secret values.
- **Owner confirmation required:** account, KYC/jurisdiction, product permissions,
  funding and operational choices that code cannot establish.
- **Not execution-ready:** one or more mandatory facts or safety paths are absent.

## Current production boundary

Read-only inspection of the running `schurfer-execution` container on 2026-09-15
established:

- `AUTO_TRADE=false` and `DRY_RUN=true`;
- every configured Binance, Bybit, OKX, Gate, KuCoin, BingX and MEXC credential field
  is empty;
- per-strategy mode overrides are unset;
- the public scanner uses its default registry because `PUMP_EXCHANGES` is empty;
- Bybit and Binance momentum-capture services are running, which proves process
  presence only, not complete research coverage or trading access.

No private exchange endpoint was called. Therefore balances, account product access,
API-key scopes, order minima, margin mode and jurisdiction eligibility remain unknown.

The generic manual order endpoint and legacy order lifecycle exist, but the current
mode ceiling rejects entries and there are no authenticated production clients. The
new strategy `Broker` contract intentionally has no `LIVE_PROBE` or `LIVE_MICRO`
implementation. Consequently **no automated candidate currently has a safe,
authorized live execution route on any venue**.

## Venue matrix

| Venue / product                                                    | Public observation                                               | Continuous retained research baseline                                                                                      | Authenticated client constructor                      | Production credentials | Account/product eligibility                                                                                                      | Current decision role                                                                                              |
| ------------------------------------------------------------------ | ---------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------- | ---------------------- | -------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------ |
| Bybit linear USDT perpetual                                        | Default broad scanner; exact public client                       | Deep 1m momentum bars/trades, ticker/BBO-derived fields, OI and liquidation paths exist; quality remains contract-specific | Yes                                                   | Empty                  | Owner confirmation required                                                                                                      | Cheapest potential execution target because data and identity foundations already exist; still not execution-ready |
| Binance USD-M perpetual                                            | Default broad scanner; exact public client                       | Deep momentum capture, OI and censored liquidation paths exist with Binance-specific semantics                             | Yes                                                   | Empty                  | Internally declared unavailable for Poland residents; fresh owner/account confirmation required before relying on that statement | HYP-012 research target only today; cannot satisfy the money gate                                                  |
| Gate linear USDT perpetual                                         | Default broad scanner and source-lead capture                    | Source-lead observations exist; no equivalent full continuous deep baseline is established                                 | Yes                                                   | Empty                  | Owner confirmation required                                                                                                      | Discovery/source venue; trading on Gate itself is untested and not execution-ready                                 |
| MEXC linear USDT perpetual                                         | Default broad scanner and on-demand public market path           | No equivalent full continuous deep baseline is established                                                                 | Yes                                                   | Empty                  | Owner confirmation required                                                                                                      | Radar/source candidate only; no money-route claim                                                                  |
| OKX linear USDT perpetual                                          | Default broad scanner                                            | No current deep baseline established                                                                                       | Yes, passphrase required                              | Empty                  | Owner confirmation required                                                                                                      | Public radar only until a measured coverage need and account confirmation exist                                    |
| KuCoin futures                                                     | Default broad scanner                                            | No current deep baseline established                                                                                       | Yes, passphrase required                              | Empty                  | Owner confirmation required                                                                                                      | Public radar only                                                                                                  |
| BingX linear perpetual                                             | Default broad scanner                                            | No current deep baseline established                                                                                       | Yes                                                   | Empty                  | Owner confirmation required                                                                                                      | Public radar only                                                                                                  |
| Bitget, CoinEx, Phemex, Crypto.com, HTX, LBank, XT, Toobit, BloFin | Default broad scanner                                            | No current deep baseline established                                                                                       | No authenticated constructor in the execution service | Not applicable         | Not audited                                                                                                                      | Public radar only; adding execution requires a separate capability and safety decision                             |
| BitMart historical rows                                            | Removed from current default clients after upstream CCXT removal | Historical attribution remains                                                                                             | No                                                    | Not applicable         | Not audited                                                                                                                      | Historical evidence only; not a current venue                                                                      |

"Deep baseline" above means that a relevant collector/storage path exists. It is not a
blanket statement that every minute, field or instrument is complete. Each research
contract still needs its own point-in-time coverage and gap report.

## Implications for active candidates

### HYP-012 source lead

The frozen Gate-to-Binance cohort must not be rewritten. Its result can answer whether
the registered information lead survives on the Binance market proxy. With the current
execution boundary it cannot establish an executable money path.

If the mechanism survives and Binance remains unavailable, a Gate-to-Bybit or
source-venue execution variant requires all of the following before registration:

- owner-confirmed account/product/API eligibility;
- exact point-in-time source-to-target identity and native market route;
- outcome-blind coverage and executable-price readiness;
- a new prospective cutoff and contract. Existing HYP-012 episodes cannot be
  relabelled or reused as confirmation for the new target.

### HYP-015 hold12h

The current pair is PAPER research. Its reader/cost/verdict can be frozen and read
without choosing a live venue. A positive result still needs a separately confirmed
account route, executable capacity and a safe live broker before any funded test.

### Abnormal-flow economic screen

The historical discovery may begin on exact-native Bybit/Binance retained inputs, but
must report research venue and proposed execution venue separately. Until at least one
account route is owner-confirmed, its result can nominate a mechanism or prospective
cohort but cannot clear the roadmap's business-money gate.

## Owner confirmation checklist

Fill this without storing account IDs, balances, keys or other secrets in Git. A
simple yes/no/unknown plus verification date is sufficient for each candidate venue:

- account exists and KYC country is current;
- exact spot/perpetual product is visible and order-enabled;
- long and short permissions;
- API trading is permitted for that product;
- API key can be withdrawal-disabled and IP-allowlisted;
- collateral/settlement asset and practical minimum order;
- one-way versus hedge mode and maximum permitted leverage;
- funding or spot-borrow availability;
- tax/regulatory/operator restriction acknowledged outside this repository.

Do not create or test credentials merely to complete the table. Owner confirmation
selects which venue deserves a later bounded authenticated preflight; that preflight
must remain read-only until a separately reviewed execution-safety step.

## Decision and next gate

1. Treat Binance as research-only for HYP-012 unless the owner explicitly reverses the
   current internal restriction with fresh account evidence.
2. Ask the owner to confirm candidate account capabilities, with Bybit first because
   it has the smallest current data/identity gap. This is a priority, not an assertion
   that Bybit is available.
3. Do not add a venue adapter or L2 feed from this matrix alone. A new venue requires
   measured unique coverage plus a declared source or execution role.
4. Continue freezing HYP-015 and the abnormal-flow discovery protocol without private
   access. Do not claim a money candidate until a proposed execution venue is confirmed.

## Repository evidence

- Public/default venue registry: `apps/analytics/schurfer_analytics/exchange_registry.py`.
- Public versus authenticated client isolation and constructors:
  `apps/execution/schurfer_execution/exchanges.py`.
- Trading-mode ceiling and unavailable live brokers:
  `apps/execution/schurfer_execution/execution_intent.py`.
- Manual order mode gate: `apps/execution/schurfer_execution/routers/orders.py`.
- Per-venue market-data semantics:
  `docs/research/momentum-venue-capability-matrix-v1.md`.
- Account and scaling policy: `ECONOMICS.md`.
