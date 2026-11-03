# Net live accounting, design v1

Status: draft for the design touchpoint. Required before LIVE_PROBE
(`live-entry-failure-paths-design-v1.md`, section 6; `docs/research/live-probe-step2-design-v1.md`,
sections 5 and 8). It enables no entry.

## Problem

- `journal.close_trade_in` on the live branch (`legacy_price_only_v1`) writes gross PnL
  from the journal prices and `size_usd`, with `fees_usd = 0`, `funding_usd = 0`, no net
  PnL and `accounting_status = 'legacy'`.
- `realized_pnl_today` sums `pnl_usd`, which is that gross figure. The daily loss breaker
  therefore misses every fee and funding payment of a live trade.
- LIVE_PROBE's threshold L counts fees and funding paid, and its probe report lists every
  fill, fee and funding settlement. Neither exists for a live trade today.

## Scope

- **In:** non-paper trades opened after the deploy, on every close path (monitor exit,
  recovered close attempt, exchange stop, incident worker, emergency close round trip).
- **Out:**
  - paper accounting, unchanged;
  - live trades already journalled, which stay `legacy` with no backfill;
  - the L formula and unrealized PnL at the mark (LIVE_PROBE PR 1).

## 1. Source: the Bybit execution list

Fees and funding come from `/v5/execution/list` (category `linear`). ccxt's
`fetch_my_trades` and `fetch_funding_history` both call it.

- **Window.** One request covers at most 7 days and pages by cursor.
- **Trade rows** (`execType = Trade`), one per fill. Each has `execId`, `orderId`,
  `orderLinkId`, `side`, `execQty`, `execPrice`, `execValue`, `execFee`, `feeCurrency`
  and `execTime`. `execFee` is negative for a maker rebate.
- **Funding rows** (`execType = Funding`), one per settlement of a held position.
  `execFee` is the amount paid, negative when received.
- **Why not the order's `fee` field:** it is cumulative, can be missing on a conditional
  stop, and has no per-fill id. Executions have a unique `execId`, so rereading them is
  idempotent.
- **Why not `/v5/position/closed-pnl`:** it is per close order, spreads the open fee pro
  rata and leaves funding out. It can serve as a cross-check, not as the source.
- **Sign conventions** are an assumption until the LIVE_PROBE testnet run (its section 7)
  confirms them on a real lifecycle. They are fixed in one function, so a wrong sign is a
  one-line fix found before mainnet.

## 2. Which executions belong to a trade

Ownership is decided by orders, never by time alone. The read window runs from the first
entry fill minus 1 minute to the last exit fill plus 1 minute. It only bounds the
download, and being inside it makes no row the trade's.

- **The trade's orders:**
  - the entry order;
  - its closes: `trade_close_fills.order_id`, `exit_order_id`, and the close rows of an
    emergency close task;
  - every order on the exchange whose `orderLinkId` carries the entry's derived prefix
    (`<entry>sN`, `<entry>cN`, section 0 of the failure-paths design). This covers stops
    and closes whose order ids the journal never saw.
- **Each order is complete** when the sum of `execQty` over its trade rows equals the
  confirmed terminal filled amount of that order, read from the order itself. A
  non-terminal order, or fewer rows than its fill, leaves the trade `pending`.
- **The trade is complete** when the entry rows sum to the trade's entry filled
  quantity, and the exit rows to the same quantity. All comparisons are exact `Decimal`
  equality, with no step tolerance, so a lost contract is never absorbed.
- **Other trade rows in the window:**
  - a row of another trade of ours on the instrument (its orders or its derived prefix)
    is that trade's, and no conflict;
  - a row of an unknown order inside the holding period, from the first entry fill to
    the last exit fill, sets `incomplete`;
  - a row of an unknown order outside that period is not this trade's; foreign positions
    stay reconciliation's.
- **Fee currency:** every fee must be in USDT, or the costs are `incomplete`.

## 3. Funding

