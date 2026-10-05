# Where the edge is lost v1: readout (read 2)

Registered study: [edge-loss-decomposition-v1](edge-loss-decomposition-v1.md) with
amendments 1 and 2.

- **Read 1** (reader `7b66074`) is superseded. Review 2 found correctness defects in it
  (bar quality, verdict minimums, entry timing, Gate prices and identity).
- **Read 2** (reader `f8c99fa`, Gate `72ecb55`) uses the same verified inputs; only the
  amendment 2 fixes differ. `9999a0e` changed only the command's printed summary.
- **Review 3** found that Gate ordered trades with the same timestamp by price. With
  Gate's deal order (`348b96f`) the Gate read reproduces the same `gate-result.json`
  byte for byte (sha256 `c8359904...`).

**Evidence** ([read-2](evidence/edge-loss-decomposition-v1/read-2/); read 1 is kept
under `read-1-superseded/`):

| File                                                          | sha256                    |
| ------------------------------------------------------------- | ------------------------- |
| `inputs.json`                                                 | `e3e1615f60a1...`         |
| `result.json`                                                 | `587923861364...`         |
| `gate-identity.json` / `gate-tapes.json` / `gate-result.json` | see their `.sha256` files |

**Inputs:**

- Bybit bars 2026-08-13..09-28 (47 days, each a verified Borg fetch reduced on the host).
- Binance from 2026-08-18, the first day with at least 99% price coverage.
- The MEXC 1-minute archive from 2026-08-28.
- The scanner's 20,653 pump sources (07-23..09-28).
- 90 Gate July tapes resolved through the scanner's stored `market_id`.

## Verdict on the registered primary contrast: negative established

Bybit linear, 5-minute return of at least +5% on price-complete bars, long, 60-minute
hold, one firing per instrument per hour, 20.5 bps per side (Bybit's published 5.5 bps
taker fee, not measured, plus 15 bps slippage). The values are written in the order
they were computed:

| Entry                                         | Resolved | Unresolved | Clusters (instr. / day) | MDE (80%) | Mean net  | 95% CI by instrument | 95% CI by day | Verdict  |
| --------------------------------------------- | -------- | ---------- | ----------------------- | --------- | --------- | -------------------- | ------------- | -------- |
| t+1 open (registered; bar-optimistic)         | 2,584    | 11         | 419 / 47                | 54.7 bps  | -40.5 bps | -79.9 .. -2.9        | -79.8 .. -3.6 | negative |
| t+2 open (first open after data is available) | 2,582    | 13         | 419 / 47                | 58.2 bps  | -40.4 bps | -77.0 .. -5.6        | -82.7 .. -2.1 | negative |

The gross mean is about zero (+0.5 bps; median -93.5 bps). **For this trigger, a
5-minute +5% Bybit move does not pay for its costs over the next hour, and a faster
entry does not change that.** The verdict covers this one contrast only.

**Other cells.** The other 214 cells are descriptive and multiple (3 venues x 2
families x 3 thresholds x 2 sides x 3 horizons x 2 entries); 33 of them show a positive
mean net at the middle cost. They are not findings. Among the 5-minute cells, 12 of 108
are positive. Examples:

- Binance +10% long at 15, 60 and 240 minutes (+67 bps at 60 minutes with the t+1
  entry, median -28 bps);
- Bybit +5% and +10% shorts at 240 minutes (+12 and +36 bps).

Most of the positive means have negative medians, so a few large moves carry them.

## Part A: how late the scanner's pumps are seen (descriptive)

Share of the move (24h low to the 24h peak after the crossing) already gone, median:

