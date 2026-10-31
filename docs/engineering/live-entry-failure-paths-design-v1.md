# Live entry failure paths: stop placement failure and unknown submission (design v4)

Status: **draft v4 for design review, 2026-10-30. No code.** v1 (9a36755), v2
(cc7407f) and v3 (16e445d) got REQUEST CHANGES. v4 allows a linked open trade in
section 4. v3 defined:

- recovery with the database down;
- a late partial fill on a still-active order;
- the executor's transitions;
- the owned remainder. Live trading is off
  (`AUTO_TRADE=false`, `LIVE_PROBE` blocked in code). These paths must be fixed with
  tests before any live order, as the [research strategy v2](../research/research-strategy-v2.md)
  requires. They are a subset of the fault tests in the
  [LIVE_PROBE step 2 design](../research/live-probe-step2-design-v1.md), section 7, and
  land before its PRs.

## The problems, checked in code on main (1e957d1)

### A. The protective stop cannot be placed

`orders.place_order`, around lines 527 to 598. The entry has filled and the attempt is
`accepted`. Then `create_stop_market_order` raises, and the code sends a reduce-only
market close, alerts, and returns. What goes wrong:

1. **The round trip is not journalled.**
   - The attempt stays `accepted`. Reconciliation resolves it to `completed` with no
     trade and no position.
   - `classify_position_state` then returns `UNKNOWN_MISMATCH`, and
     `INCIDENT_BLOCKER` blocks all entries under a name that says nothing.
2. **The emergency close is not durable.** It has no attempt row and no
   `clientOrderId`, so a timeout on it is never reconciled.
3. **If the close fails, nothing protects the position.**
   - `exit.py` has no loss stop.
   - Reconciliation v1 never closes.
   - The pending-close path (`write_pending_close`, `monitor._retry_pending_closes`)
     only re-commits a close that already filled; it never sends an order.
4. **A retried stop gets a new `clientOrderId`.** A stop that timed out but was created
   stays on the book unknown, and could later trigger against a new position.

### B. The entry submission result is unknown

`orders.place_order`, around lines 431 to 452. On `ccxt.NetworkError` or an unexpected
error, the attempt becomes `submission_unknown`, `SUBMISSION_UNKNOWN_BLOCKER` is set, and
no stop is placed. What goes wrong:

1. **A late fill has no stop** until an operator acts.
2. **Partial fills on a live order count as complete.**
   `reconciliation_worker._attempt_state_from_order` returns `completed` for any
   `filled > 0`, even while the order is still open. Protecting only the filled part
   leaves the rest free to fill later.
