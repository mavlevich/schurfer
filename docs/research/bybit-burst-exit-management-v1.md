# Bybit 1-minute burst: exit management v1 (protocol draft)

Status: **design review 1 folded in (2026-10-05); registered before any exit is
simulated.** Exploratory throughout. It runs
after the [cost history](bybit-burst-cost-history-v1.md) and only if that study does not
park HYP-030. Data before 2026-09-29 only. No trading rule follows from it directly.

## Why

The burst's gross return rests on a few large moves while the typical firing is near
zero. When that is so, cutting the trades that do not move and keeping the ones that do
can change the net. It can also make it worse: in the pump-short work (HYP-024) stops
hurt and patience won. This study tests a small, fixed set of exits honestly: chosen on
one half of the window, judged on the other.

## Population and data

- **Firings:** the 732 frozen decay firings (sha256 `cb3f8154bb43...`).
- **Entry:** the first trade at or after B + 5 s, then the cost study's executable entry
  (USD 50 walked through the asks at that moment).
- **Prices during the hold:** the decay study's verified Bybit trade tapes. Exits are
  triggered on trade prices and filled at the order book at the trigger moment, read
  from the cost study's verified archive.
- **Costs:** fees on entry and exit notional and funding as q x mark x rate, as in the
  cost study.

## The exits (fixed now; no other variant is evaluated)

| Id  | Exit                                                                                        |
| --- | ------------------------------------------------------------------------------------------- |
| H   | hold 60 minutes (the baseline)                                                              |
| S   | stop: the first trade at least 3% below the entry price, else 60 minutes                    |
| T   | trailing: the first trade at least 3% below the highest trade since entry, else 60 minutes  |
| E   | early check: at 15 minutes exit if the last trade is below the entry price, else 60 minutes |
| P   | take-profit: the first trade at least 5% above the entry price, else 60 minutes             |

**Execution.**

- A triggered exit (S, T, P) fills at the book 5 s after the trigger trade, the same
  latency target as the entry. The signal must still be received, processed and sent;
  on a sharp move that delay matters.
- A scheduled exit (H at 60 minutes, E at 15 minutes) fills at the book at its moment:
  that moment is known in advance.
- The same results with a zero exit delay are reported beside them, labelled as the
  optimistic bound only.
- A book that is broken, stale or too thin at the fill moment makes the firing
  `exit_unfilled` for that exit. It is counted and never filled at the trade price.

## The split (fixed now; both halves exploratory)

Both halves lie inside the window in which the trigger was found and its results were
studied. The split helps choose a candidate; it is **not** an independent confirmation.
Every interval below is descriptive, and confirmation is left to a new registered
cohort.

**A common denominator for every comparison.**

- Within a half, exits are compared only on the firings where the entry and all five
  exits resolve (with costs and funding).
- This common set must hold at least 60% of the half's firings with a resolved entry,
  or the half is `insufficient_data` and nothing is chosen.
- Each exit's own exclusions are reported apart, by reason. An exit can never win
  because its losers were excluded.

**Choice half:** firings with bar start before 2026-09-07 00:00 UTC (ISO weeks 33 to
36).

- On the common set, all five exits are evaluated.
- The one with the highest mean net is chosen, H included.
- With fewer than 150 firings in the common set, nothing is chosen and the study
  stops.

**Test half:** firings from 2026-09-07 (ISO weeks 37 to 40).

- Only the chosen exit and H are evaluated, on the half's common set.
- Reported: the mean net of the chosen exit and its paired difference against H.
- Descriptive 95% cluster bootstrap intervals by instrument and by UTC day (10,000
  iterations, fixed seed).
- The same minimums apply: at least 150 firings in the common set, 30 instruments and
  10 UTC days.

## What it decides

- **The chosen exit's mean net on the test half has both descriptive lower bounds above
  zero:** the exit becomes the candidate rule of a separately registered forward
  cohort (on the sealed path, an untouched window). That cohort, not this study, is
  the confirmation.
- **Otherwise:** no exit is put forward.
- The paired difference against H is reported as evidence of whether the exit itself
  adds value; it is not needed for the decision above.
- **Not decided here:** any order, size, or live trading.

## Not in this study

- Other stop or target levels, combinations of exits, or entry filters (a separate
  registration if ever wanted).
- Maker exits, partial exits, sizes above USD 50.
