# LIVE_PROBE loss threshold L, design v1

Status: draft for the design touchpoint of LIVE_PROBE PR 1. It fixes what
`docs/research/live-probe-step2-design-v1.md` section 5 leaves to PR 1: the formula of
L, the maximum age of the mark price, and the action when L is reached. It enables no
entry.

## What L is

L is a positive USD amount written in the probe's authorization record (section 2 of
the LIVE_PROBE design). It bounds what the probe may lose before it stops. It is a
stop threshold, not a guaranteed maximum: a gap through the stop, or slippage on the
close, can lose more.

L is per probe, apart from the daily loss limit of the risk checks. Both apply; the
first one reached stops entries.

The probe has one slot, and its entry preflight requires an empty account (sections 3
and 7 of the LIVE_PROBE design). So an entry is never decided while a probe position
is open: the loss of an open position feeds only the close (section 4), and an entry
sees closed trades and reserves only (section 3).

## 1. The probe's loss

For one probe, at the moment `t` of a fresh mark (section 2):

```
loss(t) = - realized_closed
          - realized_open_parts
          - unrealized_at_mark(t)
          + fees_paid_open
          + funding_paid_open
          + funding_unlisted_estimate
          + exit_fee_estimate(t)
```

With no open probe position every term after `realized_closed` is zero, and no mark is
needed.

- **realized_closed:** the sum of `pnl_usd` of the probe's closed trades. Once a
  trade's costs are settled from the exchange (`live_exchange_costs_v1`, `complete` or
  `operator_settled`) this is net of its fees and funding. While a closed trade is
  `pending` its `pnl_usd` is gross minus the paper-model estimate, and entries are
  blocked by PnL readiness until it settles.
- **realized_open_parts:** the gross PnL of the parts of the open trade already
  closed: `sign × Σ exit_qty_i × (exit_price_i - entry_vwap)` over its exit execution
  rows, with `sign` +1 long and -1 short. Their fees are in `fees_paid_open` and are
  not subtracted again. Example: long 50 at 1.00, 20 closed at 0.90, mark 1.00: this
  term is -2.00 and the loss before costs is 2.00, not 0.
- **unrealized_at_mark:** `sign × open_qty × (mark - entry_vwap)`.
- **entry_vwap, open_qty:** from the open trade's own execution rows as the collector
  owns them (section 1.1): the amount-weighted entry price, and entry quantity minus
  exit quantity. `open_qty` must equal the journalled open quantity of the probe; a
  difference is an incident (section 1.2) and never a close by L. Bybit's own
  `unrealisedPnl` and `avgPrice` are not used: the quantity and price must be the ones
  the probe owns.
- **fees_paid_open:** every fee of the open trade's fills so far, entry and partial
  exits, positive when paid and negative for a rebate.
- **funding_paid_open:** every listed funding row the collector owns for the open trade.
- **funding_unlisted_estimate:** settlements held but not listed yet (section 1.1).
- **exit_fee_estimate:** `open_qty × mark × taker_rate`. Counting the close fee before
  it is paid makes the position close while L still covers it.
- **taker_rate:** the account's taker rate for the instrument, read from Bybit's fee
  rate endpoint (`/v5/account/fee-rate`, as `carry_preflight.py` does) at the
  `live_probe` preflight and stored on the attempt. Not the entry's own fee over its
  value: a maker entry would give a rebate rate and understate the close.

L is **reached** when `loss(t) >= L`. All arithmetic is `Decimal`.

### 1.1 The open-position collector result

`live_costs.settle(..., open_position=True)` from #547 stops at the first funding
settlement it cannot match and then returns only a status: `fees_usd` and
`funding_usd` are `None` even when the fills and the earlier settlements are proven.
It also raises no incident. PR 1 adds a separate open-position entry point that runs
the same order, quantity and ownership checks and returns:

- `status`: `unproven_tail`, `pending` or `incomplete`. An open position has no
  `complete`: the history never lists a settlement after now (below);