Bybit does not guarantee that a position opened or closed within 5 seconds of a
settlement is charged for it ([Bybit, funding fee calculation](https://www.bybit.com/en/help-center/article/Funding-fee-calculation)).
So funding is matched to settlements, never cut by a time rule alone.

- **Settlement times** come from the instrument's public funding history
  (`/v5/market/funding/history`), so a changed funding interval is followed.
- **The settlements checked** are every T from the first entry fill minus 5 s to the
  last exit fill plus 5 s, both ends included. An exit at `07:59:58` therefore checks the
  settlement at `08:00:00`, and an entry at `08:00:03` the one at `08:00:00`.
- **For each settlement T,** the quantity held at T is the entry fills minus the exit
  fills before T. Then:
  - **Clearly held:** no fill of the trade within 5 s of T and a quantity held. A funding
    row at T must exist, and its `execQty` must equal the quantity held. A missing row
    leaves the trade `pending`; a different quantity sets `incomplete`.
  - **At the boundary:** a fill of the trade within 5 s of T. A row at T whose `execQty`
    equals the quantity held just before or just after the fill is counted. Anything
    else, including no row, sets `incomplete` for an operator. It is never taken as a
    zero.
  - **Clearly not held:** no row is expected. A row at that T is not this trade's.
- **One proven owner.** The account is one-way (`positionIdx` 0, checked by the Bybit
  preflight), so a funding row belongs to the symbol's single position, not to an order.
  Before a row at T is counted, every other candidate for it is listed:
  - each other live trade of ours on the symbol, legacy ones included, from the journal
    (not from the read window) that was held at T or has a fill within 5 s of T;
  - each fill of an unknown order on the symbol within 5 s of T.

  The row is counted only when this trade is the only candidate. Any other candidate
  sets `incomplete`, even when the quantity matches: a legacy position of 100 closed
  2 s before T and a new one of 100 opened 2 s after it both match a row of 100. The
  unique `execId` stops a row being counted twice, not being given to the wrong trade.

- **Funding is a confirmed zero** only when:
  - the settlement list has been read past the last exit fill plus 5 s, so a settlement
    just after the exit is in it;
  - no checked settlement is held or at the boundary;
  - at least 5 minutes have passed since the last exit fill.

  Until then the trade stays `pending`, so the worker never settles before a charge can
  appear.

## 4. Figures

The sign convention matches paper accounting: a cost is positive.

- `fees_usd` is the sum of `execFee` over the trade's fills; rebates come in negative.
- `funding_usd` is the sum of `execFee` over its funding rows.
- **Gross from executions:** the exit `execValue` minus the entry `execValue` for a long,
  the reverse for a short. It replaces the journal's gross, which uses the intended
  `size_usd`. Spread and slippage are already in the fill prices, so `slippage_usd` is
  not deducted again and stays empty.
- `net_pnl_usd` is gross minus `fees_usd` minus `funding_usd`.
- `net_pnl_pct` is `net_pnl_usd` divided by the entry `execValue`, times 100.
- `pnl_usd` and `pnl_pct` become the net figures once settled; these feed the daily loss
  breaker.

## 5. States and the entry block

- **At open.** `journal.accounting_contract`, the one function every open goes through,
  gives a non-paper trade `accounting_version = 'live_exchange_costs_v1'` and
  `accounting_status = 'pending'`. This covers the emergency close round trip of PR 3b
  too.
- **At close.** `close_trade_in` writes the gross figure as today, plus estimated costs
  from the paper cost model for display only, and `pnl_usd` = gross minus the estimate.
  The status stays `pending`. The estimate is not an upper bound: one funding settlement
  can cost more than the model.
- **Entries wait for the costs.** The PnL tracker revokes PnL readiness while any closed
  `live_exchange_costs_v1` trade is `pending` or `incomplete`. This is a new check next
  to `any_open_trade_close_fills`. Readiness gates only entries (`place_order`), so
  protective stops, exits and emergency closes keep working. A cost worker that is down
  therefore blocks entries; it never lets them through on an estimate.
- **Settled.** A supervised cost worker, about every 60 s, takes each closed `pending`
  trade and does one transaction:
  1. reads the window and the settlement list;
  2. checks every order, the trade's quantities and the funding (sections 2 and 3);
  3. writes the evidence rows (section 7) and the final figures;
  4. sets `accounting_status = 'complete'`.

  It revokes the PnL readiness lease, as every close does; the tracker's next tick
  admits entries again.

- **A stale tick cannot restore readiness.** Today a tick can read the journal, a close
  can revoke and commit, and the tick can then publish the lease from the old figures.
  `try_commit_close` revokes before its commit, and the post-publish recheck sees only
  the Redis pending-close markers, which a committed close does not leave. With the cost
  block, a tick that read the trade as `pending` with the estimate can publish after the
  worker has committed the net figures. Both are closed by an accounting generation:
  1. **Counter.** One row, `app.pnl_generation`, holds a number. Every transaction that
     changes a tracker input increments it before it commits: a close, a
     `trade_close_fills` row, a fill incident opened or resolved, a cost settlement, an
     operator settlement. Database triggers on those tables do it, so no writer can
     forget. A sequence would not do: its value is visible before the writer commits.
  2. **Tick.** The tracker reads the realized PnL, every blocking check and the counter
     G in one read-only snapshot transaction. It publishes the lease with G and the
     daily PnL in one value. Then it reads the counter again; if it is no longer G, it
     revokes the lease it published.
  3. **Entry.** `place_order` reads the lease, then the counter in its own query. It
     admits the entry only when the counter still equals the lease's G, and refuses when
     the database cannot be read. The daily PnL it checks is the one in the lease, so
     the figure and its generation are never read apart.

  A change committed before the tick's snapshot is in its figures. One committed after
  it moves the counter, so the entry is refused whether or not the writer's own revoke
  ran or the post-publish read saw it. The writers' revokes and the post-publish read
  stay, so the lease does not linger, but safety rests on the counter check at entry.

- **Not settled.**
  - An unknown fill in the holding period, a quantity that does not match, a fee not in
    USDT, or funding at a boundary that does not match sets `incomplete` with
    `accounting_error`. Entries stay blocked and an alert goes out once.
  - A trade still `pending` 24 h after its exit becomes `incomplete` the same way. The
    24 h only limit the automatic retries; entries were blocked from the close.
  - A failed read or write changes nothing: the trade stays `pending` and the next pass
    retries.
- **Operator settlement.** An `incomplete` trade blocks entries until an operator acts.
  A CLI records the figures the operator confirmed from the exchange, with a note, as
  `operator_settled`; that lifts the block. The figures stay marked as entered by hand.

## 6. Costs of an open position

One collector serves both cases. For an open trade it returns the fees of the entry
fills so far and the funding settled since the first entry fill, with the same order and
funding checks and a flag when they are not complete. It ships in this PR; LIVE_PROBE
PR 1 wires it into L.

## 7. Evidence rows (migration 0062)

New table `app.trade_cost_legs`, one row per execution attributed to a trade:

- `trade_id`, `exchange`, `exec_id`, `exec_type` (`trade` or `funding`);
- `order_id`, `client_order_id`, `side`;
- `quantity`, `price`, `value_usd`, `fee_usd`, `fee_currency`;
- `executed_at`, and the raw row as received.

It is unique on `(exchange, exec_id)`. It is written in the same transaction as the
trade's final figures, so a rerun after a crash writes nothing twice. These rows are the
fills, fees and funding of the LIVE_PROBE probe report (its section 8).

The same migration creates `app.pnl_generation` with its one row and the triggers of
section 5. The migration is deployed only on the owner's go.

## Tests

All on a fake execution list and settlement list, plus Postgres for the transaction and
the uniqueness.

| Case                                                          | Expected                                                          |
| ------------------------------------------------------------- | ----------------------------------------------------------------- |
| Market entry, market exit, two settlements held, one paid     | `complete`; fees, funding, gross and net from the rows            |
| Maker exit with a rebate                                      | Negative fee counted                                              |
| Funding received on a short                                   | Negative `funding_usd`                                            |
| Exit by the exchange stop `<entry>s1`, id never journalled    | Attributed by its `orderLinkId`, `complete`                       |
| Emergency close round trip with `<entry>c1` and `<entry>c2`   | Both legs attributed, `complete`                                  |
| Entry and exit filled 100, the list returns 50 of each        | Orders incomplete: `pending`, never `complete`                    |
| Exit rows short by one contract                               | `pending`, then `incomplete` at the deadline                      |
| Unknown fill on the symbol inside the holding period          | `incomplete`, entries blocked, one alert                          |
| Trade B opens 20 s after trade A closed                       | B's rows are B's; A `complete`                                    |
| Settlement held, its funding row not yet listed               | `pending` until the row appears                                   |
| Funding row quantity differs from the quantity held           | `incomplete`                                                      |
| Exit fill 3 s after a settlement, no funding row              | `incomplete`, never a zero                                        |
| Exit fill 3 s after a settlement, row with the quantity held  | Counted, `complete`                                               |
| Exit fill at `07:59:58`, settlement at `08:00:00`, no row     | Settlement checked; `incomplete`, never a zero                    |
| Exit fill at `07:59:58`, settlement at `08:00:00`, row of 100 | Counted, `complete`                                               |
| Legacy 100 closed 2 s before T, new 100 opened 2 s after T    | Two candidates for the row: `incomplete`                          |
| Unknown fill on the symbol 3 s from a settlement held         | Two candidates: `incomplete`                                      |
| No settlement in the holding period, 5 minutes after the exit | `funding_usd = 0`, `complete`                                     |
| Fee in a currency other than USDT                             | `incomplete`                                                      |
| Holding longer than 7 days                                    | Window read in 7-day requests, every row once                     |
| Execution list fails                                          | Still `pending`, retried; `incomplete` after 24 h                 |
| Closed trade `pending` or `incomplete`                        | PnL readiness revoked: entries refused, stops and exits unchanged |
| Operator settlement of an `incomplete` trade                  | `operator_settled`, readiness returns on the next tick            |
| Close commits between the tick's snapshot and its publish     | Counter moved: entry refused, lease revoked after publish         |
| Net figures commit between the tick's snapshot and publish    | Counter moved: entry refused, estimate never used                 |
| Writer commits but its revoke never runs                      | Entry refused by the counter until a fresh tick                   |
| Counter unreadable at entry                                   | Entry refused                                                     |
| Crash after the read, before the commit; rerun                | One set of evidence rows, figures written once                    |
| Open position with its entry fees and one settlement          | Collector returns both, complete flag set                         |
| Paper trade and legacy live trade                             | Unchanged, never block entries                                    |

## Delivery

One PR:

- migration 0062;
- the version at open and the estimate at close;
- the tracker's entry block, the accounting generation and its check in `place_order`;
- the cost worker under the existing supervisor, and the operator settlement CLI;
- the shared cost collector;
- the evidence rows and the tests above.

The testnet run of LIVE_PROBE confirms the sign conventions and the funding row fields
(`execQty` as the quantity held) on a real lifecycle before any mainnet order.

## Settled in review

- **Entry block, not an estimate:** a closed trade whose costs are unknown blocks entries
  through PnL readiness; the paper-model estimate is for display only.
- **Completeness:** each order against its confirmed fill and the trade against its
  quantity, with exact equality; equal sums of what was found are not enough.
- **Funding:** matched to settlements by quantity; a boundary within 5 s that does not
  match goes to an operator; a zero is confirmed only after the settlement list is read.
- **Settlements checked:** from the first entry fill minus 5 s to the last exit fill
  plus 5 s, both ends included.
- **Funding owner:** a row is counted only when the trade is its one candidate among our
  trades, legacy included, and unknown fills; otherwise `incomplete`.
- **Readiness:** an accounting generation in the database, read in the tick's snapshot,
  published with the lease and checked at entry, so a stale tick cannot admit an entry.
- **Window:** for the download only; a neighbouring trade of ours is not a conflict.
- **Gross:** from the executions, without deducting slippage again.
- **Open-position collector:** in this PR; wired into L by LIVE_PROBE PR 1.
- **24 h:** the limit of automatic retries; entries are blocked from the close.