| Moment                                         | Bybit | Binance | MEXC   | Gate (point-in-time trades) |
| ---------------------------------------------- | ----- | ------- | ------ | --------------------------- |
| 5m +3% crossing                                | 0.30  | 0.30    | 0.13   | 0.19                        |
| 5m +5% crossing                                | 0.42  | 0.38    | 0.23   | 0.27                        |
| 24h +20% crossing (bar close / trade boundary) | 0.69  | 0.67    | 0.59   | 0.62                        |
| same, 101 s later (Gate)                       |       |         |        | 0.61                        |
| scanner's `first_seen_at`                      | 0.70  | 0.68    | 0.64   | 0.73                        |
| scanner lag after the crossing (median)        | 63 s  | 56 s    | 29 min | 41 min                      |

- **About two thirds of a scanner pump is over when its 24h change reaches +20%.** The
  crossing comes about 22 hours after the 24h low.
- **Gate, 0 to 101 seconds after the crossing:** the medians of this descriptive share
  are close (0.62 at the crossing, 0.61-0.63 at 5, 15, 45 and 101 s; the median price
  age is 0.5-2 s). They are medians over different event sets (195 at 0 s, 181 at 101 s,
  after stale prices are dropped). They are a share of the later move, not a loss in bps
  and not executable economics. **An acceptable delay is not established.**
- **The scanner itself** adds about one cycle on Bybit and Binance, but sees MEXC and
  Gate pumps about half an hour late. That cause is not investigated here.
- Part A describes pumps that happened. It says nothing about the economics of early
  triggers; Part B covers that.

## Exploratory: 1-minute bursts (hypothesis only)

One descriptive family stood out: a 1-minute return of at least +5% with turnover of at
least 5x the prior-60-minute median, long. It was found among 216 cells, so it is a
candidate, not a result. A paired look on the same window (read 2 bar rules; same
firings in every column; exit fixed at the open of t+61; only the entry moves;
[script](evidence/edge-loss-decomposition-v1/read-2/exploratory_burst_paired.py.txt),
[output](evidence/edge-loss-decomposition-v1/read-2/exploratory-burst-paired.json)):

| 1m >= +5%, same firings, exit fixed            | Bybit (732)    | Binance (761)  |
| ---------------------------------------------- | -------------- | -------------- |
| Gross, entry t+1 open (mean / median)          | +123 / +50 bps | +186 / +90 bps |
| Entry t+2 open                                 | +45 / +3 bps   | +126 / +36 bps |
| Entry t+3 open                                 | +30 / -35 bps  | +95 / -11 bps  |
| Paired loss, t+1 vs t+2 (95% CI by instrument) | 79 (49 .. 108) | 60 (31 .. 91)  |

- **On Bybit most of the gross return is the first minute after the burst bar.**
  Entering one minute later, on the same firings with the same exit, loses about 79 bps.
- **Whether that first minute can be captured is unknown.** The t+1 open is the first
  trade after the bar's end. A reader of bars sees the bar 2.7 s later, and a minute bar
  cannot show how much of the 79 bps is gone by then. Only trade-level data can.
- **Caveats:**
  - one window, chosen after viewing;
  - weeks vary (Bybit ISO week 38 is negative);
  - unmeasured slippage just after a +5% minute;
  - Binance cannot be traded by the owner;
  - the route "Binance signal, Bybit execution" is not tested by either column.
- An unused variable was removed from the script after the run; the output is
  unaffected.

## What follows (proposals, for review)

1. **Seconds-level decay on Bybit's own trades (Aug-Sep, descriptive).** Measure how
   much of the first-minute move is left 0.5, 1, 3, 10 and 30 seconds after the burst
   bar ends. That sets the latency a fast path must reach, or shows none can. It
   confirms nothing about the rule.
2. **HYP-030 stays a proposal.** If step 1 leaves a capturable part, register the rule
   with:
   - the entry delay set to the measured latency of the path that would trade it;
   - real-quote costs;
   - a sealed forward accrual read after HYP-012 v2 is terminal.
3. **No architecture change follows yet.** The canary venue (MEXC versus Bybit), the
   fast path and any library change wait for step 1 and for the owner's answer on Bybit
   perpetual access.
