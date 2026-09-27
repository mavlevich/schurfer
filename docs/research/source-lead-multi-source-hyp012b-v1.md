# HYP-012b: which source venues lead Bybit (registered discovery family, v1)

Status: REGISTERED, 2026-09-26, amended the same day after review and still before any
read (see "Amendment A1"). No return from these windows had been computed when this was
written or amended. The only reads so far were outcome-blind: event counts per source and
bar presence per candidate.

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
  because their capture only began on 2026-09-07. They are reported at discovery only, with
  no verdict, and never get a candidate verdict in this study. Their holdout outcomes are
  never computed, so that window stays unread for a later registration.

## Candidate set (outcome-blind)

A pump event is a candidate when:

1. Its **unique** earliest source observation is a family venue. Ties within the same
   timestamp are excluded, and so are events where Bybit itself is first.
2. That observation passes the HYP-012 identity checks: swap, USDT quote and settle, base
   match, no identity conflict.
3. **Exactly one** Bybit USDT linear perpetual for the base was live over the whole episode:
   `launchTime` before the signal, and `deliveryTime` either unset or after the exit bar
   closes. The catalogue is fetched with every status (Trading, PreLaunch, Delivering,
   Closed), so perpetuals delisted since the window still count. No live contract is
   `bybit_not_live_over_episode`; two are `ambiguous_bybit_route`. Neither is guessed.
4. **Route price filter (pre-signal).** Bybit is matched to the source by base ticker, and
   the same ticker can be a different project. The source's first observed price must be
   within a factor of 2 of the close of the last Bybit minute that ended at or before the
   signal (the minute before the one holding it). A bar's open is not used: Bybit's
   `startTime` is the start of the candle, not the time of its first trade, so the open of
   the minute holding the signal can come after the signal. A live router can apply the
   same rule. The filter removes gross mismatches and does not prove identity. Otherwise the
   episode is excluded as `route_identity:price_level_mismatch`, `no_source_price` or
   `missing_reference_bar`. Exclusions are counted in the funnel.

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

| Stage     | Window                               | Rule                                                                                                                                                                                                                                                                                                                                                                                                   |
| --------- | ------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Discovery | ISO weeks 33-35: 2026-08-10 .. 08-31 | Cluster-bootstrap mean and null p-value per formal venue. Holm across the 5; a venue **survives** if Holm rejects and the mean is positive                                                                                                                                                                                                                                                             |
| Holdout   | ISO weeks 36-39: 2026-08-31 .. 09-28 | Only survivors. Floor per venue: at least 100 **resolved** episodes, at least 30 assets, and no week above 45%. At or above 100 resolved with a mean at or below zero it is `fail` even below the floor, with the missed criteria recorded. Otherwise below the floor it is `insufficient_data`. Holm across the survivors that meet the floor; **candidate** if Holm rejects and the mean is positive |

- **No crossing between stages.** An episode whose exit bar crosses its stage end is excluded,
  so the two stages never share an outcome.
- **Bootstrap:** asset clusters, 10,000 iterations, seed 20260926 derived per venue, family
  alpha 0.05.
- **Maturity.** A stage is refused until its window end plus one day (late rows, final
  bars). The holdout can run from 2026-09-29 00:00 UTC; the discovery can run now.
- **Frozen inputs, then a claim, then one read.** Phase 1 (`prepare`) writes `inputs.json`
  once, with its SHA-256. It holds the raw Bybit catalogue, a hash of the loaded event ids,
  the funnel, the candidates after the route identity check, and their raw 1-minute klines.
  No return is computed. Phase 2 (`read`) first creates `claim.json` in the persistent prod
  research directory. The claim pins the inputs hash, the ordered tested family, and at
  holdout the discovery result hash. Only then does it compute `result.json`, from the
  stored inputs alone. A completed read is never repeated. A claim without a result (a
  crashed read) resumes only when it pins exactly the same inputs, family and discovery
  result; otherwise it is refused. So a replay gives the same bytes, and nothing is fetched
  again after the claim.
- **Crash safety.** Every file is published atomically: written to a temp file, fsynced,
  then hard-linked to its name, which fails if the name exists. A crash leaves no file or
  a whole one, never a partial one. A crash between a file and its `.sha256` is finished
  on the next run. The inputs digest is taken from the whole file. A result digest is
  written only if the stored result equals its replay from the claim and the same inputs;
  otherwise the run refuses. The claim is a file rather than an `app.formal_read_claims` row because the
  run is a one-shot job on the single prod host that owns this directory. For this study
  that gives the same guarantee without depending on the unmerged v2 claim stack.