3. **The classifier does not compare quantities.** `MISSING_TRADE_EXACT_ATTEMPT` is
   returned for an attempt that filled 1 contract against an exchange position of 10
   (reviewer's synthetic check). An exact `clientOrderId` match alone does not prove
   the whole position is ours.
4. **The reconciliation worker sends no alert.**

### C. Live accounting is gross only

`journal.close_trade` on the live (non-paper) branch stores gross PnL with
`fees_usd=0`, `funding_usd=0` and no net PnL. So journalling a failed round trip
recovers the trade, but not its costs. LIVE_PROBE's threshold L needs net figures.

## Principles

1. **Durable before acting.** Each step writes its intent before it touches the
   exchange. Recovery after a crash at any point continues from the intent and the
   original attempt's identity (`clientOrderId`, order id), never from process memory.
   When the database is down, the attempt row written before the entry is that intent:
   every later order id derives from it (section 0).
2. **Exchange safety never waits for the journal.**
   - A missing database or an unresolved fill price does not delay a protective stop
     or a close.
   - Accounting follows, through the existing incident path when it cannot be done at
     once.
3. **Unknown is not absent.**
   - A timeout, or a lookup that finds nothing within its deadline, proves nothing.
   - A retry first looks up the previous request by its id, and reuses that id while
     the outcome is unknown.
   - A new send, with a new id, follows only a terminal result of the previous one:
     - an explicit rejection;
     - a terminal partial fill;
     - a cancel with no fill (normal for a Bybit market or IOC order).
4. **One owner per instrument.** Every path that sends an order for an
   (exchange, base) holds that instrument's order lock (`OrderLockLease`): the entry,
   the stop, the emergency close, recovery and reconciliation's flatten.
   - A recovery runs in one executor only.
   - The database enforces task uniqueness and the executor's claim; the Redis lock
     adds to them, it does not replace them.
5. **Blocks are durable.** A block that waits for an operator is a database row, not a
   flag in process memory, so a restart keeps it.
   - The existing `app.live_reconciliation_incidents` with status `manual_required`
     already blocks entries after every scan (`INCIDENT_BLOCKER`) and is never
     auto-resolved.
   - The new failure kinds are written there.

## Design

### 0. Order ids derived from the entry attempt

The entry's `clientOrderId` becomes 32 hex characters, written on the attempt row
before the entry is sent (`create_attempt` already fails closed). Every later order for
that attempt uses an id derived from it, within Bybit's 36-character `orderLinkId`
limit:

- protective stops: `<entry>s1`, `<entry>s2`, ...;
- emergency closes: `<entry>c1`, `<entry>c2`, ....

So the attempt row alone lets recovery find every order the attempt ever caused, by
listing the symbol's open and closed orders and matching the prefix. This holds even
if no later database write landed.

**With the database down:**

- the inline path sends at most one emergency close, `<entry>c1`, reduce-only;
- it alerts through Telegram, which needs no database;
- it sends nothing more until the database answers.

**When the database is back:** the emergency worker builds the task from the attempt
row and the exchange lookup of `<entry>s*` and `<entry>c*`, then continues by the
transitions in section 1.

### 1. Durable emergency close task

A new table `app.live_emergency_closes` (one migration) holds one row per position
that must be flattened:

- the exact identity (exchange, symbol, native id, market type, side) and base;
- the source attempt id;
- the owned quantity at creation, and the owned remainder (section 1, "Owned
  remainder");
- the reason (`stop_placement_failed`, `late_fill_flattened`);
- the status.

Its close orders are `close` rows in `app.live_order_attempts`, linked by a `task_id`,
not a second list. The table enforces one active task per source attempt and per
identity. The executor claims a task through a database lease, so two executors never
own one.

**States:**

| State             | Meaning                                                                                                     |
| ----------------- | ----------------------------------------------------------------------------------------------------------- |
| `requested`       | Written before any close is sent                                                                            |
| `sending`         | A close with a recorded `clientOrderId` is being sent                                                       |
| `unknown`         | That close timed out; look it up before any new send                                                        |
| `confirmed_flat`  | The exchange shows no position for the identity, and every protective order this task knows of is cancelled |
| `accounted`       | The round trip is journalled                                                                                |
| `manual_required` | A precondition failed (quantity mismatch, competing operation)                                              |

**Executor transitions,** under the order lock and the database claim, each pass:

| Previous close order                                               | Action                                                                   |
| ------------------------------------------------------------------ | ------------------------------------------------------------------------ |
| Unknown (timed out, not yet found)                                 | Look it up. **No new send.**                                             |
| Active on the book                                                 | Follow it to a terminal state. **No new send.**                          |
| Terminal: filled                                                   | Count the fill, update the owned remainder, then decide on the next send |
| Terminal: partial fill, cancel with no fill, or explicit rejection | Count any fill, update the owned remainder; a next send is allowed       |
| None yet                                                           | First send allowed                                                       |

**Before any send:**

- read the exchange position for the exact identity;
- check the ownership and quantity conditions again. They are re-checked on every pass,
  not only at creation.

**Done:** the task reaches `confirmed_flat` only when all three hold:

- the exchange shows no position;
- **no order of the task is unknown or active;**
- every protective stop it knows of is cancelled or terminal.

A flat position with an unresolved order is not done.

**Owned remainder.** The owned quantity at creation, minus every confirmed fill of this
attempt's closes and of its protective stops that triggered.

- A send is allowed only if the exchange position equals the owned remainder, and its
  size is that remainder.
- Any other position (a manual addition, another operation) moves the task to
  `manual_required`. Example: ours 10, closed 4, someone adds 2, the exchange shows 8
  against a remainder of 6, so nothing is sent.

**Who runs it:**

- The first pass runs inline in the path that created the task, still under its lock.
- Later passes run in a separate supervised emergency worker with a short bounded
  loop (about 2 s), using the same executor code. The monitor's 30 s tick serves other
  work.
- The monitor and reconciliation never send a close for an identity that has an
  active task.

The task also writes a `manual_required` incident (`emergency_close_pending`), so
entries stay blocked until an operator clears it after `accounted`.

### 2. Stop placement (A)

1. **Before the first send,** the attempt row stores the stop's `clientOrderId`.
2. **Retries:**
   - timing: 0, 1.5 and 4.5 s, under one overall deadline of about 10 s, each request
     with its own timeout;
   - after a network error, look up that id first;
   - after an explicit rejection, a new id is allowed.

   A non-retryable rejection (an invalid trigger, for instance) skips the retries.

3. **If no stop is confirmed within the deadline,** an emergency close task is created
   at once. It also cancels any stop it knows of (found late) before reaching
   `confirmed_flat`.
4. **Accounting is never a precondition of the close.** It starts only once the task
   is `confirmed_flat`, in its own supervised worker (`emergency_accounting.py`, about
   every 10 s), so a slow exchange or database there never delays a close. It sends
   nothing.
   - **Inputs:** the source attempt row, the task's close rows and the entry's stops
     that ended with a fill, each found on the exchange by its derived id. A fill
     amount the exchange does not report yet is unknown, not zero. Once every amount is
     known, the legs must add up to the owned quantity; otherwise `manual_required`.
   - **Prices** come from each order, never from a ticker.
   - **One transaction** publishes the open, each leg in execution order (the last one
     terminal), the close at the legs' amount-weighted price, and the links of the
     task, its entry attempt and its close rows to the trade; the task is then
     `accounted`. Nothing partial is ever visible to reconciliation, and a failed
     publish leaves nothing behind, so the next run builds the round trip again from
     the same facts.
   - **Retries:** anything unknown, or a read or the publish that fails, is retried;
     still not accounted 10 minutes after the first `confirmed_flat`:
     `manual_required`.
   - **Revised in PR 3b.** This first said `complete_open` and the incident path. That
     path writes exit-tracking keys for a position that is already flat, and leaves the
     trade open in the journal across several 30 s ticks, which reconciliation reports as
     `db_only_orphan`. The task already holds every fact the incident path would
     rebuild.

### 3. Unknown entry submission (B)

1. **Before any lookup:**
   - the attempt is written `submission_unknown`, so the blocker follows from the
     database;
   - the in-process `SUBMISSION_UNKNOWN_BLOCKER` is set.

   A crash during the lookup then leaves a recoverable state.

2. **The lookup** polls at about 1, 3, 6 and 10 s, by `clientOrderId`, still under the
   order lock. Reconciliation takes the same lock before acting on that instrument, so
   the two never race. `_find_exact_order` moves to a shared module.
3. **Branch on the result:**
   - **Terminal with a fill:** `mark_accepted` with the real order id, continue the
     normal path (stop, journal) at the filled amount.
   - **Still open with a partial fill:**
     - protect the filled part with a stop at once;
     - cancel the rest and confirm the cancel;
     - then resize the stop and journal at the final filled amount.

     Entries stay blocked until the order is terminal.

   - **Terminal with no fill:** `no_fill`, nothing to protect.
   - **Nothing found:** stays `submission_unknown`; reconciliation continues the
     lookup.
   - **Found later by reconciliation,** in any of these states: reconciliation runs
     the same branch as above.
     - An active order with a partial fill gets its confirmed part protected at once,
       the rest cancelled and confirmed, and the final quantity fixed.
     - Then, being a stale entry, it is flattened through an emergency task (section
       4). It never waits for an operator just because the order was still active
       when found.

4. **`_attempt_state_from_order`** returns a new unresolved state `partial_active`
   (filled above zero, order not terminal) instead of `completed`. Unresolved states
   keep blocking entries.

### 4. Late fill found by reconciliation

Reconciliation creates an emergency close task (`late_fill_flattened`) only when **all**
of these hold; otherwise the case stays `manual_required`:

- the exact order is found by `clientOrderId` and is terminal with a known filled
  amount, or has been made terminal by the partial-active branch of section 3;
- the identity is complete, and the side matches the attempt;
- the exchange position equals our accounted quantity for that identity: the source
  attempt's final filled amount;
- **open trades:** either none exists for the identity, or exactly one exists and it is
  linked to this source attempt. "Linked" means the attempt row's `trade_id` points to
  it and its journalled quantity equals the attempt's filled amount.
  - The partial-active branch of section 3 journals the open before the task exists,
    so this case is normal, not a conflict. It also lets recovery continue after a
    crash between journalling the open and creating the task.
  - Any other open trade on the identity, or a link that is missing or ambiguous,
    means `manual_required`;
- no other active attempt, close attempt or emergency task exists for the identity,
  and the order lock is free.

**The classifier changes:** `MISSING_TRADE_EXACT_ATTEMPT` requires equal quantities. A
mismatch is `POSITION_SIZE_MISMATCH`, manual.

**One round trip:** when a linked open trade exists, the task closes that trade
(`close_trade` on its `trade_id`) and does not journal a second open.

**What the worker reads:** the attempt's full row (size, leverage, contract size, exit
parameters, setup context), and the entry fill price through `resolve_fill_price` on
the found order, for the journal.

**What does not trigger a close:** a missing Redis stop key on its own. After a Redis
loss it would flatten every healthy position. The protective-order check reads the
exchange's open conditional orders instead.

### 5. Alerts

- **Deduplicated:** the reconciliation worker alerts only when an incident key is first
  inserted, or moves from `resolved` back to `open` (from the upsert's result), never
  on every 30 s scan.
- **Emergency tasks** alert when created, on `manual_required`, and when
  `confirmed_flat`.
- **Delivery failures:** every alert checks `notify_alert`'s return value and logs a
  failed delivery as an error.

### 6. Net live accounting

Recording the round trip fixes the lost trade, not the lost costs. Net live accounting
(fees from the exchange fills, funding settlements) is a **prerequisite of LIVE_PROBE**
and its own PR. This design does not enable any entry, and `LIVE_PROBE` stays blocked
until that PR is in.

## Delivery in four PRs

1. **Classifier and alerts:** the quantity check, `partial_active`, deduplicated
   reconciliation alerts. No migration.
2. **Emergency close task:**
   - the derived order ids;
   - the table and the `task_id` link;
   - the executor and its supervised worker;
   - the incident kind;
   - the protective-order cancel.

   One migration.

3. **Stop placement path,** delivered in two parts:
   - **3a:** the durable stop id, bounded retries with lookup, the task on failure with
     its inline first pass, the one close without a database, and the worker building
     the task once the database is back.
   - **3b:** accounting of the confirmed-flat task, and the `accounted` state.
4. **Unknown submission path:** durable state first, the locked lookup, partial-active
   handling, and reconciliation's late-fill task.

Each has the two reviewer touchpoints. The migration is deployed only on the owner's
go.

## Tests (fake exchange, before any testnet run)

| Case                                                            | Expected                                                                                                                      |
| --------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| Stop times out but was created                                  | Found by its id, no second stop                                                                                               |
| Stop rejected 3 times                                           | Emergency task, flat, round trip journalled, incident `manual_required`                                                       |
| Stop fails and the fill price is unresolved                     | Close sent at once; the round trip is journalled once every price resolves, or `manual_required` after 10 minutes             |
| Database down before the close                                  | One close `<entry>c1` sent; the task is built from the attempt row and the exchange once the database is back; flat confirmed |
| Database down, close sent, crash before the database returns    | After restart the worker finds `<entry>c1` by prefix and continues; no blind second send                                      |
| Restart between each pair of steps                              | Recovery continues from the task and attempt rows; no duplicate exposure                                                      |
| Emergency close: explicit rejection, zero, partial, unknown     | Each takes its own transition; unknown is looked up before any new send                                                       |
| Two recoverers at once                                          | Only the holder of the database claim and the lock sends                                                                      |
| Previous close unknown or still active                          | No new send until it is terminal                                                                                              |
| Position absent while a close is still unknown                  | Task not done                                                                                                                 |
| A close fills partially (IOC), then is cancelled                | Fill counted, next send for the remainder                                                                                     |
| Foreign volume added after a partial close                      | Position differs from the remainder: `manual_required`, nothing sent                                                          |
| Entry times out, found filled                                   | Stop, journal, no blocker                                                                                                     |
| Entry times out, found open with a partial fill                 | Partial protected, rest cancelled and confirmed, then final size                                                              |
| Entry times out, the rest fills after the cancel was sent       | Final amount from the terminal order, stop resized                                                                            |
| Entry times out, found terminal with no fill                    | `no_fill`, no blocker                                                                                                         |
| Entry times out, nothing found                                  | `submission_unknown` durable before the lookup; blocker stays                                                                 |
| Reconciliation later finds the entry active with a partial fill | Part protected, rest cancelled and confirmed, then flattened as a stale entry                                                 |
| Late partial fill, open already journalled before the task      | Task closes the linked trade; one journalled round trip                                                                       |
| Late partial fill, restart right after the open was journalled  | Recovery finds the linked trade, creates the task, closes it; one journalled round trip                                       |
| Late fill with an unlinked open trade on the identity           | `manual_required`, nothing sent                                                                                               |
| Late fill, quantities equal, no competing operation             | Flattened through a task, journalled, incident resolved                                                                       |
| Late fill, position larger than our quantity                    | `POSITION_SIZE_MISMATCH`, manual, nothing closed                                                                              |
| Orphan with no exact attempt                                    | Unchanged: quarantined, manual, never closed                                                                                  |
| Redis loss on a healthy position                                | No close; the protective check reads the exchange                                                                             |
| Same incident over many scans                                   | One alert                                                                                                                     |

The 5 and 10 s budgets are starting values, measured on testnet before mainnet. They
are not shown safe by this design.

## Settled in review

- **Stop placement failure:** close after a short bounded recovery.
- **Late fill:** close, never adopt the stale signal.
- **Closing on an exact match:** allowed only with proven ownership and quantity. A
  `clientOrderId` match alone is not enough.
- **Storage:** a separate task table, with its orders kept as linked `close` rows in
  `app.live_order_attempts`. Uniqueness and the claim are enforced in the database.
- **Executor:** a separate supervised worker with an inline first pass.
- **Net live accounting:** a separate PR, required before LIVE_PROBE.
