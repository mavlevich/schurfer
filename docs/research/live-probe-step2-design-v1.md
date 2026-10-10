# LIVE_PROBE step 2: the first real orders on Bybit (design draft v1)

Status: **draft for design review, not registered, no code.** `LIVE_PROBE` stays blocked
in code (`build_broker` raises for it) until the PRs below are merged **and** a
candidate has passed the stage its own registration names before live (see "Which
candidate first"). The design is written now; the code starts only after the
2026-10-12 disk analysis and once a candidate's next stage is defined.

## What a probe is, and is not

A probe tests **execution only**: that one candidate's orders can be sent, filled,
monitored and closed on the owner's real Bybit account without an unreconciled state.
Its PnL does not confirm the strategy, does not change any cohort verdict, and
authorizes no more capital (as already fixed in the HYP-012b early pilot protocol).

## What exists (merged)

| Piece                                                         | Where                                     |
| ------------------------------------------------------------- | ----------------------------------------- |
| Read-only account preflight, `diagnostic` and `live_probe`    | `bybit_preflight.py` (#458, step 1)       |
| Durable order attempt written before the exchange call        | `order_attempts.py`, `orders.place_order` |
| `clientOrderId` (Bybit `orderLinkId`) generated locally       | `orders.place_order`                      |
| Journal crash window closed                                   | #282                                      |
| Partial fills journalled at the filled size                   | `orders.py`, `fill_price.py`              |
| Reduce-only protective stop and reduce-only close             | `orders.py`                               |
| Unknown send result resolved by `clientOrderId`, incidents    | `reconciliation_worker.py` (#299)         |
| Live position monitor with the shared exit decision           | `monitor.py`, `exit.py` (#361)            |
| Trading-enabled flag (kill switch) checked on the order path  | `risk.py`, `routers/account.py`           |
| Worker readiness gate checked again right before the exchange | `orders.place_order`                      |

Gaps in what exists, found in review and checked in code:

- `AUTO_TRADE=true` is today the only way to allow live orders, and it opens two entry
  paths that call `place_order` directly: the manual `POST /order` and the legacy
  pump-short path in `trader.py`.
- `place_order` draws a fresh `uuid4` for every `clientOrderId`, so a retried intent
  becomes a second order.
- The `live_probe` preflight refuses an account with any open position or order, so it
  cannot run before a protective stop or a close.
- `exit.load_exit_params` falls back to the pump-short defaults when the Redis key is
  missing (a lost Redis turns a 720-minute hold into a 60-minute `no_progress` exit),
  and `monitor.py` substitutes "now" for an unresolved `opened_at`, which suppresses
  the age exit that is the main exit of a hold strategy.

## Design

### 1. One entry gate inside `place_order`, not only in a broker

Every **new entry** passes one check inside `orders.place_order` itself: it needs a
`ProbeReservation` (section 4) for an active authorization. A call without one is
refused and recorded, whoever makes it. So the manual `POST /order`, the legacy
pump-short path and any future caller cannot open exposure outside a probe.

`LIVE_PROBE` gets its own switch and does **not** use `AUTO_TRADE`. `AUTO_TRADE` stays
false in production; with `LIVE_PROBE` on, `mode_ceiling` still refuses the manual and
legacy entry paths. Tests: with `LIVE_PROBE` enabled, `POST /order` and the legacy
trader path are refused, and a `place_order` entry without a reservation is refused.

The `LiveProbeBroker` stays the only strategy-facing piece (strategies never touch the
order path), but it is not where the permission lives.

### 2. Authorization record in PostgreSQL

One row per probe, written by the owner through a CLI, append-only history of issue
and revoke:

- probe id, environment (`testnet` or `mainnet`), the account it is bound to (an
  internal account key checked against the preflight; no Bybit user id or key in any
  artifact), strategy, contract version and sha256, allowed instruments;
- N (maximum entries), the notional per entry, L (section 5);
- `entries_until` (D: the last moment a new entry may be sent) and `flat_by` (the moment
  by which every position must be closed, `flat_by >= entries_until`);
- issued at, revoked at, and who did each.

The broker and the gate read it from the database. A file export exists for audit only.

### 3. Entries and protective actions follow different rules

- **New entry:** active, unrevoked record for this account and environment; now before
  `entries_until`; a free slot; N and L headroom reserved (section 4); the `live_probe`
  preflight (empty account, isolated, 1x, one-way) run immediately before the order.
  The first failed check refuses the entry and records why.
- **Protective stop, reduce-only close, monitor exits:** never blocked by an expired or
  revoked record, by reaching L, by stopped entries or by the kill switch for entries.
  They are allowed only for a **probe position**: one whose journalled fills link it
  to an attempt of this probe, on the exact instrument and side, and the close
  quantity is capped at the journalled open quantity of that probe. The preflight is
  not run for them (it would refuse because a position is open).
- An exchange position with no probe link, or a quantity that differs from the
  journal, is an incident: new entries stop, and that position is **never closed
  automatically** (it may be the owner's manual position). The owner resolves it.
- At `flat_by` the monitor closes any open probe position reduce-only, under the same
  link and quantity cap.
- Recommended: run probes on a dedicated Bybit subaccount, so the owner's manual
  trading neither blocks probe entries (the preflight wants an empty account) nor
  meets the probe's closes.

### 4. Idempotency and spending N

A fixed chain, stored before any exchange call:
`probe -> intent.idempotency_key -> attempt -> clientOrderId`.

- `idempotency_key` is deterministic from the probe id and the strategy's decision id.
- The slot, one unit of N and the L headroom are reserved in **one transaction**, with
  a unique `(probe_id, idempotency_key)`. A repeated delivery returns the existing
  attempt and its result and sends nothing.
- `clientOrderId` is written on the attempt before the send and reused on retry.
- `pending` and `submission_unknown` keep the reservation until reconciliation decides
  the outcome; a fill spends N; a proven non-placement releases it.

Reservation happens before the network preflight, so a revoke or `entries_until` can
arrive while the preflight runs. The attempt therefore moves through states:

- `reserved`: slot, N and L headroom held; nothing may be sent.
- `reserved -> sending`: one short transaction after the preflight passes. It locks the
  authorization row (`SELECT ... FOR UPDATE`), checks the record is unrevoked and the
  database clock is before `entries_until`, and only then marks the attempt `sending`.
  A revoke takes the same row lock, so the two are ordered.
- Revoke or deadline first: the attempt becomes `refused_before_send`, the reservation
  is released, nothing is sent.
- `sending` first: the send goes ahead. From that moment the attempt counts as
  potential exposure (slot, N and L stay held) until reconciliation settles it, even if
  the record is revoked a millisecond later; a later revoke stops only further entries.

Tests: revoke before the transition (no send), revoke after it (send, exposure held
until reconciled), and `entries_until` passing during the preflight (no send).

### 5. L: the stop threshold

L counts realized PnL, the unrealized PnL of the open position at the mark price, fees
and funding paid. The formula, the maximum age of the mark (a staler mark counts as L
reached for entries) and the action are fixed in PR 1. Reaching L stops new entries
and closes the open position reduce-only. L is a stop threshold, not a guaranteed
maximum loss: slippage on the close can exceed it.

### 6. Exit policy restored from the database

The full exit policy of the strategy, with a version, is stored on the attempt
(`order_attempts.exit_params` already exists) and the position's `opened_at` comes from
the journalled fill time. After a Redis loss the monitor restores both from the
database. If neither Redis nor the database has the policy, the position keeps its
protective stop, an incident is raised, and no default policy is applied. Test: a
restart with an empty Redis keeps the 720-minute HYP-015 hold, not `no_progress`.

### 7. Faults tested on purpose, then testnet, then the smallest mainnet size

- On a fake exchange in tests: partial fill, IOC partly filled then cancelled (Bybit
  can do this to a market order), timeout after send (`submission_unknown`), repeated
  delivery, restart mid-position, Redis loss. Their chance appearance on testnet is not
  a test.
- Then a testnet lifecycle on `api-testnet.bybit.com` with a testnet key: open,
  restart, reconcile, close, no incident.
- Then a mainnet probe at the smallest size Bybit accepts, under its own authorization
  record. Only after it ends clean, a separate record may allow USD 50.

### 8. Probe report

Every intent, attempt, fill, fee, funding settlement and the reconciliation state,
written once with a sha256, so the probe's claim (execution works) is checkable.

## PR order

1. Authorization record and reservations: schema, migration, CLI, the L formula, the
   idempotency chain and the `reserved -> sending` transition; tests that a missing,
   expired, revoked or foreign-account record refuses, that a repeated delivery sends
   nothing, and both orders of revoke against the transition. No order path yet.
2. The entry gate in `place_order`, the `LIVE_PROBE` switch apart from `AUTO_TRADE`,
   the closed manual and legacy entry paths, `LiveProbeBroker` and the entry/protective
   split with the probe-position link and quantity cap (an unknown position is an
   incident, never closed), with the fault tests on a fake exchange. `build_broker` still refuses
   `LIVE_PROBE` on mainnet.
3. Exit policy persistence and restore, `opened_at` from the fill, the first
   candidate's exit parameters, and the probe report.
4. Testnet run (owner provides a testnet key), recorded; then the mainnet switch opens
   only for an authorization record that names a candidate which passed its stage.

Each PR gets one design touchpoint (this document) and one review before merge.

## Which candidate first

- **HYP-015 hold12h**: a `candidate` on 2026-11-04 is **not enough**. Its registration
  (`momentum-flow-hold12h-verdict-v1.md`, Gate E) lets `candidate` authorize only the
  episode study or a real shadow, never live. A probe needs that next stage passed and
  then a separate owner authorization record.
- **Carry** only after its stage B: two legs (spot and perpetual) need a second order
  path (spot) and leg-pairing, so it is a separate design.
- Anything else only with its own registered path to live.

## Not in this draft

- Any size above USD 50, leverage above 1x, more than one slot, or automation without
  a per-probe record.
- MEXC, Gate or Binance execution.
- Using probe PnL as evidence for a strategy.

## Review answers (2026-10-10)

1. The record lives in PostgreSQL with issue and revoke history and binding to the
   account, the environment and the contract version; a file is an audit export.
2. L counts the open loss, fees and funding (section 5).
3. Testnet, then a separately authorized smallest-size mainnet probe, then possibly
   USD 50, with faults reproduced on purpose (section 7).