- `entry_vwap`, `open_qty`, `realized_open_parts`, `fees_paid_open`: from every
  confirmed execution up to now, a partial close after `T_last` included. `T_last`
  bounds only the completeness of funding, never the fills;
- `funding_paid_open`: the listed funding rows the trade owns, complete up to `T_last`;
- these figures are filled for `unproven_tail` only;
- `published_to`: `T_last`, the newest settlement the history lists;
- `unlisted`: one item per observed settlement held and not listed yet, with its time,
  its rate, the quantity held and the mark used (below), or marked unknown;
- `reason`: why it is `pending` or `incomplete`.

`pending` is a fill order that is not terminal or whose rows do not add up yet, as in
#547; no figure is used and L waits for the next tick. `incomplete` keeps today's
meaning (an unknown fill, a row of an odd type, a quantity that does not match,
another candidate owner); no figure of it is used.

**Settlements after the newest published one.** The public funding history lists a
settlement only once it is published, and is complete only up to its newest listed time
`T_last`. Nothing proves the tail after it: Bybit's dynamic settlement frequency can
settle on a shorter interval and return to a longer one before either history lists
that settlement (history at 08:00, an hourly 09:00 not listed, then back to 4 hours
with the next at 12:00: `12:00 - 08:00` equals the current interval, yet 09:00 was
charged). The current interval and `nextFundingTime` are never taken as proof.

So while a probe position is open, the tail from `T_last` to now is always
**unproven**, and the computed loss is a lower bound (section 4 says what that
allows). Within the tail:

- **Observed settlements.** The evaluator keeps every `nextFundingTime` it reads from
  `/v5/market/tickers`, at the entry preflight and on every monitor tick. One that has
  passed and is not listed is a settlement Bybit announced; it is estimated as below.
  If Bybit moved it and it never happened, the loss is overstated until the history
  passes that time: the safe direction.
- **Anything not observed** (a settlement between two reads, a restart) adds nothing
  and stays unknown.

The closed-trade path of #547 has the same gap: a settlement inside the holding period
that the funding history does not list yet, and whose row the execution list does not
list either, is never checked, and the trade settles `complete` without it. It is
fixed apart from this note, before #547 is deployed: a closed trade settles only once
the history lists a settlement at or after the end of its checked range (5 s before
the first entry to 5 s after the last exit, both included); until then it stays
`pending`, and after the 24 h deadline `incomplete`.

**Each unlisted settlement** is estimated on its own, by Bybit's rule (funding fee =
position value × funding rate; a positive rate means long pays short, a negative rate
short pays long):

```
estimate_s = max(0, sign × qty_held_s × mark_s × rate_s)
```

- `rate_s`: the rate of that settlement from the public funding history (`fundingRate`
  of `/v5/market/funding/history`, the endpoint the collector already pages for the
  times; PR 1 makes `settlements()` return the rate with the time).
