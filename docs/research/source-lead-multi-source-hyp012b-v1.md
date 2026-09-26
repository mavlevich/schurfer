# HYP-012b: which source venues lead Bybit (registered discovery family, v1)

Status: REGISTERED, 2026-09-26. No return from these windows had been computed when this
was written. The only reads so far were outcome-blind: event counts per source and bar
presence per candidate (`--funnel-only`).

Code: `source_lead_multi_source.py` (pure) and `source_lead_multi_source_report.py`
(CLI `source-lead-multi-source-report`).

## Why

HYP-012's discovery (2026-07-24..08-08) tested two sources. MEXC to Bybit had the largest
Holm-significant effect, 3.42% [2.04%, 5.18%], but on only N=27, and it was never followed
up. The scanner records the first-seen time of each pump on 14 venues. More leading sources
mean more tradable signals, which is the main lever for how fast a cohort accrues and for
scale.

## Family (fixed before any return)

- **Target:** Bybit, the venue the owner can trade.
- **Formal family (Holm):** BloFin, MEXC, BingX, Gate and LBank.

  The membership rule, applied to the outcome-blind holdout counts of events with a live
  Bybit perpetual, is at least 100 events, at least 30 assets, and no single ISO week above
  45%. It is applied to counts only, never to results.

- **Exploratory, no verdict:** CoinEx (week share 0.52), Bitget (97 events), Binance as a
  source (91), OKX, HTX and XT (fewer than 100). Toobit and KuCoin are also exploratory,
  because their capture only began on 2026-09-07. They are reported separately and never get
  a candidate verdict in this study.

## Candidate set (outcome-blind)

A pump event is a candidate when:

1. Its **unique** earliest source observation is a family venue. Ties within the same
   timestamp are excluded, and so are events where Bybit itself is first.
2. That observation passes the HYP-012 identity checks: swap, USDT quote and settle, base
   match, no identity conflict.
3. Bybit had a live USDT linear perpetual for the base, with `launchTime` before the signal.

**Bybit confirmation is NOT required.** HYP-012's paired design required Bybit to pump later.
For a standalone trade entered at the signal, that would condition on the future.

## Estimand

The primary estimand is the standalone long net return on Bybit, against zero:

- **Entry:** the open of the first 1-minute bar after the source first-seen time.
- **Exit:** the close of the bar that opens at entry + 30 minutes. This is the v2 exit-bar
  convention.
- **Resolution:** only the entry and exit bars are required, not every minute. The
  every-minute rule caused missingness selection before.
- **Costs:** 10 bps taker per side, the HYP-012 frozen 20 bps round-trip impact, and 5 bps per
  8 hours of funding prorated over the 31-minute hold.

**Secondary (mechanism only, never the decision):** unresolved reasons, capacity, and the
HYP-012 paired delta, which may be added in a follow-up.

## Two stages, one read each

The window uses only data never used by HYP-012's discovery, and ends before the HYP-012 v2
cohort (starting 2026-09-29):

| Stage     | Window                               | Rule                                                                                                                                                                                                                             |
| --------- | ------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Discovery | ISO weeks 33-35: 2026-08-10 .. 08-31 | Cluster-bootstrap mean and null p-value per formal venue. Holm across the 5; a venue **survives** if Holm rejects and the mean is positive                                                                                       |
| Holdout   | ISO weeks 36-39: 2026-08-31 .. 09-28 | Only survivors. Floor per venue: at least 100 **resolved** episodes, at least 30 assets, and no week above 45%, otherwise `insufficient_data`. Holm across the survivors; **candidate** if Holm rejects and the mean is positive |

- **No crossing between stages.** An episode whose exit bar crosses its stage end is excluded,
  so the two stages never share an outcome.
- **Bootstrap:** asset clusters, 10,000 iterations, seed 20260926 derived per venue, family
  alpha 0.05.
- **Write-once artifacts.** Each stage writes a write-once artifact with its SHA-256. The
  holdout refuses to run without the discovery artifact, and it reads only the discovery
  survivors.
- **Never reads the v2 window.** Nothing on or after 2026-09-29 is read by this study, for any
  venue: pumps are shared across venues, so reading another venue's October outcomes would
  unblind the v2 Gate cohort.

## Decision

- **A `candidate` venue** becomes a proposal for a new, separately registered forward cohort
  version. HYP-012 v2 is unchanged. That cohort is read no earlier than, or together with, the
  v2 formal read.
- **With no candidate,** the result goes into the discovery ledger, and no execution routing is
  built for these sources.

## Known limits

- **First-seen resolution is one scanner cycle (60 s).** Which venue counts as "first" is partly
  scan-order dependent. The signal itself (entry at detection) is unaffected.
- **Survivorship in the Bybit catalogue.** It is fetched at run time, so perpetuals delisted
  since are missing; a delisted perpetual cannot be traded now either.
- **Entry is a bar-open proxy.** A historical executable quote is not available; this is
  covered by the fixed 20 bps impact.

## Early pilot protocol v1 (execution test, registered before the holdout read)

A venue that is a holdout `candidate` **and** passes a clean shadow execution (#454) may be
given one separately authorised `LIVE_PROBE` on Bybit. The probe tests execution only:

- **Limits:**
  - one open slot, and at most USD 50 of total open notional;
  - 1x leverage and **isolated** margin, checked before every order;
  - one-way position mode;
  - at most N trades and D days, and a stop at an accumulated loss of L USD including fees and
    funding.

  N, D and L are fixed in the authorising decision before the probe starts.

- **Prerequisites:** durable order intent with `orderLinkId`, reconciliation of an unknown send
  result, partial-fill handling, reduce-only close, restart recovery, and a kill switch. These
  are separate PRs, and `LIVE_PROBE` is blocked in code until then.
- **What its PnL does and does not do:** the probe's PnL does not confirm the strategy and does
  not change any cohort verdict. More capital needs prospective confirmation.
