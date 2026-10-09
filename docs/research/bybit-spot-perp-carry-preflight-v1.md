# Bybit spot and perpetual carry: account and margin preflight, funding and basis capture (design draft v1)

Status: **draft for design review, not registered.** Nothing is collected, read or
traded under it. Design review 1 (2026-10-08) folded in.

## Where this sits

This is the **next stage** of the existing
[execution feasibility canary](bybit-spot-perp-feasibility-v1.md) (#476,
`bybit_carry_feasibility`), not a replacement. Everything the canary fixes stays as it
is: USD 50 per leg, a USD 300 bank as context, three rounds, the book-age and skew
limits, one common base quantity, the 10 bps scenario fee, and its run boundary
(not before 2026-10-31).

The canary's own decision rule names the only permitted next step: if at least 10
distinct exact catalog pairs pass in at least two of its three rounds, design an
account-mode and margin-risk preflight together with separate prospective funding and
basis capture. This document is that design. **It starts only if the canary passes;**
if it does not, the USD 50 Bybit-only carry line stops at the canary and this draft is
withdrawn.

## The return, in money

For one pair held from entry to exit, with q the common base quantity:

- spot leg: q x (spot bid at exit) minus q x (spot ask at entry);
- perpetual leg (short): q x (perp bid at entry) minus q x (perp ask at exit);
- funding: the sum over the contract's actual settlements inside the hold of
  q x mark price at the settlement x rate (a positive rate pays the short, a negative
  one charges it);
- fees: the account's actual taker rate on each of the four fill notionals.

Net = spot leg + perpetual leg + funding - fees. The basis change between entry and exit
is inside the two legs, never left out: if spot stays at 100 while the perpetual rises
from 101 to 103, the pair loses 2q whatever the funding. Executable bid and ask carry
the spreads, so no spread is added again.

**Funding schedule.** Each contract's settlements are read from its own schedule
(`fundingInterval` in the instrument catalog, and the settlement times actually
recorded); intervals differ between contracts and can change. A rate "per 8 h" appears
only as a labelled normalization unit, never as the settlement rule.

## Capital (fixed now)

- **Notional:** USD 50 per leg, as in the canary.
- **Bank:** USD 300 for one pair.
- **Spot cost:** the spot leg's entry notional plus its fee.
- **Collateral allocated to the short:** the short is held in **isolated margin**, with
  position margin explicitly allocated. It must cover the +300% stress: the loss on a
  quadrupled price (3 x the entry notional), the maintenance margin at that price, and
  the fees, with the account's actual maintenance rate and fee tier from the preflight.
  At USD 50 that is USD 150 plus 0.5% to 2.5% of a USD 200 notional, about USD 151 to
  155 before fees; with the spot leg, roughly USD 210 of the USD 300 bank.
- **Reserve:** what remains of the bank. A pair whose spot cost plus allocated
  collateral plus fees exceeds USD 300 is not openable at this size, and that is a
  result, not something to work around.
- Cross-collateral (spot holdings backing the short in a unified account) is recorded
  by the preflight if the account offers it, but the stress and the capital rule use
  isolated margin, so the result does not depend on one account's settings.

## Stage A: account and margin preflight (the owner's account, no order)

The owner trades from Poland; Bybit's EU entity may not offer USDT perpetuals to
retail clients. On the owner's own account, recorded with the date:

1. spot and USDT linear perpetuals both tradable;
2. isolated margin available on the perpetual, and how position margin is added;
3. the maintenance margin rate tiers for the pair's notional;
4. the actual spot and perpetual taker fee rates;
5. whether the account offers cross-collateral, as context only.

Pass: 1 and 2 hold, and the capital rule above fits USD 300 for at least the canary's
passing pairs at their measured books. Otherwise the line stops here and nothing is
collected.

**Tooling.** `make prod-carry-preflight CANARY_SHA256=<the canary's published sha256>
TRADING=2026-10-07 ADD_MARGIN=<date> ADD_MARGIN_LIMIT=<USD>`
(`schurfer_execution.carry_preflight`). Before any exchange request it refuses a run
before 2026-10-31 and any canary artifact whose SHA-256, version, clean revision, run date
or decision does not hold; the pairs come only from that artifact. It then reads items 2
to 5 with the read-only key through a GET-only allow-listed client and applies the capital
rule per pair. Two things a read-only key cannot establish are owner confirmations with
their dates: item 1 (the owner opened a Bybit perpetual position on 2026-10-07), and that
margin can be added to an isolated position, with the largest amount; a pair whose margin
above the 1x initial margin exceeds it is blocked. The record keeps every request, the safe
part of every response with its SHA-256 (the key only as derived facts), the canary's
SHA-256 and the code revision, written once with its own SHA-256 under
`runtime/research/carry-stage-a`.

## Stage B: prospective funding and basis capture (registered separately)

Funding and basis outcomes are read only under their own registration, written after
stage A passes, which fixes before any outcome:

- the trade rule: when a pair opens and closes, at most how many at once, the capital
  per pair from the rule above;
- the money formula above, with books for every fill (the canary's age and skew limits)
  and the actual settlement schedule;
- a missing settlement, mark price or book is missing, never zero;
- the PR 1 planning rule for what net effect is worth testing; a few hundred book
  observations establish feasibility, not power;
- **the data boundary.** The registration is written only after HYP-012 v2 is
  terminal. Data on or after 2026-09-29 is then available to it under v2's rules:
  - after an administrative stop, the stop artifact's `closed_windows` are excluded for
    good, by canonical asset (by base ticker when no canonical asset maps), from any
    venue, whenever **any** window a pair uses intersects one: entry and exit books,
    the funding settlements of its hold, and any lookback a rule uses;
  - excluded pairs are dropped, never imputed;
  - data dated before the registration is historical for it (discovery), never
    prospective; the confirming window starts after the registration.

## Not in this draft

- Other venues (MEXC, Gate, Binance): the owner's execution access is the constraint.
- Reverse carry (borrowed spot short) and leverage on the spot leg.
- Any order, size above USD 50 per leg, or automation.