- `qty_held_s`: the quantity the probe held at the settlement, from its own fills. With
  a fill within 5 s of the settlement, the largest of the quantities held across that
  window (`held_around` of #547).
- `mark_s`: the highest mark of the two one-minute bars of
  `/v5/market/mark-price-kline` either side of the settlement. An upper bound: the
  estimate only raises the loss.
- Funding received (`sign × rate_s < 0`) counts as zero until its row is listed.

- `rate_s` of an observed settlement not in the history yet: the `fundingRate` the
  ticker showed with that `nextFundingTime`, the last read before it passed.

If `rate_s`, `qty_held_s` or `mark_s` cannot be read (no rate was read before the
settlement, an exit order of the trade has rows not listed yet, the kline read fails),
that settlement is **unknown**: no estimate is made from the current quantity
or mark. The loss is then a lower bound (section 4 says what that allows).

### 1.2 Who raises the incident

The **probe loss evaluator** (PR 1, inside the position monitor) owns the result. On
`incomplete`, on an observed settlement not listed 30 minutes after its time, or when
the history's `T_last` is more than 8 h 30 min old while a probe position is open
(assumed: no Bybit interval is longer than 8 h), it writes
`costs_incident_at` and the reason on the authorization record, once, and sends one
alert. While that field is set the record admits no entry; only the owner clears it.
It never closes the position by itself. After the close, the trade's own costs go
through the cost worker of #547 as for any live trade, and an `incomplete` there
blocks entries through PnL readiness as today.

## 2. The mark and its age

- **Source:** Bybit's `markPrice` from the position read (`/v5/position/list`), the
  same read that proves the position's quantity. `account.fetch_positions` drops
  positions of zero size, so an empty account returns no row and no mark; that is
  correct, because with no open probe position the loss needs no mark (section 1).
- **Evaluated:** on every position monitor tick (every 30 s) while a probe position is
  open.
- **Maximum age: 60 s.** The age is the time since the read that returned the mark. A
  stale or missing mark never **decides** that L is reached: the exchange's protective
  stop still guards the position. Three ticks in a row without a fresh mark raise one
  alert. A stale mark never delays a close task already written (section 4).
- **Entries** use no mark. The quote that sizes the entry order is checked by the
  existing order preparation, apart from L.

## 3. Entries: headroom for the next trade

An entry needs room for its own worst planned loss, reserved with its slot and its unit
of N (section 4 of the LIVE_PROBE design):

```
reserve = notional × (initial_sl_pct / 100 + 2 × taker_rate)
        + notional × |funding_rate| × settlements_in_max_hold
```

- `taker_rate` is the same preflight reading as in section 1.
- `funding_rate` is the instrument's current rate, `settlements_in_max_hold` the
  number of settlements the strategy's maximum hold can span, rounded up, plus one.

The entry is admitted only if `-realized_closed + reserved_open + reserve <= L`, with
`reserved_open` the reserves still held by attempts not yet settled. A reserve is
released when the trade's costs settle (its real result is then in `realized_closed`)
or when reconciliation proves the order was never placed. If `-realized_closed >= L`
on its own and the record has no `l_reached_at`, the admission writes it (section 4).

## 4. When L is reached

**The decision.** The evaluator decides L is reached from a fresh mark when
`loss(t) >= L`. With an unknown settlement (section 1.1) the computed loss is a lower
bound: it may decide reached, never not reached.

**Recorded, then closed, in two transactions:**

1. `l_reached_at`, the loss and its inputs (mark, its read time, quantities, fees,
   funding, the estimates, unknown settlements) on the authorization record, only if
   `l_reached_at` is empty. It commits on its own, so entries stop at once, without
   waiting for the instrument's order lock.
2. Under the instrument's order lock (`lock:order:{exchange}:{BASE}`, the lock every
   close takes), an emergency close task (`emergency_close.py`, section 1 of
   `live-entry-failure-paths-design-v1.md`) for the probe position, with the reason
   `probe_loss_threshold`. A busy lock (a monitor exit in flight) writes no task; the
   step is retried.

`l_reached_at` on a record with an open probe position and no task is a standing close
request, not only a field: the evaluator (PR 1c) runs step 2 again on every tick for
every such record, whether the decision was taken in this process or not, so a failed
step 2 is finished after a worker restart as well. The task row then carries the
request: it is written before any close is sent, survives a restart, and its executor
runs whatever the price and the mark do afterwards. If step 1 fails, the evaluator
keeps the decision and its inputs in process, refuses the probe's entries through the
worker gate, and retries step 1 (with the first inputs, not a fresh evaluation) on
every tick. A restart before it lands loses only that latch: section 5 then closes the
position anyway.

**Closed by the emergency task.** Its rules are merged and answer the close's failure
modes:

- **Ownership before quantity.** The task's `owned_quantity` is the trade's full
  confirmed entry fill, as the executor expects: on every pass it subtracts, once each,
  the confirmed fills of the task's close rows and of the entry's protective stops
  (found by the `<entry>s` prefix). So every close of the trade, terminal ones
  included, must be a close row of the task: PR 1 links the trade's existing close
  attempts (`app.live_order_attempts`, operation `close`) to the task by setting their
  `task_id`, never inserts a second row for the same `clientOrderId`, and a close of
  the trade the task cannot link moves it to `manual_required`. Example: entry 10, the
  stop filled 4, exchange 6: `owned_quantity` 10, remainder 6, close 6 (with 6 as the
  owned quantity the executor would compute 2 and refuse). A send is allowed only if
  the exchange position on the exact instrument and side equals that owned remainder,
  and its size is that remainder. Any other position (journal 10, exchange
  5 with nothing confirmed to explain it) moves the task to `manual_required`: nothing
  is sent and the owner resolves it, as section 3 of the LIVE_PROBE design requires.
