# Bybit 1-minute burst: exit management v1, readout

Protocol: [bybit-burst-exit-management-v1](bybit-burst-exit-management-v1.md) (two design
reviews; reader details fixed before the read). Read once on 2026-10-05 at `5cf52a0` from
a clean checkout. Data before 2026-09-29 only. Exploratory: both halves lie inside the
window in which the trigger was found.

**Evidence** ([bybit-burst-exit-management-v1](evidence/bybit-burst-exit-management-v1/)):

| File               | sha256            | What                                                   |
| ------------------ | ----------------- | ------------------------------------------------------ |
| `exit-result.json` | `c2819a328b5f...` | the result; it pins the firings, tapes, books, funding |

**Coverage:** the common set holds 421 of 421 entered firings in the choice half (218
instruments, 25 days) and 293 of 295 in the test half (132 instruments, 22 days). 16
firings had no trade within 2 s of the 5 s entry; 3 exits met a stale book.

## Results

Net in bps of the entry notional, after the book on both sides, fees and funding. A
decided exit fills 5 s after its decision.

| Exit               | Choice half: mean / median | Test half: mean / median | Decided (test) | Median hold (test) |
| ------------------ | -------------------------- | ------------------------ | -------------- | ------------------ |
| H, hold 60 min     | +71.8 / +42.6              | +53.0 / -134.2           | 0%             | 60 min             |
| S, stop -3%        | +15.1 / -244.6             | +17.4 / -281.9           | 71%            | 6 min              |
| T, trailing 3%     | +3.2 / -44.4               | -31.3 / -91.0            | 97%            | 79 s               |
| E, check at 15 min | +68.3 / -28.4              | +52.5 / -190.1           | 61%            | 15 min             |
| P, take-profit +5% | **+102.3** / +266.8        | -52.0 / +81.9            | 44%            | 60 min             |

**Chosen on the choice half: P.** On the test half (n = 293):

- P's mean net -52.0 bps (median +81.9); 95% CI by instrument -143.6 .. +28.4, by UTC
  day -140.0 .. +37.6.
- Paired against H: -105.0 bps; CI by instrument -260.1 .. +41.3, by UTC day -220.3 ..
  +17.0.
- The top-5 share is undefined: the total net is negative.

**Zero exit delay (optimistic bound only):** the stops lose most to the delay (S's
choice-half mean +54.6 at zero delay against +15.1 at 5 s; T's +38.1 against +3.2). P
does better with the delay (+102.3 against +63.8): after +5% the price kept rising for a
few seconds.

## Decision: no exit is put forward

- The chosen exit's lower bounds on the test half are below zero.
- **What the numbers say plainly:**
  - the take-profit makes the typical trade positive (median +82 bps in the test half)
    but caps the few large moves that carry the mean, and the mean turns negative;
  - stops do not help: the moves are so violent that a 3% trail fires within about 80 s
    in almost every firing, and a 5 s exit delay costs the stops most of what they keep;
  - holding the hour stays the best mean, with a typical trade that loses (test-half
    median -134 bps).
- **HYP-030 on this history:** no simple exit turns a mean carried by a few moves into a
  rule that pays on the typical trade. The sealed forward measurement keeps running to
  its end as the independent check; nothing here registers a forward cohort.
