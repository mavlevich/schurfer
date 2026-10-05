# HYP-030 design v1: 1-minute burst on Bybit, a decision point before registration

Status: **design draft for review; nothing registered, no data read beyond the
2026-10-05 burst decay evidence.** It follows the order agreed after the decay readout:
design, power for bursts, a bounded measurement of a new path, then registration.

## The candidate rule (unchanged from the exploration)

| Element  | Definition                                                                                                                                                                                                                            |
| -------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Venue    | Bybit linear USDT perpetuals of the momentum capture universe (the owner's probable execution venue; access unconfirmed)                                                                                                              |
| Trigger  | A closed 1-minute bar with return of at least +5% over the previous close, from price-complete bars; the bar trade-complete; turnover at least 5x the median of the trade-complete bars of the prior 60 minutes (at least 30 of them) |
| Cooldown | one firing per instrument per 60 minutes                                                                                                                                                                                              |
| Side     | long                                                                                                                                                                                                                                  |
| Entry    | at the first executable ask after the signal is known (see availability below)                                                                                                                                                        |
| Exit     | one fixed hold, to be chosen below                                                                                                                                                                                                    |

**Availability.** The bar is known when the capture has it:

- 2.7 s after the close at the median and 7.7 s at p90 (measured 2026-09-20..27);
- plus the signal computation and the quote fetch, which are not measured yet.

Today's watch path adds a fixed 30 s settle and a 10 s poll. A new path would remove
them. Its latency distribution is the bounded measurement's job, not an assumption.

**Universe and gaps.**

- The capture's registered universe at the firing.
- A firing needs price-complete bars for t-1 and t and a trade-complete trigger bar.
- An entry or exit without an executable quote within a fixed wait is a recorded miss,
  never a substituted price.

**Portfolio limits.**

- USD 50 per position.
- At most 3 positions open.
- A firing that arrives while all slots are busy is recorded as blocked, in the
  denominator of the executable calendar.

## Power for bursts (planning, not a test)

Planning inputs:

- dispersion: the 732 frozen decay firings
  ([power](evidence/hyp030-design-v1/planning-burst-power.json), sha256 `668ff524...`;
  [dispersion by hold](evidence/hyp030-design-v1/planning-burst-hold-dispersion.json),
  sha256 `f73691d4...`);
- method: the cost/power planning code, with centered returns and whole-cluster
  resampling;
- provenance: both outputs pin the code revision, the decay firings' sha256, the read-2
  inputs' sha256 (bars verified against their manifests and pins) and the parameters.

The window is the one the cell was found in, so its means choose nothing. Only the
dispersion and the dependence are used.

| Hold   | SD per firing | Independent n, 50 bps net at 80% | Independent n, 100 bps | In-sample gross, t+1 entry (mean / median) |
| ------ | ------------- | -------------------------------- | ---------------------- | ------------------------------------------ |
| 5 min  | 576 bps       | 1,043                            | 261                    | +69 / +39                                  |
| 15 min | 829 bps       | 2,157                            | 540                    | +110 / +43                                 |
| 30 min | 1,242 bps     | 4,842                            | 1,211                  | +121 / +27                                 |
| 60 min | 1,112 bps     | 3,885                            | 972                    | +123 / +50                                 |

**Dependence (60-minute hold, simulated).**

- **By instrument:** 282 instruments, about 2.6 firings each. No inflation; the ICC is
  negative.
- **By UTC day:** a design effect of about 2. The requirements are 6,001-8,000 firings
  for 50 bps net at 80% and 1,501-2,000 for 100 bps.
- **Caveat:** the day-cluster test is anticonservative with 47 days (7.9% passes at a
  zero effect against the 2.5% target). The real requirement is at least this large.

**Calendar.** At the window's 15.6 firings per day, before any blocking:

| Hold, net effect | Firings     | Calendar                                                   |
| ---------------- | ----------- | ---------------------------------------------------------- |
| 60 min, 50 bps   | 6,000-8,000 | 13-17 months                                               |
| 60 min, 100 bps  | 1,500-2,000 | 3-4 months                                                 |
| 5 min, 50 bps    | about 1,000 | about 2 months with independence, more with the day effect |

**What is and is not known about the size of a net effect.** The table above is a set of
planning scenarios, not an estimate of any hold's economics.

- The decay readout measured the entry-delay loss for the 60-minute exit only. About
  30 bps is lost at a 5 s entry, so on the 60-minute hold about 125 - 30 - 41 = 54 bps
  of in-sample mean net remain at the middle cost scenario. That sits at the 50 bps
  point that needs more than a year to test; the median is much lower.
- For the 5, 15 and 30-minute holds the loss from a delayed entry was not computed on
  these firings, so their economics after delay and costs are **not established**.
- Slippage just after a +5% minute is not measured and is likely above the scenario's
  15 bps.

## Decision needed before any registration

The 60-minute hold, with the one in-sample estimate of net economics available, sits
where a test needs more than a year. The shorter holds could be tested sooner, but their
economics after delay and costs are not established. Neither is yet a money path.
Options for review:

1. **Park HYP-030** in the discovery ledger as `parked`. Record the decay finding and
   the power result, and spend the next effort elsewhere.
2. **A bounded path measurement first** (latency distribution, executable quotes and
   depth at entry and exit at USD 50, fees, funding), under the boundaries below. It
   turns the largest unknown, the real cost at burst moments, into a number, but only
   once the v2 blind window has ended.
3. **Register the 60-minute rule anyway** with a sealed accrual of about a year. Not
   recommended.

The recommendation is **option 2, under its own protocol, then park or re-plan**. It
reuses the Bybit capture (a read-only subscriber on its bars plus a quote snapshot at
the signal and at the would-be exit) and changes neither the v2 nor the HYP-015 path.

## Boundaries of the bounded measurement (to be fixed in its own protocol)

HYP-012 v2 is not terminal. Until it is, nothing on or after 2026-09-29 is read for
research on any venue (the v2 administrative stop's blind-window rule). The measurement
therefore splits into what may be seen now and what is stored unread.

- **Visible now (operational counters only):**
  - signal counts per day;
  - latency distributions of each stage, as times only: bar ready, signal computed,
    quote requested, quote received;
  - quote availability and age;
  - missed and blocked counts;
  - errors, reconnects and bytes.
- **Never printed, logged, plotted or summarized before the blind window ends:** any
  price, spread, depth, size, side or return.
  - Quote snapshots and the exit snapshots are written to sealed files with a manifest
    and sha256 and not opened.
  - The health endpoint and any daily summary carry counters only; a test asserts this.
- **After v2 is terminal:**
  - The sealed data may be read only by a study registered after the terminal state,
    under that study's protocol. For that study it is historical (descriptive), never
    prospective.
  - v2's closed windows are excluded mechanically: by canonical asset, or by base ticker
    when no mapping exists, over any window a firing uses (lookback, entry, hold,
    exit). Excluded firings are dropped, never imputed.
  - Closed windows exist only after an administrative stop. If v2 ends by its completed
    formal read instead, the blind window ends under that rule, and rule 3 of the stop
    document (data on or after 2026-09-29 only for a study registered after the
    terminal state) still applies.
- **A confirmatory HYP-030 cohort**, if one is ever justified:
  - registered separately after the measurement's readout;
  - collected on a forward window that starts after that registration;
  - never including the measurement's own data.
- **Scope:** read-only towards the existing capture and the database tables of v2 and
  HYP-015. Its own container and limits; no order is ever sent.

## Not in this design

- Binance (not tradable for the owner) and the route "Binance signal, Bybit execution".
- Any second rule or threshold.
- Any change to the old scanner, watch or paper paths.