- **Orders already in flight.** A close of the trade not terminal when the task is
  created (a monitor exit, a `flat_by` close) is linked the same way and followed as a
  previous order of the task. The monitor holds the order lock from its task check
  until its close row is final, so step 2 sees every such close.
- **Closes that bypass the task.** The monitor's task check fails open on a database
  error, so a protective close may still be sent after the task exists. On every pass,
  under the order lock and before anything is sent, the executor links the trade's
  closes written since then and follows them; a close of the exact instrument since
  the task (since the entry, when the task is written) that it cannot link moves the
  task to `manual_required`. A close proven to be of another exact instrument of the
  same base does not count; one whose instrument is unknown does.
- **No send before the previous order is resolved.** An unknown order is looked up, an
  active one is followed to a terminal state; neither allows a new send. A visible
  position proves nothing about an order Bybit is still processing. A terminal
  partial fill, a cancel with no fill or an explicit rejection allows the next send,
  sized by the owned remainder.
- **Done (`confirmed_flat`)** only when the exchange shows no position, no order of the
  task is unknown or active, and every protective stop of the entry is cancelled or
  terminal. A flat position with an unresolved order is not done.
- **Accounting** of the round trip starts after `confirmed_flat` and never delays the
  close. PR 1 extends it to a trade already in the journal: the task's close legs are
  written to that trade, not a new round trip. A leg the journal already holds for the
  trade (a monitor exit's partial fill linked to the task, or a stop the monitor
  journalled) is kept as journalled when it matches its exchange order (amount, price,
  exchange) and is never written again. Only the last leg may already be terminal; then
  the trade is closed as it stands. A journalled leg of the trade that is not one of
  the task's legs, or one that does not match, needs the owner, and nothing is written.
  Every writer of a trade's close legs holds the trade row until it commits, so no leg
  is added between the check and the writes, and a closed trade takes no new leg.
- The monitor and reconciliation send no close for an instrument with an active task.