- **Holdout scope.** The holdout inputs are prepared for every family venue, so they never
  depend on a discovery outcome. The holdout read refuses to run without the discovery
  result (checked against its SHA-256). It computes outcomes only for the discovery
  survivors: candidates of every other venue, formal or exploratory, are filtered out
  before any outcome is computed.
- **Never reads the v2 window.** Nothing on or after 2026-09-29 is read by this study, for any
  venue: pumps are shared across venues, so reading another venue's October outcomes would
  unblind the v2 Gate cohort.

## Decision

- **A `candidate` venue** becomes a proposal for a new, separately registered forward cohort
  version. HYP-012 v2 is unchanged. That cohort is read no earlier than, or together with, the
  v2 formal read.
- **Only confirmed routes are ever traded.** Any cohort, shadow or pilot order for a
  candidate venue needs the source-to-Bybit route confirmed per asset by an identity rule
  (the v4 registry rule or a versioned equivalent for that source). A ticker match plus the
  price band is enough for this historical estimate, never for an order. A historical
  `candidate` is therefore a hypothesis about confirmed routes, to be tested on them. It
  is not evidence of the economics of confirmed routes.
- **With no candidate,** the result goes into the discovery ledger, and no execution routing is
  built for these sources.

## Known limits

- **First-seen resolution is one scanner cycle (60 s).** Which venue counts as "first" is partly
  scan-order dependent. The signal itself (entry at detection) is unaffected.
- **Identity is a price-level filter, not a contract-address match.** Full address
  confirmation (the v4 registry rule) needs per-venue asset data that most of these sources
  do not publish historically. A same-ticker project that passes the 2x band stays in the
  sample. Its return is not tied to the source pump, but it is not guaranteed to be neutral
  either: two assets can rise together with the market. So the historical estimate can be
  biased in either direction by such routes. The binding restriction is in the Decision
  section: trading needs a confirmed route.
- **Delisted before the run.** Delisted perpetuals are in the catalogue. If Bybit no longer
  serves a delisted contract's klines, those episodes resolve as missing bars and are
  reported as unresolved.
- **Bybit ticker renames and `1000`-prefixed contracts** never match a plain base, so they
  are excluded as `no_bybit_perp`. This is conservative and costs some episodes.
- **Entry is a bar-open proxy.** A historical executable quote is not available; this is
  covered by the fixed 20 bps impact.

## Early pilot protocol v1 (execution test, registered before the holdout read)

A venue that is a holdout `candidate` **and** passes a clean shadow execution (#454) may be
given one separately authorised `LIVE_PROBE` on Bybit. Orders go only to routes confirmed by
an identity rule (see Decision). The probe tests execution only:

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

## Amendment A1 (2026-09-26, after review, before any read)

Versioned changes to v1, all made before any return was computed. `FAMILY_VERSION` stays
`hyp012b_multi_source_lead_v1` because no read happened under the earlier text.

1. **Route identity:** single live Bybit contract over the episode, with ambiguity
   excluded; the pre-signal price filter on a minute closed at or before the signal;
   trading only on confirmed routes.
2. **Delisted contracts** are included via every catalogue status.
3. **Frozen inputs, a durable claim, and a replay** from stored inputs only. The claim
   pins the inputs, the ordered family and the discovery result. Every file is published
   atomically, with crash completion. The holdout computes survivor outcomes only.
4. **The maturity guard**, with the holdout from 2026-09-29.
5. **Verdict order:** a mature non-positive holdout mean is `fail` before the
   diversification floor.
6. **Pilot authority is separate from HYP-012 v2.** The early pilot above is authorised by a
   HYP-012b holdout `candidate` for that venue, under this document's own rule. It neither
   uses nor changes the HYP-012 v2 rule, which still requires a v2 `candidate` for any v2
   execution test. Neither study's verdict authorises trading on the other's routes.
7. **The discovery window is 3 weeks, not the 4 discussed.** Between the end of the HYP-012
   discovery (2026-08-08) and the v2 start (2026-09-29), there are 7 full ISO weeks. The
   holdout kept 4 of them, because it decides; the discovery took the remaining 3. This is a
   deliberate change from the discussed 4 + 4 plan, fixed before any read.