**What the close depends on.** The executor needs the database (its task row) and
Redis (the instrument's order lock, as every close). While either is down, no close
is sent and an alert says so. The only protection independent of this process is the
exchange-side protective stop (`<entry>sN`); `flat_by` is a deadline in the same
process, not a separate guard. No lock-free close path is added: it would race the
other closers.

**After the close.** `l_reached_at` stays, so the record admits no entry. A new probe
needs a new record.

**Alerted.** One alert with the probe id, the loss, L, the inputs and whether the
transaction landed; one more at `confirmed_flat`, or at `manual_required`.

Reaching L never touches a position without a probe link, and never blocks a
protective stop, a close or a monitor exit.

## 5. Restart recovery

A decision held only in process (section 4) dies with the process, and the price can
recover before it restarts. So a restart never continues an active probe on its own.

On every start of the execution service, before the monitor's first tick and before
any entry:

1. Every active authorization record gets `suspended_at`. A suspended record admits no
   entry until the owner resumes it with the probe CLI, after seeing the probe's
   state.
2. For each record with a probe position open in the journal, an emergency close task
   with the reason `probe_recovery` is created, in a second transaction under the order
   lock (section 4, step 2), unless a task for that position already exists. Its
   ownership and quantity rules (section 4) apply: a position that does not match the
   owned remainder is not closed and goes to `manual_required`. Until every position
   has its task, the start is not recovered and is retried; the suspensions of step 1
   stay committed.
3. One alert per record: suspended, and whether a recovery close was created. The
   suspensions are alerted even when step 2 fails, since a retry suspends nothing more.

If the database is not readable at start, nothing of the probe runs until it is: no
entry is possible without it, and the protective stop guards an open position. The
recovery runs on the first successful read, still before any probe evaluation.

Any deploy of the execution service while a probe position is open therefore closes
that position. That is the price of not trusting a decision that may have been lost.

## Tests

| Case                                                             | Expected                                                              |
| ---------------------------------------------------------------- | --------------------------------------------------------------------- |
| Long, mark falls until `loss(t)` crosses L                       | Record and close task in one transaction, one alert                   |
| Same, the price recovers on the next tick                        | Task runs until `confirmed_flat`; no entry admitted                   |
| Long 50 at 1.00, 20 closed at 0.90, mark 1.00                    | Loss before costs 2.00, not 0                                         |
| Partial close of 20 after `T_last`, its rows listed              | Counted in `open_qty` and `realized_open_parts`; no quantity mismatch |
| Exit fee estimate tips `loss(t)` over L                          | Reached before the close fee is paid                                  |
| Short, negative rate, settlement held, row not listed            | Counted as paid at the held quantity and the bar high mark            |
| Long, negative rate, row not listed                              | Counted as zero, not as income                                        |
| Partial exit after a settlement, its row not listed              | Estimated at the quantity held then, not the current one              |
| History at 08:00, hourly 09:00 unlisted, back to 4 h, next 12:00 | Tail unproven; loss a lower bound; never a zero                       |
| Observed `nextFundingTime` passed, not listed                    | Estimated at the ticker's rate; incident 30 min after it              |
| History's `T_last` older than 8 h 30 min, position open          | `costs_incident_at`, one alert                                        |
| Collector `pending` on the open position                         | No figure used; next tick                                             |
| Collector `incomplete` on the open position                      | `costs_incident_at`, one alert, no automatic close                    |
| Empty account, no mark returned                                  | Entry decided on closed trades and reserves                           |
| Mark stale for three ticks, nothing requested                    | No close, one alert                                                   |
| Mark stale after the task exists                                 | Task continues                                                        |
| Decision transaction fails                                       | No send; entries refused in process; same transaction retried         |
| L recorded, order lock busy, worker restarted                    | Next tick sees `l_reached_at` with no task and writes it              |
| Monitor check fails open after the task, close unknown           | Linked on the next pass; no send before it is resolved                |
| Close of another native market of the same base                  | Not counted                                                           |
| Crash after the decision, before the transaction landed          | Restart: suspended, `probe_recovery` close                            |
| Entry 10, stop filled 4, exchange 6                              | `owned_quantity` 10, remainder 6, close 6                             |
| Monitor exit filled 3 before the task                            | Its row linked, not inserted again; remainder 7                       |
| Journal 10, exchange 5, nothing confirmed explains it            | `manual_required`, nothing sent                                       |
| Monitor exit in flight when L is reached                         | Adopted; no new send before it is terminal                            |
| Close times out after send, position still visible               | Lookup only; no new send                                              |
| Close partly filled, then cancelled                              | Next send of the owned remainder                                      |
| Flat, but a close order unknown or a stop still resting          | Not `confirmed_flat`                                                  |
| Redis down after the decision                                    | No send, alert; exchange stop guards; send once the lock is held      |
| Restart with an active record and no position                    | Suspended, no close, alert                                            |
| Restart, order lock busy at step 2                               | Suspensions alerted; step 2 retried; not recovered until it lands     |
| Restart with an existing task for the position                   | No second task                                                        |
| `-realized_closed >= L`, record field empty                      | Admission writes `l_reached_at`, refuses                              |
| Entry whose reserve does not fit under L                         | Refused before the reservation is written                             |
| Reserve held by a `submission_unknown` attempt                   | Counted until reconciliation decides                                  |
| Closed probe trade still `pending`                               | Entries blocked by readiness                                          |
| Exchange position without a probe link                           | Not counted, never closed by L                                        |

## Not in this note

- The authorization record's schema and CLI, the reservation chain and the
  `reserved -> sending` transition: the rest of PR 1, as the LIVE_PROBE design has them.
- Any value of L: each authorization record names its own.
