# New-perp short, laboratory protocol v1 (DRAFT, revision 10, 2026-11-17)

Study identifier: `new-perp-short-v1`. Idea: `I25` in the
[consilium ideas list](../consilium/ideas.md). A hypothesis identifier (`HYP-039` is the
next free number at the time of writing) is assigned only when the contract is
registered (stage 2, below).

Status: **DRAFT protocol for the colleague's design discussion. It is a design document
only.** It reads no data, fetches nothing, registers nothing, authorizes no order and
changes no code. The checks it describes (launch-time meaning, catalog against trade
archive, identity per launch, power calibration, the laboratory read) are written as
**methods with pass and fail criteria and status codes**; each is run later, on the
owner's separate go. Merge of this document is also a separate decision of the owner
and the colleague.

**Revision 10** follows two reviews of the experiment model, a consistency pass over the whole chain
(selection, entry, events of an open position, exit, funding and fees, verdict) and six reads of
the rules by the colleague (the sign map, the trigger window and the margin-loss and cost wording;
then the gap rule, the portfolio timing of unresolved episodes, the entry clock and the six-part
decomposition; then the `EVT` of a stop, the mark-price test in a zero-trade minute and the dating
of a confirmed event against a gap; then the lookback cut, the funds check, funding coverage as
a selection condition, the `exec_net` guards, a missing entry bar and the benchmark end of an
unresolved episode; then the cash ledger and the four money paths; then the known cash flows of an
unresolved episode in L). What changed
in each revision is in section 13; the rules are pinned by the worked examples of Appendix A.

Chain of evidence behind it:

- the count of new Bybit linear USDT perpetuals passed its planned gate: 263 launches
  over 53 ISO weeks in the discovery year, 254 in the backward holdout
  ([result](catalyst-feasibility-v1-result-2026-11-16.md), section "New perp launches"),
  from the `instrument_launches` field of the
  [report](evidence/catalyst-feasibility-v1/report.json.gz);
- that count read no price. It measures the coverage of Bybit's API catalog, not a
  verified full history, and `launchTime` is not shown to be the moment trading opened
  (the limits listed there are carried into this protocol as checks, section 4);
- the idea's own paper screen ([A, 2026-11-15](../consilium/screens/a-2026-11-15.md))
  gave a working hurdle above 41 bps plus funding paid and demanded a fixed protocol
  before any price, with at most three variants. This document follows that.

This protocol is not a claim that a short of new perps has an edge. The mechanism is
widely known and the market has a price for it: funding. The edge exists only where the
drift exceeds funding, costs and the squeeze risk, which is a narrower claim than "new
listings fall".

## 1. Question

Can a short of a newly launched Bybit USDT perpetual, opened a fixed time after launch
and held a fixed time, make a **net** return that is positive on all reserved capital
after costs from real execution, funding actually accrued, the cost of the right-tail
risk rule and the 5% annual hurdle, and is it **testable at all** with about 263
launches that are not independent?

Two answers are wanted, in this order, and the order is the point of the document:

1. **Is a correctly testable candidate there?** (stages 1 to 3: reconstructibility,
   identity, coverage, capital feasibility, power). Most of the stop codes in section 8
   can end the study here, **before any return is read**.
2. **If yes, what does the laboratory say?** (stage 4) and where could a positive
   laboratory result be confirmed (section 9).

The study does not look for a positive result at any cost. A stop at stage 1 to 3 is a
useful, cheap result.

## 2. Windows and what each may do

| Window                                              | Dates (UTC)              | Role here                                                                                                                                                                                                                  |
| --------------------------------------------------- | ------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Discovery year                                      | 2024-10-01 to 2025-09-30 | open, used for development; the laboratory. A result here is a hypothesis for confirmation, never evidence by itself                                                                                                       |
| Backward holdout                                    | 2023-02-01 to 2024-09-15 | one shared single shot (section 9). In this protocol it is read only at **coverage level**, only by stage 1b, after stage 1 passes and on a separate go; its ticker list stays sealed outside Git as in the catalyst study |
| HYP-031 test year                                   | 2025-10-01 to 2026-09-28 | excluded and unassigned. Not used here without a separate reassignment (section 9)                                                                                                                                         |
| HYP-012 v2 blind window                             | from 2026-09-29          | closed to every price read. Nothing here touches it                                                                                                                                                                        |
| Anything else (before 2023-02-01, 2024-09-16 to 30) | -                        | discarded                                                                                                                                                                                                                  |

**Path cut at the window end (fixed now).** An episode belongs to a window only if its
**whole path**, from `T0` (the first public trade, section 4 M1) to the planned exit `X` of the variant,
lies inside the window. A
launch whose path would run past the window end is `window_censored`: counted, listed
with its reason, never priced. For the discovery year the last eligible launch is
2025-09-16 for the primary variant (7 d age plus 7 d hold) and 2025-09-09 for the
longest (14 d plus 7 d). This keeps every price read out of the HYP-031 test year and
the blind window, at the cost of about 5 to 12 of the 263 launches. The same cut applies
at 2024-09-15 for the holdout. The cohort is not extended to compensate.

**Lookback cut (fixed now).** No feature, benchmark selection or pool membership may read
**any data dated before the start of the window the episode belongs to** (discovery year:
2024-10-01; holdout: 2023-02-01). The protocol has features with a 30-day lookback from
`E`: BTCUSDT's 30-day return of the regime view (7.2) and the 30-day turnover that
selects the liquid pool (7.3). An episode whose lookback would start before the window
start is **`lookback_censored`**: counted and listed with its reason, **not in the primary
sample**, never priced. **This does not widen data access**: it keeps the reads inside the
window, and the shorter lookback of an early launch is never substituted. A 30-day lookback
starts at `E - 30 d`, so an episode is eligible when `E - 30 d >= window start`, that is
`T0 >= (window start + 30 d) - age`: for the discovery year **`T0 >= 2024-10-24` for `V1`**
(age 7 d), `T0 >= 2024-10-28` for `V2` (age 3 d) and `T0 >= 2024-10-17` for `V3` (age 14
d). Each variant is cut by its own age; `V1` decides, and the shape across age is
reported on the common set `T0 >= 2024-10-28`. The same cut applies to the holdout from
its own start (stage 1b).
**Planning guess, not a measurement:** the window of cut launch dates for `V1` is the 23
days 2024-10-01 to 2024-10-23, and if launches arrived evenly that would remove about
`263 * 23 / 365`, **about 17**, launches (arrivals are not even; the true number comes
from metadata at stage 1). The planned count of resolved episodes falls from about 220 to
**about 205** (section 7.4). The primary count is

```text
n_primary = launches - window_censored - lookback_censored - t0_ambiguous
            - identity exclusions - funding_uncovered
```

and if `n_primary` is below 100 episodes or 26 ISO weeks the stop is
`insufficient_events` (section 8). **The lookback is never shortened to rescue the
count.**

## 3. Cohort

- **Unit:** one launch is the first Bybit linear USDT perpetual of a base asset (after
  stripping a multiplier). Every launch is its own base (263 of 263 in the count).
- **Source:** Bybit `instruments-info` for `Trading`, `Delivering` and `Closed`, by
  `launchTime`, as counted. Delisted perps are included, so survivors are not the only
  cohort. Whether the catalog really holds all delisted perps is check M2.
- **No entry filter.** Every resolved launch is entered. No signal, no price filter, no
  funding filter, no market filter, no size filter. A filter would be a new variant and
  needs a new registration. This is what keeps the protocol testable: it is a calendar
  event, not a pattern.
- **No outcome-dependent exclusion.** Inclusion is decided from information at or
  before `E` and from non-price identity evidence (section 4). Nothing the price does
  after `E` removes an episode: a halt, a delisting, a squeeze or a crash is an outcome.
- **What "new" means.** A new **perp** is not always a new **token**. Many launches are
  of assets that have traded on spot for months. The late-buyer and unlock-seller
  mechanism fits a new token best. The cohort is therefore recorded with the base's
  prior Bybit spot history (check M3), and token newness is a **pre-declared diagnostic
  stratum that gates nothing** (see question Q3).
- **Pre-market.** A perp may trade before its regular launch, in a pre-market phase.
  Whether `launchTime` marks that start or the regular launch is the core of check M1.
- **Counts that matter.** The 263 is an upper bound. Plan with about **205** resolved
  episodes after the end cut, the lookback cut (about 17, section 2), identity and
  coverage losses (a planning number, not a measurement; it was about 220 before the
  lookback cut).

**Selection and entry statuses.** Each is counted with its reason, none is priced, and
none depends on a return. They are decided from dates, catalog fields and the **existence**
of data, never from its content.

| Status                       | Decided from                                      | Effect                                                                                                                                             |
| ---------------------------- | ------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| `window_censored`            | `T0`, planned `X`, window end (section 2)         | counted, never priced                                                                                                                              |
| `lookback_censored`          | `T0`, age, window start (section 2)               | not in the primary sample                                                                                                                          |
| `t0_ambiguous`, identity     | M1, M3                                            | excluded before entry                                                                                                                              |
| `funding_uncovered`          | funding-history coverage listing (M4)             | excluded before entry; share at most 5% (M4)                                                                                                       |
| `entry_unavailable`          | **existence** of the trade bar that starts at `E` | no position modelled; counted in the M5 coverage share and the 5% gate of section 8; never scored, never in the denominator of net                 |
| `skipped_insufficient_funds` | cash at `E` in that scenario's portfolio (5.4)    | not entered in that scenario's portfolio, in L and H separately; counted with its reason; the per-episode statistics of section 7 are not affected |

## 4. Verification methods, before any return (stage 1)

These are **methods with criteria**. None is executed by this document. **Stage 1
runs on the discovery year only**, needs the owner's separate go and a registry row, and
is a data-recoverability check: launch semantics, coverage, and identity by independent
evidence. The backward holdout is touched only by **stage 1b**, after stage 1 passes and
on a further separate go, with a sealed output (aggregate counts and hashes). The checks
read **timestamps, file lists, catalog fields and announcements**, never returns.

**Principle: no outcome-dependent exclusion.** Whether an episode is in the sample is
decided only from information dated at or before its entry time `E`, plus non-price
identity evidence that is **also dated at or before `E`**. Nothing the price did after
`E` may remove an episode, and neither may non-price information that becomes known
after `E`: an announcement on day 10 about a trade opened on day 7 describes an
already open position. Such an event is scored in the result (section 5.3), not deleted. A jump, a crash
or a squeeze after entry is exactly what the strategy must survive, so a price move
alone never removes a trade. A halt or a delisting after `E` is an outcome of the
episode, handled in section 5.3, not an exclusion. **The only data-availability
exclusions** are `funding_uncovered` and `entry_unavailable` (section 3): each is decided
from the existence of data and never from a price, each is counted, and both are gated
(5% each, M4 and M5). Their limit is survivorship: missing data can correlate with young,
thin or delisted symbols (M4).

### M1. What `launchTime` means versus the first public trade

Lifecycle timestamps are distinct and are kept distinct: instrument creation, announced
listing, `launchTime` (catalog field), first public trade, first 1-minute bar with
volume, first usable order book, suspension, resumption, delivery or delisting. A
conveniently named field is not evidence that trading opened then.

- **Method.** For each launch in the discovery year, take the first public trade
  timestamp from the Bybit trade archive (the first file of the symbol and its first
  row) and the first 1-minute bar with non-zero volume. Compute `d = first_trade -
launchTime`. Read the catalog's pre-launch indicators if the endpoint returns them for
  closed instruments, and record when it does not.
- **Classes of `d`.**
  - `aligned`: first trade within [-5 min, +60 min] of `launchTime`;
  - `delayed_open`: first trade later than +60 min and up to +24 h;
  - `premarket_or_ambiguous`: first trade earlier than -5 min, or later than +24 h.
- **Pass.** `premarket_or_ambiguous` is at most 10% of the launches counted in the
  window, and `aligned` plus `delayed_open` is at least 90%.
- **Fail code.** `not_reconstructible` with reason `launch_time_unverified`.
- **Consequence for the clock.** The age at entry is counted from **T0 = the first public
  trade**, which is known at the entry time (point in time, past by day 3). `launchTime`
  is kept as the catalog reference and as the cohort key. An episode in
  `premarket_or_ambiguous` is `t0_ambiguous`: it has no `T0`, is **not entered**, is excluded at
  selection, before any entry, and is counted with its reason in the M1 gate. It is not part
  of the `unresolved after E` gate of 5.3, and it is never re-anchored after seeing a price.

### M2. Reconciliation of the instruments-info catalog against the public trade archive

The catalyst study states that the archive was not reconciled. This check does it.

- **Method.** For the window, build two sets by symbol: **catalog** (launches by
  `launchTime`) and **archive** (symbols whose first daily file falls in the window,
  from file listings, no content). Compare by exact symbol and by date. Also record, per
  catalog symbol, the number of days in its span with no archive file.
- **Pass.**
  - catalog symbols with no archive file within 1 day of `launchTime` are at most 5% of
    the catalog set;
  - archive symbols that are absent from the catalog (typically delisted symbols that
    `Closed` no longer returns) are at most 5% of the union;
  - symbols missing more than 5% of the days of their planned path are at most 5% of the
    catalog set. A missing last file is **not** taken as an end of trading: the days
    after it are a data gap unless a separate source confirms an end dated at or before the first missing minute (5.3, items 3 and 4).
- **Fail code.** `incomplete_coverage`, with the three shares named. An archive-only
  symbol is **never** added to the cohort by hand after the check: the cohort is the
  catalog as registered. The share is reported because delisted perps are the likely
  best outcomes of a short, so their absence biases the result **against** a short, in
  a way that is visible but not repairable here.
- **Known unknown.** The trade archive held 1,130 USDT symbols on the 2026-10-30 check,
  against 715 distinct catalog symbols. The gap is expected to come from symbols
  launched after 2025-09-30, which the fetch dropped by design, and from dated futures;
  it is not verified. M2 is exactly where it gets resolved, inside the window only.

### M3. Identity per launch, by independent evidence known at entry

Under the [identity cases](instrument-identity-cases.md), a same-ticker match is not
proof of the same asset. Identity is judged **only by evidence that is not a price**:
catalog fields (symbol, base coin, contract size and quantity filters, where snapshots
exist), Bybit announcements with a machine publication time, the trade-archive file
list, and the Bybit spot catalog. A price series is never evidence of identity.

**Timing rule.** Evidence counts only if it was **published or dated at or before
`E`**, the entry time of the episode (known-at-entry evidence). Evidence that appears
after `E` is never used to exclude the episode; it is an event of an open position and
is scored (section 5.3, item 4). Each launch receives one status:

| Status                   | Meaning (evidence known at `E`)                                                                                                                                   |
| ------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `resolved`               | one catalog symbol for the base at `E`, no earlier Bybit linear perp of the same base, no contradicting evidence                                                  |
| `relist`                 | the same base had an earlier Bybit linear USDT perp (any time, including before 2023); the launch is not new                                                      |
| `multiplier_ambiguous`   | two catalog symbols of one base existed at `E` (for example a `1000` prefix and a plain ticker)                                                                   |
| `ticker_reuse_suspected` | a catalog row whose `launchTime` is months later than the symbol's first archive file, or a file-list gap longer than 30 days before the launch                   |
| `change_known_at_entry`  | a machine-timed announcement **published at or before `E`**, or a catalog field at `E`, of a symbol, contract-size or multiplier change effective inside the path |
| `identity_unverifiable`  | no independent source can speak for this launch (for example its announcements are untimed). **The episode stays in the sample** and is counted                   |

- **Pass.** `identity_unverifiable` is at most 20% of the launches that survive M1 and
  M2, the four contradicting statuses (`relist`, `multiplier_ambiguous`,
  `ticker_reuse_suspected`, `change_known_at_entry`) together are at most 10%, and at
  least 100 episodes remain.
- **Fail code.** `identity_unresolved`.
- **Per-launch decision.** `resolved` and `identity_unverifiable` launches are traded in
  the primary read. The four contradicting statuses are excluded from the primary
  cohort **before entry**, on evidence available at `E`, and counted by status.
  `relist` is not a new perp.
- **After `E`: events, not exclusions.** A contract conversion, symbol migration,
  multiplier change, halt or delisting that becomes known after `E` is an
  **instrument event after entry**. The episode stays in the sample and is scored by
  the scenario rule of 5.3, item 4. It is never deleted afterwards.
- **Known limit.** Catalog fields exist as a current snapshot, and the announcement feed
  that carries contract changes (`product_updates`) has no machine publication time. A
  change that no source dates stays in the sample as an ordinary episode. This is
  accepted as a stated limit and is not repaired from prices.
- **Price diagnostic, outcome-level, gates nothing.** At stage 4 only, the report lists
  episodes whose 1-minute close ratio between consecutive bars falls outside [1/8, 8]
  after the first hour, with their share and their contribution to the result. It is
  labelled **outcome-level** (it reads prices), prompts a later evidence search, and
  is **never used to exclude an episode from the result**.
- **Diagnostic recorded, gates nothing.** For each resolved base, the Bybit spot
  `launchTime` if any and the age of the spot market at the perp launch. This is the
  token-newness stratum of section 3.

### M4 to M6. Coverage of what the net needs (also before any return)

| Check | Method                                                                                                                                                                                                                                                                                                                                                                                                                            | Pass                                                                           | Fail code             |
| ----- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------ | --------------------- |
| M4    | Funding history coverage proven by pagination for the span **days 3 to 21 after `T0`** (the union of the planned paths of every variant) of every episode (the HYP-036 rule: a full page covers only from its oldest row; a span not inside one contiguous union of covered spans is incomplete). **A selection condition:** an episode that fails it is `funding_uncovered`, decided from metadata at stage 1, before any entry. | the `funding_uncovered` share is at most 5% of the episodes otherwise eligible | `incomplete_coverage` |
| M5    | 1-minute trade bars, 1-minute mark-price bars and hourly mark bars at settlements, listed by existence (no content), over the planned path of every variant (days 3 to 21 after `T0`). A missing last file is **not** an end of trading: bars missing after the last archive file are a data gap unless a separate source confirms an end dated at or before the first missing minute (5.3, items 3 and 4).                       | at least 95% of episodes have their bars and mark bars                         | `incomplete_coverage` |
| M6    | Static capital feasibility known at `E`: contract minimum order value, quantity step, maximum leverage, risk limit and position cap of the symbol, from the catalog snapshot, for notional USD 50 per slot.                                                                                                                                                                                                                       | the order opens for at least 90% of episodes                                   | `capital_infeasible`  |

**Funding coverage is a selection condition (M4).** An episode whose funding history does
not cover its planned path in one contiguous union is `funding_uncovered`, excluded before
entry, counted, never entered. **Unknown funding is never taken as zero**, and no rate is
imputed. **Limit, stated plainly:** coverage can correlate with young, thin or delisted
symbols, so dropping uncovered episodes can drop exactly the launches that failed. The
share by launch month and by status (delisted or not) is reported as a diagnostic, and a
share above 5% is `incomplete_coverage`, not a smaller sample.

**A missing entry bar (M5).** If the 1-minute trade bar that starts at `E` is missing, the
episode is **`entry_unavailable`**: `P_e`, `F_e`, `q`, the fee and the margin are
undefined, so **no position was modelled**, the slot is never taken, and the episode is
**never scored** and never in the denominator of net. It is decided from **bar existence
only, not from prices**, and it is counted in the M5 coverage share and in the 5% gate of
section 8 (example A9).

**Amendment 1 (2026-11-17, stage 1 contract, with the owner's and the colleague's
decisions).** The stage 1 evidence for M4, M5 and M6 is fixed as follows. It deviates from the
text of the M4 to M6 table above, which is why it is written here and not left to the request
list.

- **M5 at stage 1** proves (a) the 1-minute trade bar that starts at `E` exists (else
  `entry_unavailable`), and (b) **hourly** trade and mark-price series are **continuous** over
  the V1 path, from the hour of `E` to the hour before `X`: a missing hour inside the path is a
  gap, a series that ends early is a gap unless a catalog delivery time at or before the first
  missing hour confirms the end, and a page that may be cut by its row limit proves nothing.
  **Continuity at minute level over the whole path is proven at stage 4**, when those bars are
  fetched; the gap rule of 5.3 is unchanged. Stage 1 does not request minute bars.
- **M4 at stage 1** is a **coarse check, not proof**: finishing the pagination is not enough,
  and coverage passes only if the funding timestamps form one chain with no step above 8 hours,
  start at most one step after the span start and end at most one step before its end (or the
  catalog delivered the symbol). It is labelled "no gap longer than 8 h". It does not show that
  every settlement is present when the real interval is shorter than 8 hours, and the catalog
  `fundingInterval` is a current snapshot, so the report counts those launches apart.
- **M6 at stage 1 is a partial check** on catalog fields only. It can fail an order, and it
  never shows that an order opens: no price, quantity rounding, risk limit or depth is checked.
- **Not read at stage 1:** order-book existence (item 7 stays a later decision), the spot
  catalog, announcements, and the first bar with volume: `T0` is the first public trade.
- **M2, second share** is assessed only for archive directories that are absent from the catalog
  and whose last file is inside the window. A directory whose date is unknown or later is not
  kept, because the cut forbids recording a launch date of the test year. It is a lower bound
  and is **never proven**: it can fail the gate, not prove it.
- **Stage 1 runs as two gos:** metadata only (catalog, archive names, listings, file sizes), then
  first files, entry bars, hourly series and funding after the owner reviews the manifest of
  the first. See the [stage 1 request list](new-perp-stage1-request-list-v1.md).

**Proposed Amendment 2 (2026-11-19, separate M2(ii) metadata contract).** The
[M2(ii) supplement](new-perp-m2ii-supplement-v1.md) can assess the observed
archive using first daily-file dates rather than a root modification date.
It binds the completed run's frozen 263 catalog symbols; a fresh catalog is
not subtracted. Unknown names stay in RAM until a pre-cut file admits the name.
Before admission only opaque operational attempt/byte accounting is retained,
without names, URLs, raw-body hashes, schema keys or post-cut launch metrics.
This is a separate capped go, not a continuation of either original go.
Incomplete scans remain incomplete. The original report stays immutable; the
companion preserves M4/M5/M6 and the remaining partial limits. Observed archive
coverage never proves all market launches or historical instrument identity.
This proposal authorizes no read.

**Proposed Amendment 3 (2026-11-19, M2(ii) filename identity).** Supplement v2
recognizes dated CSV-gzip filenames independently of their containing directory.
A pre-cut foreign prefix never establishes shared identity: retain only an opaque
unresolved issue, continue the metadata scan, and block a positive M2(ii) verdict.
Mixed-prefix directories do not admit even their matching-prefix dates. Valid
post-cut-only files retain no identity, date or scientific count. The exact share
is unavailable while identity issues remain; a known matching-prefix numerator
above 5% still fails. Scan completion is separate from identity-check completion.
This proposal changes no episode selection and authorizes no read or reuse of
stopped roots. Review, merge and an owner go for the new hash are required.

**Proposed Amendment 4 (2026-11-19, fresh M2(ii) v3 filename cohort).** The
[new v3 contract](new-perp-m2ii-file-prefix-v3.md) measures only the
`observed_pre_cut_file_prefix_cohort`. A directory must have exactly one
canonical pre-cut filename prefix, absent from the frozen 263 and unique across
all unknown directories. Mixed/noncanonical prefixes and any cross-directory or
frozen-catalog collision block a positive result without retaining blocked names.
Canonical prefixes inside mixed directories participate in collision detection.
Admission is delayed until the complete snapshot; no normalization, merge or
inferred rename is allowed. Directory names are retained only when equal to the
admitted prefix. Post-cut names/dates/counts disappear before scientific counters.
The first pre-cut file day defines the observed series; pre-window series remain
outside the numerator even if they continue into the window. The threshold stays
`a/(263+a) <= 0.05` (13 passes, 14 fails), with explicit partial-archive labels and
no claim of actual instrument identity or all market launches. Other Stage 1
checks and the primary cohort are unchanged. Old reports remain immutable and
incomplete. This proposal requires a fresh full HTML snapshot, review, merged code
and a new owner go; it authorizes no request or repetition of CSV/Go 1/Go 2.

**Proposed Amendment 5 (2026-11-19, sealed Stage 1b metadata only).** After
discovery's reviewed `stage1_pass_partial`, the
[metadata proposal](new-perp-stage1b-metadata-v1.md) implements C1/C3/D1/D2h only
for `[2023-02-01, 2024-09-16)`, with a sealed cohort/manifest and aggregate output.
It grants no request and no body/coverage or exam read. All in-window catalog rows
remain in N even when their names cannot be admitted; missing archive evidence is
not removed from coverage/identity counters. Prior catalog names/bases and early
filename dates are RAM-only predicates. A first file before the window remains
`t0_pre_window_unreadable` only when its day does not already prove ambiguity;
known ambiguity retains `t0_ambiguous`. No HEAD/CSV access outside the window.
Safe catalog labels retain only their in-window file-day calendar for M2(iii).
A proven M2(i) missing-share lower bound is not hidden by a later HTTP stop when
the catalog denominator and source integrity remain valid. Catalog
and unknown folders share the strict parser; all canonical catalog-folder
pre-cut prefixes protect the unknown-folder collision set. X is an exit-bar end,
so only `X > cut` censors it, while start timestamps remain below cut. Discovery
code, contracts, reports and partial checks are unchanged. The metadata proposal
requires its own code/contract review, merged clean code, registry row and owner
go for its exact new hash/caps. A body queue and budget need a later separate go.

**Order books are not a gate.** Existence and depth of the order-book archive are
**recorded, not required**: the share of episodes with a book at entry and at exit, at
each order size, is reported. It decides only which cost source scenario L uses for
each fill (section 6.2); it does not stop the study. The archive's status as Bybit's
official source is still to be confirmed (registry, coverage facts of 2026-10-30), and
that confirmation is part of stage 1. If it is not confirmed, book coverage counts as
zero and scenario L uses its fixed allowance everywhere, which the report states.

## 5. Trade definition (fixed before any return)

All times UTC. Prices are fractions, results are bps of notional.

### 5.1 Entry age and holding grid

**Three variants, no more.** They differ only in the age at entry; the hold is fixed.
(Why the age and not the hold: the age is what the idea is about; a hold grid would
multiply variants on one path.)

| Variant | Entry age from T0 | Hold   | Role                                                |
| ------- | ----------------- | ------ | --------------------------------------------------- |
| `V1`    | 7 days            | 7 days | **primary. The only variant that can decide**       |
| `V2`    | 3 days            | 7 days | sensitivity: earlier, launch-day noise partly gone  |
| `V3`    | 14 days           | 7 days | sensitivity: later, more of the unlock selling done |

- **Why 7 and 7.** A week is long enough that fees of about 20 to 25 bps plus spread are a small part
  of a plausible drift; it is short enough that the squeeze exposure and the overlap
  between episodes stay bounded. The first days (extreme spreads, pre-market conversion,
  launch-day volatility) are skipped on purpose. The values are chosen by mechanism and
  by cost, not by any price. The primary is the middle of the grid so that no end of the
  grid can be picked after the read.
- **V2 and V3 do not decide.** They are reported to show the shape across age. A pass
  of V2 or V3 with a failing V1 is recorded as a hypothesis and decides nothing. They
  are counted in the [discovery ledger](discovery-ledger.md): +3 variants.
- **Excluded variants** (each needs a new registration): any hold other than 7 days;
  filters on the price since launch, on funding level, on spot age or on market state;
  maker entries; scaling in; trailing stops.

### 5.2 Entry, planned exit and the actual exit

- **Clock.** `T0` is the **exact time of the first public trade** (to the second or finer,
  as the archive gives it). The entry time is `E = T0 + age` **rounded up to the next whole
  minute** (unchanged if it is already on a minute), and `X = E + 7 days`, so `E` and `X`
  are whole minutes. Every rule below, the path cut of section 2, the funding window
  `E < s < t_exit`, the benchmark interval and the slot timing, uses these `E` and `X`
  (example A3, clock check).
- **Entry:** a taker sell at the 1-minute bar that starts at `E`. The
  quantity `q = N / F_e` is fixed at entry, rounded down to the contract's quantity
  step (checked in M6). Reference price `P_e` is that bar's open.
- **Planned exit:** a taker buy of the same `q` at the 1-minute bar that ends at
  `X = E + 7 days`. Reference price `P_x` is that bar's close.
- **Actual exit.** The position ends at the first of: the planned exit, a stop, a
  liquidation, or an event after entry (5.3). Its time is `t_exit`: `X` for the planned exit,
  and the **end of the triggering bar** (`bar start + 1 minute`) for the other three.
  **Everything downstream is tied to `actual_exit`**: the exit fill and its fee (on
  the value of `q` at that fill), the funding settlements counted (`E < s < t_exit`,
  strict, as in HYP-036), the benchmark interval `[E, t_exit]` of section 7.3, the
  release of the slot, and the capital-time used in section 6.4 (example A3). An
  `unresolved` episode has **no actual exit**: its slot and loss timing are set per scenario in 6.1
  (example A7). Its `t_last` (the end of the last covered bar) plays the role of `t_exit` **in H
  only**, for the time of the whole-margin loss, the funding window and the end of the
  market-adjusted interval `[E, t_last]` of 7.3; in L the episode is out of the mean and has no
  interval, but it **is entered**, and its known cash flows stay in the L ledger (6.1).
- **No other exit.** No discretionary exit, no re-entry.

### 5.3 Right-tail rule (short squeeze)

A short has unbounded loss on paper; a new perp can pump several times in hours. The rule
is fixed now and is part of the strategy, not an overlay. **No loss ceiling is promised**:
the rule defines what the model does, and the report measures the loss distribution.

**What Bybit's primary pages say (read on 2026-11-16).**

- [The New Margin Calculation](https://www.bybit.com/en/help-center/article/Understanding-the-Adjustment-and-Impact-of-the-New-Margin-Calculation)
  (last updated 2026-06-18). From 2025-09-02 the margin algorithm changes for perpetual
  and expiry contracts, **rolled out gradually** (the page gives no per-account date).
  For isolated margin, the maintenance margin uses the **mark price** and the initial
  margin keeps the entry price; the risk-limit tier then moves **dynamically** with the
  mark price. The short liquidation price **before** the change is
  `E + (IM - MM) / q + extra margin / q`; **after** it, for a USDT contract,
  `[E*q + E*q/lev + extra/(1+taker) + MM deduction] / [q + q*MMR]`. With no extra
  margin and no deduction this is `E * (1 + 1/lev) / (1 + MMR)`: at 2x and an `MMR` of
  0.5% about `1.4925 * E` after the change and about `1.495 * E` before it (the
  fee terms cancel in the old formula). The maintenance rate sits in the
  **denominator** of the new formula.
- [Insurance Fund](https://www.bybit.com/en/help-center/article/Insurance-Fund) (last
  updated 2026-03-24). A liquidation is triggered when the **mark price** reaches the
  liquidation price, and the position is **always settled at the bankruptcy price**, the
  level where no initial margin is left, whatever the market price. A better market
  execution leaves the surplus with the insurance fund, a worse one is covered by it,
  and if the fund runs out the loss goes to auto-deleveraging.
- Two older pages, "Liquidation Price, USDT Contract" and the isolated-mode page for the
  unified account, answered "not supported on this site" and are **not cited**. A
  partial-liquidation step is not described in the pages read and is **not modelled**.

**Consequence.** Liquidation here is a **scenario model**, not a reproduction of the
exchange's engine. Reasons: the formula changed during the discovery year and the
rollout date is unknown, so no single historical formula applies to every episode (the
holdout is wholly under the old logic); the maintenance rate and deduction are
symbol-and-tier specific and the tier now moves with the mark price; and the initial
margin also reserves a closing-fee amount that the 50% simplification ignores.

**The model.**

1. **Isolated margin and the liquidation scenario.** Each position holds its own
   margin, **50% of its notional** (2x leverage per position). The liquidation trigger
   is a **scenario parameter** `T_liq`, applied to the **1-minute mark-price high**:
   primary `T_liq = 1.49 * F_e` (between the two formulas at an `MMR` of 0.5%), and a
   sensitivity `1.46 * F_e` (an `MMR` of 2.5%, plausible for a days-old perp). The
   stage 2 contract records each symbol's maintenance rate and deduction as far as the
   data show them; the scenario value is not claimed to be what the exchange computed.
   Modelled result: the position settles at the bankruptcy price, so the **whole
   position margin is lost** (`-5,000 bps` of entry notional before funding and
   fees), whatever the fill, and **only the entry fee** is charged (exit-type table in 6.1). No separate
   liquidation fee is assumed; none is stated on the pages read.
2. **Stop.** A stop-market order on the **last trade price**, triggered when a 1-minute
   trade-price high reaches `S = P_e * 1.30`, from the entry bar onward (and only while
   the position is open, item 3). The fill is a
   **scenario assumption**, not a bound (6.2): scenario L fills at `max(S, bar open)`
   (gap aware), scenario H at the bar's **high**. A stop can lose more than 30% (example A2).
3. **Trigger window, and order inside one bar.** The stop trigger, the liquidation
   trigger `T_liq` (on the mark-price high) and the gap tests below are evaluated **only
   on bars while the position is open**: in time order from the **entry bar** through the
   **planned-exit bar** (the bar that ends at `X`), **inclusive**. The first bar that
   triggers ends the episode; a trade high or a mark high in any later bar, including
   one after an earlier stop, liquidation, confirmed event or the planned exit, never
   stops or liquidates anything retroactively and is not read.
   **Coverage condition.** A trigger counts as the first exit only on bars that are
   **continuously covered from the entry bar**, both the 1-minute trade bar and the
   1-minute mark-price bar of every minute. **After the first missing bar nothing later is
   read**: the episode is `unresolved` from that bar (item 5), even if a later bar shows a
   stop or liquidation level. Only a trigger found **before** the first missing bar is an
   exit. A minute with no trades inside a covered file or page is **not** missing: its trade bar
   is the explicit zero-volume bar, or, if the source emits none, a flat bar at the
   previous close with zero volume (a rule recorded in stage 2). Such a trade bar cannot
   trigger the **stop**, but it **does not cancel the mark-price test**: liquidation is
   tested on the independent mark-price stream, and a covered mark bar whose high is at or
   above `T_liq` liquidates even if the trade bar of that minute is flat or has no volume
   (example A2h). A minute is missing only when the source for it (file, page or series)
   is absent, and the two streams are never substituted: a missing mark bar is a gap and
   is **never rebuilt from trade prices**, and a missing trade bar is **never rebuilt from
   mark prices**. A
   missing minute at or after the time of a **confirmed event** (item 4) is not a gap;
   it is the event. A confirmed event dated **after** a gap does not rescue the episode:
   the last trade before it lies beyond the gap, which is not read (example A2f). One OHLC bar does not say what came first. Fixed conventions, the
   same in both scenarios L and H:
   - if the same bar reaches both the stop trigger and the liquidation trigger, the
     **liquidation applies** (the worse outcome for the short);
   - if the **planned-exit bar** itself reaches a trigger, the **trigger applies, not
     the planned exit** (the stop, or the liquidation if both are reached), and `t_exit`
     is the same `X`: only the fill and the exit type change (example A2e);
   - if a bar **opens** at or beyond the stop level (a gap), the stop triggers at that
     bar, and the fill is `bar open` in L and `bar high` in H;
   - if a bar's mark price opens at or beyond `T_liq`, liquidation applies at once. These conventions and the trigger window are pinned by
     examples A2a to A2h.
4. **Events after entry.** A halt, delivery, delisting, contract conversion or symbol
   migration after `E` is an **event of an open position**, not an exclusion.
   - **Confirmation.** The end of the archive is **not** the end of trading: a missing
     last file may mean an incomplete archive. An instrument event is established only
     by a **separate source**: the instrument's status in `instruments-info` (for
     example `Delivering` or `Closed` with a delivery time) or a machine-timed Bybit
     announcement. A confirmation counts only if the **event time** is at or before the first
     missing minute (item 3). Without it, or if the event is dated after the gap
     began, it is a **data gap**: the episode is
     `unresolved` (item 5), counts against the 95% bars coverage of M5 and the 5% gate
     below, and is never called a status exit.
   - **Scoring of a confirmed event.** The last trade before a halt is **not an
     executable fill** and is never assumed to be one. The episode stays in the sample
     and is scored in two stated scenarios, **L:** the last trade price before the
     event (labelled non-executable) and **H:** the whole margin lost. A recorded
     delivery price is reported beside them. Their share is reported by event type. Pinned by examples A4 and A6.
5. **Unresolved after `E`.** An episode with a missing trade bar or mark bar between the
   entry bar and the first exit (or the planned-exit bar when there is no earlier exit)
   (item 3), not explained by a confirmed event dated at or before it, is `unresolved` **from that bar on**. Its last covered bar is the
   bar before the first missing one, and `t_last` is the end of that bar. It is counted
   in the denominator, never dropped. Its price result, fee, funding window and slot
   timing are set **per scenario** (6.1, example A7): **L** keeps it out of the mean, debits its
   entry fee and the funding up to `t_last` in the ledger, and keeps its slot blocked until `X`; **H** gives it the whole margin lost at `t_last` and
   releases the slot then. Their share, together with `entry_unavailable` (M5), of the episodes eligible for entry
   must be at most 5%, else `incomplete_coverage` (examples A2f, A6 and A9).
6. **Why +30% and 2x.** A 30% stop limits the typical loss of a squeeze and a 50%
   margin leaves a buffer so that the stop is expected to fire before the exchange
   liquidates. The values are chosen once, by that purpose and not by any price, and
   **they do not bound the loss**: gaps, fills beyond the stop and liquidation
   mechanics can cost more than 30%. The price of the protection is measured by a
   diagnostic "no stop, liquidation only" beside the primary (section 7, component
   `STOP`).
7. **Reported, not capped.** The report gives the distribution of per-episode loss
   beyond 30%, the number of stops, liquidations and events after entry, and **the worst
   simultaneous stop-out observed**, with no hard cap assumed.

**Tail at the portfolio level.** Young alts pump together. Sizing keeps gross notional at
most 1x of reserved capital, which is a sizing rule and **not a loss ceiling**: stop fills
beyond +30% and gaps can make the loss of a market-wide squeeze larger than 30% of
reserved capital. The portfolio report shows the worst case that occurred.

### 5.4 Sizing and slots

- **Reserved capital `C`.** All capital held for the strategy, including idle margin and
  slots with no position. It is the denominator of the hurdle (section 6.4).
- **Slots `k`.** Fixed **before any price read**, from the resolved launch times of the
  verified cohort and the V1 timing: the smallest integer such that at least 90% of
  episodes find a free slot, computed on metadata. A planning guess: launches arrive
  about 5 a week and each occupies a slot for a week, so `k` is expected near 8.
- **Slot notional** `N = C / k` (gross leverage at most 1x of `C`); margin per slot is
  `0.5 N`; the remaining capital is an unallocated buffer that still counts in `C`. A
  slot is released at `actual_exit` (example A3); for an `unresolved` episode at `X` in L
  (blocked) and at `t_last` in H (example A7). `k` is computed on planned holds; early
  exits only free a slot sooner.
- **Cash is a time ledger.** Each portfolio keeps a ledger: the **entry fee** is debited at
  entry, **funding** at each settlement (signed), **price pnl and the exit fee** at close.
  Margin is a separate reserve, not a ledger entry. Nothing is charged twice: at close the
  entry fee and the funding already debited are not repeated. Unrealised pnl is not
  counted. `available_cash(t) = C + (sum of all ledger entries up to t) - margin reserved
by open positions`.
- **Funds check before every entry.** An entry needs `0.5 * N + phi * q * F_e` (margin plus
  entry fee) from `available_cash`. If it is below the requirement the entry is
  **`skipped_insufficient_funds`**: no reduction of size, no top-up, counted with its
  reason (examples A7b and A7c).
- **Four money paths.** The portfolio is simulated **separately for four paths: L on `net`,
  L on `exec_net`, H on `net`, H on `exec_net`.** In an `exec_net` path every positive `EVT`
  is set to zero in the ledger **at the moment it would be realised** (the positive price
  result of the episode is not credited; fees and funding stay), so a non-executable gain
  never funds a later entry. The entered set can differ between paths. `roc_annual` and
  every guard use the matching path (7.3, example A8b). H has no positive `EVT` (its
  results are `-margin` or a stop loss), so the H path on `exec_net` equals the H path on
  `net`; it is still computed.
- **Order-size grid for costs:** order notional `N` of **USD 50 / 250 / 500 / 1,000**.
  The primary economics is at **USD 50** (the registered start); the other sizes show
  capacity and decide nothing except the scale-up claim (section 6.3).
- **USD 50 one-slot portfolio.** Reported as the program plan's rule 2 requires:
  notional min(USD 45, cash minus USD 5), simultaneous signals ordered by SHA-256 of
  `new-perp-short-v1|symbol|E`, exits before entries. One slot takes about a fifth of the
  launches. Its skips, occupancy, daily PnL, drawdown and losing streak are reported in
  full. It gates nothing.

## 6. Net economics

### 6.1 Per-episode net

For an episode with entry fill `F_e` (sell), exit fill `F_x` (buy back at the actual
exit), quantity `q = N / F_e` fixed at entry, the settlements `s` with
`E < s < t_exit` (strict, as in HYP-036), and a taker fee rate `phi` per side:

```text
price_usd   = q * (F_e - F_x)
funding_usd = sum over s of  r_s * q * mark(s)        (a short receives when r_s > 0)
fee_usd     = phi * q * F_e  +  phi * q * F_x         (each fill is charged on its own value)
net_usd     = price_usd + funding_usd - fee_usd - borrow_usd
net_bps     = 10,000 * net_usd / (q * F_e)            (bps of the entry notional)
```

- **The fee is charged on each fill's value, not as a fixed 22 bps.** The exit is charged
  on the value of the position being closed. At `phi = 11 bps` (example A1) and a 30% price rise
  the round trip costs `11 + 11 * 1.3 = 25.3 bps` of entry notional; after a 30% fall
  it costs `11 + 11 * 0.7 = 18.7 bps`. In bps of entry notional the fee is
  `10,000 * phi * (1 + F_x / F_e)`. The model works in quantity and in the cost of
  each fill, including the exit VWAP for the same `q`.
- **Funding is actually accrued**, from the settlement history of that symbol, at the
  rate and settlement times of each row, not an assumed rate, on the position value
  `q * mark(s)` at each settlement. New perps change funding interval and cap; the
  schedule is what the history says. `mark(s)` is the open of the mark-price bar that
  starts at `s`. A hold with no settlement inside it gets zero funding only if M4
  proved its coverage. Funding stops at `t_exit`.
- **Borrow and other short costs.** A linear perp short has no borrow fee on Bybit:
  `borrow_usd = 0`, written down as a stated zero with its reason, never an omission.
  The other short-specific costs are modelled where they are real: the stop and
  liquidation rules (5.3), the event-after-entry scenarios (5.3), and the loss of the return
  on reserved capital (6.4). A spot-margin short is a different instrument and is not
  part of this protocol.

**Exit types and what each one charges.** The same rule, in one place, so that no other
section has to assume an exit. `F_x` is the fill of section 6.2; `margin = 0.5 * q * F_e`.

| Exit type                      | `price_usd`                                          | `fee_usd`                         | Funding counted for `s` with | Slot released                        |
| ------------------------------ | ---------------------------------------------------- | --------------------------------- | ---------------------------- | ------------------------------------ |
| Planned exit                   | `q * (F_e - F_x)`                                    | entry fee plus `phi * q * F_x`    | `E < s < X`                  | at `X`                               |
| Stop                           | `q * (F_e - F_x)`                                    | entry fee plus `phi * q * F_x`    | `E < s < t_exit`             | at `t_exit`                          |
| Liquidation (L and H)          | `-margin` (settled at bankruptcy)                    | **entry fee only**                | `E < s < t_exit`             | at `t_exit`                          |
| Confirmed event after entry, L | `q * (F_e - F_last)`, non-executable                 | entry fee plus `phi * q * F_last` | `E < s < t_exit`             | at `t_exit`                          |
| Confirmed event after entry, H | `-margin`                                            | **entry fee only**                | `E < s < t_exit`             | at `t_exit`                          |
| `unresolved` after `E`, L      | price pnl unknown, **not credited**; out of the mean | entry fee, debited at entry       | `E < s < t_last`             | **blocked until `X`** (conservative) |
| `unresolved` after `E`, H      | `-margin` at `t_last`                                | **entry fee only**                | `E < s < t_last`             | at `t_last`                          |

`F_last` is the last trade price before the event. A liquidation or a margin-loss
scenario charges no separate exit fee: the isolated margin already holds the closing-fee
reserve, and the loss is the whole margin. Pinned by examples A1 to A9 in Appendix A.

**Reading the margin-loss rows** (liquidation, event in H, `unresolved` in H):

- `-margin` **is the whole price loss of the position**. No other price loss is added
  on top of it: not `q * (F_e - F_x)` for any fill, and not the distance of a bar's high
  above `T_liq`.
- The **entry fee is paid from the account balance outside the margin**, so it is added
  **once**, as its own line: `net_usd = -margin + funding_usd - entry_fee`.
- **Funding is a separate signed line, counted once**, for the settlements up to `t_exit`
  (or the last covered bar for `unresolved`). It is not a second loss on top of `-margin`
  and is not part of it; a funding payment does not enlarge the margin loss in this model.
- No exit fee, no borrow cost and no second funding window exist for these rows. Examples
  A2a, A4 (H) and A6 (H) are computed exactly this way.

**Unresolved episodes in the portfolio.** The slot-limited portfolio (5.4, 6.4) is
computed **separately in L and in H**, because the two scenarios treat an `unresolved`
episode differently. In **H** the whole margin is lost at `t_last` and the slot is released
then. In **L** the price result is unknown and **not credited**, but the cash flows already
made are real: the **entry fee** debited at entry and the **funding** for the settlements up
to `t_last` stay in the L ledger (`net_usd` of the episode as a portfolio quantity is
`-entry_fee + funding_usd`), and its slot **stays blocked until `X`**, the conservative
choice: nothing is known about the position, so the capital is not assumed free. Two sets
are therefore kept apart: **entered** (the portfolio: it includes the L-unresolved episode
with its known cash flows) and **result included in the mean** (the per-episode statistics,
cluster means, bootstrap and decomposition of section 7: it excludes the L-unresolved
episode). Excluding an episode from the mean never removes its cash from the portfolio. The
set of episodes entered can differ between L and H (a later signal can find a free slot in
H and a blocked one in L), and **both** `roc_annual` figures are reported. If a scenario's
portfolio cannot be computed, that scenario has **no result** (section 8, sign map). Pinned
by examples A7 and A7b.

### 6.2 Cost model and its two scenarios

1. **Fee rate `phi`.** **`phi` = 11 bps is the VIP 0 (non-VIP) Innovation Zone taker rate
   for one fill, as stated on the zone FAQ; the rates that applied in the historical
   window are not recoverable.** The Innovation Zone non-VIP **taker rate of 0.1100% (11 bps) per
   side** and maker rate of 0.0400% are stated on Bybit's
   [Innovation Zone FAQ](https://www.bybit.com/en/help-center/article/FAQ-Perpetual-Trading-Innovation-Zone)
   (read 2026-11-16, last updated 2026-08-07). The standard non-VIP taker rate of 0.055%
   (5.5 bps) is a reported sensitivity. The same page says a contract can **migrate**
   between the Innovation Zone and the main board in either direction, so which launches
   sat in the zone, and for how long, is not recoverable from history; 11 bps per side
   is an assumed rate for every episode, not a bound. The
   [fee group endpoint](https://bybit-exchange.github.io/docs/v5/market/fee-group-info)
   lists the zone's symbols but returns only the current membership and applies to Pro
   and market-maker clients. The fee schedule is mutable external data: it is
   snapshotted durably with the run, and the owner's own account tier is confirmed from
   the account. Maker fills are not modelled.
2. **Fill prices are scenario assumptions, not bounds.** A market order in a thin
   days-old book can fill worse than any figure computed from candles or a snapshot.
   Two scenarios are fixed, and the report shows both:

| Fill               | Scenario L (book, else allowance)                                                                                                 | Scenario H (bar extremes)  |
| ------------------ | --------------------------------------------------------------------------------------------------------------------------------- | -------------------------- |
| Entry (sell `q`)   | order-book **VWAP** for `q` from the bids at the entry instant, where a book exists; else `P_e` less a fixed allowance of 9.5 bps | the entry bar's **low**    |
| Planned exit (buy) | order-book VWAP for `q` from the asks at the exit instant, where a book exists; else `P_x` plus 9.5 bps                           | the exit bar's **high**    |
| Stop               | `max(S, bar open)`, plus 9.5 bps                                                                                                  | the trigger bar's **high** |
| Liquidation        | whole margin lost                                                                                                                 | whole margin lost          |
| Event after entry  | last trade before the event (non-executable)                                                                                      | whole margin lost          |

- **Neither scenario dominates the other.** They are **independent assumptions**, and
  neither is a bound. In a synthetic case with flat bars at 100 and no book, L sells at
  99.905 and buys back at 100.095, while H sells at 100 and buys back at 100, so H is
  better than L. In a wide bar H is worse. The document therefore never calls one
  "the lower-cost scenario", and section 8 treats a disagreement in sign, in either
  direction, as `scenario_dependent` (example A5).
- **Allowance of 9.5 bps per fill (19 bps round trip) is a spread-and-slippage
  allowance. It is added to the fee `phi`, and it is not a fee.** It moves the fill
  price (entry `P_e` less 9.5 bps, exit `P_x` plus 9.5 bps), and the fee `phi` is then
  charged on that fill's value as in 6.1. So scenario L costs **`phi` + 9.5 bps = 20.5 bps
  per fill** where no book exists (example A5). The figure is the one used for
  established perps in the [venue cost check](venue-cost-check-v1.md); days-old books are
  likely worse, so it is a floor for L and not an estimate.
- **VWAP** is computed at order sizes USD 50, 250, 500 and 1,000 with no extra
  half-spread added (it is already inside the VWAP). Where the book has less depth
  than the size, that size is `depth_absent` for that fill and is **not extrapolated**.
  The USD 50 primary size takes the fixed allowance where no book exists; a larger size
  with no depth is left out of that size's net and counted.
- **Mixed cost sources.** Scenario L mixes book VWAP episodes and allowance episodes
  in one mean; the report therefore also gives L split by cost source, and the share
  of each. They are never presented as one evidence class.

**How the scenarios decide.** Section 8. Both at or below zero is a fail **under the
assumed costs**; opposite signs, in either direction, are `scenario_dependent`; both above
zero opens the guards. A fail under assumed costs may stop spending, but it is **not
proof that no edge exists**. Any pass is still a hypothesis: real spread and depth must be
measured before any order.

### 6.3 Capacity

The net at each order size on episodes where the book has that depth, and the share of
episodes with `depth_absent` per size. A positive net at USD 50 that disappears at USD
250 is a result about USD 50 and says so; it does not support a scale-up. The registered
scaling rule (program plan, section 6) governs size, not this document.

### 6.4 The 5% hurdle on all reserved capital

The owner's reference for money with no trading risk is **5% a year**. The economics is
computed on `C`, not on the notional of open trades:

```text
roc_annual = (sum of net_usd over the window) / C * (365 / window_days)
```

with `window_days` the days of the discovery year that episodes may occupy, each
episode occupying its slot until `actual_exit`. Idle slots,
the unallocated buffer and the days between trades count in `C`. `roc_annual` is computed on the **slot-limited portfolio**: an episode that finds no free slot is
skipped and its reason recorded, while the per-episode statistics of section 7 use every episode with a result in the
scenario (an L-unresolved episode is entered in the portfolio, with its known cash flows, but
is not in the mean, 6.1). The portfolio, and so `roc_annual`, is **computed separately on each of the four money paths of 5.4**: an
`unresolved` episode blocks its slot until `X` in L and frees it at `t_last` in H (6.1,
example A7), so the entry sets can differ and both figures are reported. A result must
exceed 5% a year **in both scenarios**, L and H (section 6.2), **on `net` and on `exec_net`**,
each on its own money path (5.4, 7.3, examples A8 and A8b). At about 205 episodes
and `k = 8` the hurdle alone needs only about 20 bps mean net per episode
(`0.05 / (205 / 8 / 10,000)`), far below the statistical bar of section 7. The hurdle
is a second guard, not the binding one. The report also shows the money in dollars for
`C` of USD 50 (one slot), 250, 3,000 and 5,000, so the scale of the prize is visible.

## 7. Dependence, decomposition and power

### 7.1 Why 263 is not 263

New perps arrive about five a week and each path spans two calendar weeks (age plus
hold). Launches in one hot month, or one market wave, share the market's move and the
same funding regime; their returns are positively correlated. Over a year in which alts
fell, a short of anything profits. A naive standard error over 263 episodes understates
the uncertainty, and the effect we look for is small against the dispersion of a young
alt over a week.

### 7.2 Dependence model

- **Cluster:** the ISO week of the launch. All launches of one week are one cluster.
  Primary statistics weight **clusters equally**; episode-weighted means are reported
  beside them.
- **Overlap beyond the cluster:** two episodes whose launches are less than 7 days
  apart overlap in calendar time. The uncertainty is therefore estimated with a
  **moving-block bootstrap over ISO weeks, primary block length 4 weeks** (about one
  market regime), with 10,000 draws and a fixed seed; block lengths 2 and 8 are reported
  as sensitivity and none is picked for its result.
- **Design effect:** `DEFF = (bootstrap variance of the cluster-mean) / (variance under
independence)`. The effective sample size is `n_eff = n_resolved / DEFF`. Both are
  reported with their inputs.
- **Regime view:** the same statistics by half-year and by a market-state split fixed
  now (BTCUSDT's 30-day return at `E`: above or below its median in the window, read only
  from data dated inside the window, lookback cut of section 2), as diagnostics. A result that exists only in one regime is not general.

### 7.3 Market-adjusted return and the six-part decomposition

The primary measure is the cluster-mean **net** (as traded). Beside it, per the program
plan's rule 2, the **market-adjusted net** must stay positive:

```text
net_adj_bps = net_bps + 10,000 * R_bench       (R_bench over [E, t_exit]; a hedge that costs nothing)
```

for two benchmarks: BTCUSDT, and the **median of a pool of the 20 most liquid linear
USDT perps** (by trailing 30-day turnover as known before `E` and dated at or after the window
start, excluding the instrument itself; point in time; lookback cut of section 2). The pool rule is fixed now, its members are computed at read
time from data older than `E`. Adjustment is a **diagnostic hedge, not a tradable
position**, as in the HYP-036 read. A pooled cross-sectional slope `beta_hat` of
episode return on the benchmark return, over all episodes, is also reported (cluster
bootstrap interval); the primary uses `beta = 1` so that no coefficient is fitted to
the outcome.

Every episode's net is split exactly into six additive parts (the identity is tested):

```text
net = M + S + FND + STOP + EVT - EXE - FEE
M    = -10,000 * beta * R_bench                           market (beta = 1 primary)
S    = -10,000 * (R_ref - beta * R_bench)                 selection: the idiosyncratic drift of the coin
FND  =  10,000 * funding_usd / (q * F_e)                  funding actually accrued, to actual_exit
STOP =  10,000 * (pnl_ref_rules - (-R_ref))               effect of the stop and liquidation rules
EXE  =  10,000 * (pnl_ref_rules - price_usd / (q * F_e))  execution: fills against the reference prices
EVT  =  10,000 * price_usd / (q * F_e)                    the whole price result of an episode with no reference exit
FEE  =  10,000 * fee_usd / (q * F_e)                      fees on each fill's value
```

with `R_ref = P_x / P_e - 1` (the planned-exit reference return, the no-rules counterfactual)
and `R_bench` over the **planned** window `[E, X]`, so that `M + S = -R_ref` exactly. The
rules, including an early exit, appear only in `STOP`. `pnl_ref_rules` is the price result at
reference prices after the rules of 5.3: `P_x` for a planned exit, the stop level `S` for a
stop, the whole margin for a liquidation.

**Episodes with no reference exit.** When a **confirmed event** or an **unresolved gap**
ends the episode before `X`, there is **no `P_x`** and `R_ref` is undefined (the same holds
for a stop or liquidation whose planned-exit bar is not covered). **No price is invented
and the episode is not dropped.** Such an episode carries a flag `no_reference_exit`, and
`M = S = STOP = EXE = 0` for it. Its **whole price result** is the part `EVT`, stated
once for **every** `no_reference_exit` episode:

```text
EVT = 10,000 * price_usd / (q * F_e)     price_usd from the 6.1 table row of its actual exit
```

that is: confirmed event in L, the non-executable last-trade result `q * (F_e - F_last)`;
confirmed event in H, liquidation (L and H) and `unresolved` in H, `-margin`; **stop**, its
stop-fill price result `q * (F_e - F_x)` in that scenario (`F_x` of 6.2). An `unresolved`
episode exists in H only (in L it is out of the mean and the decomposition, 5.3 item 5). The identity then reads `net = EVT + FND - FEE` for it and still holds
exactly; the test checks it for every episode and every scenario (examples A2g and A4b).

The market-adjusted net used by the guards, `net_adj`, uses the interval over which the
short was actually open: `[E, t_exit]`, and for an `unresolved` episode **in H only**
`[E, t_last]`, the same end as its whole-margin loss (an unresolved episode has no actual
exit and is excluded in L; `M` and `S` are already zero for it, so only `net_adj` uses this
interval, example A6). A positive result that is all `M`, or all `FND`, is not a new-perp
effect, and a positive result carried only by `EVT` is not one either: **a non-executable
last-trade gain must never be the reason for a pass.**

**`exec_net`.** For a `no_reference_exit` episode
`exec_net_usd = net_usd - max(price_usd, 0)`, that is the net with its positive `EVT`
set to zero (a negative `EVT` stays); for every other episode `exec_net = net`.
**Every mandatory condition is evaluated on `net` and on `exec_net`, in L and in H**: the
5% `roc_annual` barrier of the slot-limited portfolio (6.4, simulated on the matching
money path of 5.4: a gain set to zero in `exec_net` cannot fund an entry there), the 90% bootstrap lower
bound, the removal of the top 5 episodes and the top 5 weeks, the sign in both halves of
the year, and the market-adjusted guard (`net_adj` built from `exec_net` as well). A pass
needs all of them on both (examples A8 and A8b). The report also states which part carries the
sign, gives the **number of `no_reference_exit` episodes and the share of the total net
they contribute**, by scenario. The
portfolio report (slot occupancy released at `actual_exit`, every skip with its reason,
daily PnL on `C`, drawdown, losing streaks, worst simultaneous stop-out) is mandatory
next to the per-episode net.

### 7.4 Power plan

- **Statistic:** the cluster-mean net of V1 against zero; one-sided test at 2.5%, power
  80%, so the multiplier is `1.96 + 0.84 = 2.80`.
- **Planning MDE:** `MDE = 2.80 * SD * sqrt(DEFF) / sqrt(n)`. `SD` is the standard
  deviation of one episode's net.
- **What `SD` is.** Unknown. The catalyst contract assumed 300 bps for events of hours;
  a young alt's week is far more dispersed. The grid below uses 1,500, 2,000 and 2,500
  bps as **planning assumptions, not measurements**.
- **Planning table (arithmetic on those assumptions, MDE in bps of net per episode):**

| `n` resolved | `SD` bps | DEFF 1.0 | DEFF 1.5 | DEFF 2.5 | DEFF 4.0 |
| -----------: | -------: | -------: | -------: | -------: | -------: |
|          263 |    1,500 |      259 |      317 |      410 |      518 |
|          263 |    2,000 |      346 |      423 |      546 |      691 |
|          263 |    2,500 |      432 |      529 |      683 |      864 |
|          220 |    1,500 |      283 |      347 |      448 |      567 |
|          220 |    2,000 |      378 |      463 |      597 |      756 |
|          220 |    2,500 |      472 |      578 |      747 |      944 |
|          205 |    1,500 |      293 |      359 |      464 |      587 |
|          205 |    2,000 |      391 |      479 |      618 |      782 |
|          205 |    2,500 |      489 |      599 |      773 |      978 |
|          180 |    2,000 |      418 |      511 |      660 |      835 |

- **Reading the table.** On about 205 episodes (the planning number after the lookback cut) an effect below
  roughly 3% to 6% net per weekly trade is not detectable, and it is a **large** effect to find in an open
  market. The study may well end at the power gate. That is an answer, and a cheap one.
- **Calibration before any outcome (stage 3), dispersion only.** In the style of the
  HYP-037 calibration: for each resolved launch the return over `[T0 + 1 d, E)` (the
  six pre-entry days for V1) is a **pre-entry proxy**. Its dispersion gives `SD`
  (inflated by 7/6 as a variance scale, and conservative because volatility decays with
  age), and its week-block bootstrap gives `DEFF`. **No mean, sign or outcome is
  emitted.** The proxy window overlaps V2's hold; this exposure is registered. Requires
  at least 95% defined proxies, at least 100 resolved episodes and at least 26 launch
  weeks, otherwise `not_estimable`.
- **Power gate (fixed now).** The calibrated MDE must be **at most 400 bps** of net per
  episode at the resolved `n`, with `DEFF` from the largest of the block lengths 2, 4 and
  8 (never the favorable one). Above it: stop `insufficient_power`, and **no return is
  read**. At `n = 205` this needs `SD * sqrt(DEFF)` of about 2,050 bps or less. The 400
  bps is a planning limit, chosen as an effect that would be material against costs and
  still believable; it is question Q5.
- **Proposed Amendment 6 (2026-11-20, stage 3 contract).** The
  [stage 3 contract](new-perp-stage3-power-calibration-v1.md) fixes the calibration above and
  deviates from the text above in six places, which is why it is written here. (1) The proxy is
  read from 1-hour trade bars, not 1-minute bars: section 10 said both "1-minute" and
  "x 1 request", and six days of minute bars need nine requests per symbol; one request per
  episode returns about 145 hourly rows. (2) The cohort is the 228 primary V1 episodes of the
  completed discovery Stage 1 run, so there are 228 requests, not 263. (3) The proxy window is
  `[A, B]` on the whole-hour grid inside `[T0 + 1 d, E)`, with `A = ceil_hour(T0 + 1 d)` and
  `B = floor_hour(E)`; `B - A` is 143 or 144 hours, and no bar that ends after `E` is read.
  (4) The planning variable is the 7-day equivalent simple return
  `10,000 (exp(L sqrt(168 / h)) - 1)` of the close-to-close log return `L` over `[A, B]`, where
  `sqrt(168 / h)` replaces the flat `7/6` (equal at `h = 144`). (5) `DEFF` is the bootstrap
  variance of the cluster mean over `sd^2 / n`, the largest of the block lengths 2, 4 and 8 and
  never below 1.0, and the gate is `2.80 * SE <= 400` bps, unrounded; the MDE at the multipliers
  2.487 and 2.123 is reported and gates nothing. (6) The three requirements of the
  calibration bullet above (95% defined proxies, 100 episodes, 26 weeks), which it answers with
  `not_estimable`, instead end in the codes section 8 already names, `incomplete_coverage` (a
  defined-proxy share below 95%) and `insufficient_events` (fewer than 100 defined episodes or
  fewer than 26 weeks), which now also reach stage 3; `not_estimable` is kept for a calibration
  that cannot be computed (a zero variance, an empty bootstrap draw) and has no code. No rule
  fixed for stage 4 changes, no holdout fact enters, and the amendment authorizes no read: a fetch needs
  the owner's go for the contract's `go_sha256`.
- **Evidence floor.** At least **100 resolved episodes** (the program default) and at
  least **26 ISO weeks** with a resolved episode, counted as `n_primary` after the
  lookback cut, `funding_uncovered` and the identity statuses (section 2). No exception is
  requested. If fewer remain after the lookback cut or M1 to M4, the stop is
  `insufficient_events`, not a smaller test and not a shortened lookback. About 17
  launches are expected to be lookback-censored; the planning number of 205 stays well
  above 100, but the true count is a metadata fact of stage 1.
- **Guards from the program plan, rule 3:** the result must survive removal of the top 5
  episodes, the top 5 weeks, and hold in both halves of the year.

## 8. Decision rule and stop codes

Checked in this order. The first that applies ends the study at that stage. Stages 1
to 3 read no return.

| Order | Code                       | Condition                                                                                                                                                                                                                                                                                                                                       | Stage   |
| ----- | -------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------- |
| 1     | `not_reconstructible`      | M1 fails (`launch_time_unverified`), or the lifecycle of a launch cannot be rebuilt from native data for more than 15% of episodes                                                                                                                                                                                                              | 1       |
| 2     | `incomplete_coverage`      | M2, M4 or M5 fails (bars, mark bars and funding; **order-book coverage is not a gate**), or more than 5% of the episodes eligible for entry are `unresolved` after `E` or `entry_unavailable` (an end of archive without a confirming source dated at or before the first missing minute counts here; `funding_uncovered` is gated at 5% by M4) | 1, 3, 4 |
| 3     | `identity_unresolved`      | M3 fails                                                                                                                                                                                                                                                                                                                                        | 1       |
| 4     | `insufficient_events`      | fewer than 100 resolved episodes, or fewer than 26 weeks, after the stages above and after the lookback cut (`n_primary`, section 2); at stage 4 the same floor is applied **to each scenario's own cohort** (L leaves `unresolved` episodes out, so its count can be smaller than H's), and a scenario below it has no result (the map below)  | 1, 3, 4 |
| 5     | `capital_infeasible`       | M6 fails, or the order cannot be opened within `C` (minimum order, quantity step, risk limit, or no depth at USD 50 **at entry** for more than 20% of episodes; depth at exit never removes an episode)                                                                                                                                         | 1       |
| 6     | `insufficient_power`       | calibrated MDE above 400 bps (7.4), returns unread                                                                                                                                                                                                                                                                                              | 3       |
| 7     | `negative_net_after_costs` | the V1 cluster-mean net is at or below zero in **both** scenarios L and H (map below), on at least the evidence floor in each. A mature negative is a **fail under the assumed costs** and is not masked by thin diversity; it is not proof that no edge exists                                                                                 | 4       |
| 8     | `scenario_dependent`       | the sign of the V1 cluster-mean net **differs between L and H**, in either direction (positive means above zero; zero counts as not positive). Neither a pass nor a fail                                                                                                                                                                        | 4       |
| 9     | `positive_not_robust`      | net is above zero in both, but a guard fails in L or in H, on `net` or on `exec_net` (7.3): market-adjusted net not positive for both benchmarks; a top-5 removal or half-year flips the sign; the 90% bound is not above zero; `roc_annual` is not above 5%; or the sign comes only from `M`, `FND` or a positive `EVT`                        | 4       |
| 10    | `lab_positive_hypothesis`  | all of the above hold in both L and H, on `net` and on `exec_net`                                                                                                                                                                                                                                                                               | 4       |

**Sign map of the V1 cluster-mean net (stage 4).** Every combination has one
verdict. "Above zero" is strict; **zero is not positive**. A scenario with no result is
checked first, before any sign is read.

| Net in L                  | Net in H                 | Verdict                                                                                  |
| ------------------------- | ------------------------ | ---------------------------------------------------------------------------------------- |
| above zero                | above zero               | go on to the guards: `positive_not_robust` if one fails, else `lab_positive_hypothesis`  |
| at or below zero          | at or below zero         | `negative_net_after_costs`                                                               |
| above zero                | at or below zero         | `scenario_dependent`                                                                     |
| at or below zero          | above zero               | `scenario_dependent`                                                                     |
| no result in L, H or both | any, including no result | `insufficient_events`, naming the scenario; no sign is read, no pass, no fail, no guards |

A scenario has **no result** when its cohort is below the evidence floor (fewer than 100
resolved episodes or fewer than 26 ISO weeks), when every episode of it is excluded, or
when its slot-limited portfolio cannot be computed (6.1, 6.4), or when its bootstrap cannot be computed for lack of clusters. The one status for all of
these is `insufficient_events` (order 4), reported with the scenario and the counts. It is
never read as "negative" and never as "scenario dependent". So `L = 0` with `H > 0`, and
the reverse, are `scenario_dependent`; both at exactly `0` is `negative_net_after_costs`.

- **`lab_positive_hypothesis` is not confirmation.** It means only that the candidate may
  be considered for a frozen exam (section 9). The discovery year is open and about 124
  variants of other families were already read on it; a laboratory finding here has the
  status of a hypothesis. It is not execution-validated: neither scenario is a bound.
- **Conditional results, in words.** Liquidation here is an explicitly labelled
  **scenario model** and the fills are scenario assumptions, so every result stays
  conditional on them. **A negative result under the chosen assumptions is a reason to
  stop spending on this protocol, not proof that no edge exists.** A positive result
  under them is a hypothesis, not a confirmation.
- **Two independent assumptions, no hierarchy.** L and H are not ordered by cost, so
  neither is "the optimistic" nor "the conservative" case. An idea is rejected only
  when it fails under both, and accepted for the guards only when it passes under
  both. A disagreement is reported as such, and the report shows which fills (entry,
  exit, stop) drive it. Neither the book-versus-candle choice nor the stop fill may be
  picked after the read.
- **Lower bound.** The block-bootstrap one-sided lower bound of the V1 cluster-mean net is
  taken at **90%** for the laboratory (stricter than a point estimate, laxer than the
  exam's 97.5%, because the laboratory is explicitly a filter before the exam; question
  Q6). It is evaluated in both scenarios.
- **Reasons, not bare zeros.** Every stop reports its counters: launches by status,
  M1 classes, M2 shares, M3 statuses, unresolved episodes by reason, censored launches,
  stops, liquidations and events after entry, book coverage by size.
- **One corrective recomputation** is allowed after any first verdict: a proven defect
  against this contract, a regression test that fails on it, the same sealed inputs and
  no new request, both results kept. Changing the strategy, the rule or the sample after
  a result is not a correction.

## 9. Confirmation: where a positive laboratory result could be confirmed

State plainly: **no fresh, powered confirmation window exists for I25 today.** A
positive laboratory result is a hypothesis and would be exactly that until one of the
routes below is opened.

| Route                                       | Status                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| ------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Discovery year                              | open and used for development. Cannot confirm.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| Backward holdout, 2023-02-01 to 2024-09-15  | **The only route inside the program clock.** 254 launches over 74 weeks were counted, about the size of the discovery year. It is one shared single shot for the whole program (plan, stage 6): I25 does not spend it alone. It would be spent as one **frozen batch** (I25 alone or with other candidates, with a correction for the number of candidates), after the stage 4 result, with parameters unchanged. Its checks M1 to M6 must pass on it first, as stage 1b: run sealed at coverage level, only after stage 1 passes on the discovery year and on the owner's separate go. 2023 launches also carry the larger identity risk (ticker reuse) and the shakier catalog of delisted perps. Memory of famous launches in that period must not shape the rule: the rule is already frozen here. |
| HYP-031 test year, 2025-10-01 to 2026-09-28 | excluded and unassigned. It is a possible second window **only** if the owner and the colleague reassign it, and I25's launches in it are **not counted**: the fetch dropped instruments launched from 2025-10-01 by design. Counting them is a new metadata read (instruments launched 2025-10-01 to 2026-09-28 only, never from 2026-09-29), needing a registry row and the owner's go. Its prices were partly exposed to HYP-031's other mechanism; this must be assessed before assigning.                                                                                                                                                                                                                                                                                                         |
| Forward data after HYP-012 v2 is terminal   | renewable but late. v2's terminal deadline is 2027-03-31 and every price read from 2026-09-29 is blind until then. At about 263 launches a year, one forward quarter holds about 66 launches, **below the 100-episode floor**; two quarters hold about 130. The program stop rule is 2027-02-11. So a forward confirmation does not fit the program clock. Recording launch **metadata only** during the blind window would need a formal R1 amendment at the 2026-11-30 checkpoint.                                                                                                                                                                                                                                                                                                                   |

What would have to happen for a confirmed result:

1. stage 1 to 3 pass on the discovery year, then stage 4 reaches `lab_positive_hypothesis`;
2. stage 1b: the holdout's M1 to M6 pass, sealed, on the owner's separate go;
3. the owner and the colleague freeze the exam batch (members, parameters, correction)
   and give the go for the single read;
4. the exam passes the criteria of 8 with a **97.5%** lower bound above zero and the
   guards, on a power plan computed from the holdout's own calibrated `SD` and `DEFF`;
5. only then does the program plan's paper and LIVE_PROBE path open, with its own safety
   list (O10): isolated margin and stop orders as exchange orders, restart-safe
   reconciliation, deterministic `clientOrderId`, a squeeze circuit breaker and kill
   switch. A paper implementation alone does not make a short with an unbounded tail safe.

If the power gate or the checks fail, the plain statement is that **I25 has no
confirmable form on the data the program may use**, and the record says which stop code
and what would be needed: a longer or additional window, or a second venue's launches.

## 10. Data, budget and exposure (planning only, frozen later)

This section registers nothing. The numbers are **planning estimates**; caps are frozen
in stage 2, in a request list with a hash, before any request, as for HYP-036.

| Stage | Data                                                                                                                                                                                                                                                                                                                                             | Planning size                                                              |
| ----- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------- |
| 1     | discovery year only: trade-archive file listings and the first daily file per launch; catalog fields; machine-timed Bybit announcements already in hand; Bybit spot catalog; existence lists for trade bars, mark bars, funding and books                                                                                                        | on the order of 1,000 requests and a few GiB                               |
| 1b    | backward holdout, after stage 1 passes and on a further go: the same checks, sealed output                                                                                                                                                                                                                                                       | on the order of 1,000 requests; output is aggregate counts and hashes      |
| 3     | 1-hour trade bars for the pre-entry proxy window of the stage 1 primary episodes (Amendment 6; first drafted as 1-minute bars)                                                                                                                                                                                                                   | 228 symbols x 1 request                                                    |
| 4     | 1-minute trade bars and 1-minute mark-price bars for days 3 to 21 (about 26 requests per symbol each); hourly mark bars at settlements (1 per symbol); funding history (about 1 to 3 pages per symbol); liquid-pool hourly bars (about 400 requests); order-book snapshots for V1 entry and exit (up to about 526 symbol-days, each 1 to 460 MB) | about 14,000 API requests and a book-archive volume that needs its own cap |

**Exposure for the seen-windows registry** (row added with this document; it reads
nothing):

- stage 1: **coverage** level, discovery year only (timestamps, file lists, catalog
  fields, announcements). No price is read;
- stage 1b: **coverage** level, backward holdout, sealed aggregate output, only after
  stage 1 passes and on a separate go;
- stage 3: **features** level (pre-entry return dispersion and dependence, no mean,
  discovery year only);
- stage 4: **outcomes** level for the discovery year only, on the cohort of resolved
  launches and their paths ending by 2025-09-30. After that read those paths count as
  read at outcome level for this trigger. The price-scale diagnostic of M3 belongs to
  this stage and is outcome-level;
- holdout: coverage only until the frozen exam; the exam read, if it happens, is its
  single outcome read;
- never read: any price from 2025-10-01 (test year) or from 2026-09-29 (blind window).

## 11. Stages and what needs the owner's separate go

| Stage | What                                                                                                                                   | Gate                                             |
| ----- | -------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------ |
| 0     | this document, draft PR                                                                                                                | colleague's design discussion                    |
| 1     | **data-recoverability check, discovery year only:** M1 launch semantics, M2 and M4 to M6 coverage, M3 identity by independent evidence | owner's go per fetch, registry row, request list |
| 1b    | the same checks on the backward holdout, sealed                                                                                        | stage 1 passed **and** the owner's separate go   |
| 2     | contract freeze: JSON contract, request list with hash, code with synthetic tests, snapshots of the exchange rules, registry update    | colleague review, owner's go for the fetch       |
| 3     | power calibration (dispersion only)                                                                                                    | owner's go for the price read                    |
| 4     | laboratory read of V1, V2, V3 and the portfolio report                                                                                 | owner's go for the read of returns               |
| 5     | decision on a frozen exam batch                                                                                                        | owner and colleague                              |

**Separate go of the owner is required for each of:**

1. any fetch, including trade-archive listings and the first-file downloads of M1 to M3;
2. any price, funding, mark-price or order-book read, including the stage 3 calibration;
3. the laboratory read of returns (stage 4);
4. **stage 1b**, any read of the backward holdout beyond what stage 1 needs, and the
   exam itself;
5. any assignment of the HYP-031 test year and the metadata count of launches in it;
6. any recording of launch metadata inside the HYP-012 v2 blind window (needs an R1
   amendment first);
7. confirming the order-book archive as an official source and committing its volume cap;
8. confirming the owner's own account fee tier (Innovation Zone) for the live case;
9. the merge of this protocol, and of every later PR in the chain;
10. any paper, LIVE_PROBE or order path.

## 12. Selection history carried forward

- The catalog count read **no price**. The idea came from the A-screen and the blind
  ideas round; its mechanism is public knowledge.
- The 2026-11-11 exploratory series read about 124 variants of other families on this
  discovery year, among them candles of many of these coins. The cohort's prices are
  therefore not fresh and a laboratory result is a hypothesis. The ledger count rises by
  3 for V1 to V3 at stage 4, and by 0 at stages 1 to 3.
- A short of a post-hype asset partly mirrors known results: HYP-012b's source-lead
  at-signal entries lost after costs, and HYP-036's short at a 4 h hold ended at -42.80
  bps net. This protocol differs in horizon (a week, not minutes to hours), in costs
  relative to the drift, and in being a calendar event with no signal. It is not shown
  to differ in sign.

## 13. Revisions, and open design questions for the colleague

**Findings policy.** A later finding changes a stop code or the sign map **only if it lets
a wrong verdict pass**. Anything about the code-level detail of the portfolio simulation
goes to the stage 2 contract and its tests. Deferred to stage 2: Q18 (a), (b) and (c);
the precision and rounding of the ledger; the treatment of unrealised pnl and of quantity
steps in the funds check.

### 13.0 Revision 10: known cash flows of an unresolved episode in L (one problem)

The 6.1 row said fee `none` and funding `none` for `unresolved` in L, against the ledger
rule of 5.4. Excluding an episode from the mean must not remove its cash from the
portfolio: it could let a portfolio at 5.025% a year read as 4.975% after the known 0.50.
Now: in L the entry fee is debited at entry and the funding up to `t_last` counted, the
price pnl is unknown and not credited, the slot is blocked until `X`, and the two sets
(**entered** and **result included in the mean**) are separated in words (6.1). A2f, A6, A7
and A7b are resynced; the L funds check uses these flows through the ledger. The
suggested one-line edit of 6.4 (naming USD 5,000 as the primary working capital) was **not**
made: 5.4 and 6.4 still say the primary economics is at USD 50, and changing the primary
size is a decision for stage 2 and the owner, not a wording fix.

#### Revision 9: cash ledger and the four money paths (two problems)

Both were real and are fixed in 5.4 with examples.

1. **Cash is a time ledger (5.4, A7, A7c).** Entry fee at entry, funding at each
   settlement, price pnl and exit fee at close, margin as a separate reserve,
   `available_cash = C + ledger - reserved margin`, nothing charged twice. The funds
   counterexample (cash 250.45, not 251, against 250.55 needed) is A7c, and A7 now counts
   V's entry fee and funding in the cash at W's entry (499.10, not 499.5).
2. **`exec_net` has its own money path (5.4, 6.4, 7.3, A8b).** Four paths: L and H, each on
   `net` and on `exec_net`; a positive `EVT` is set to zero in the ledger when it would be
   realised, so a non-executable gain never funds a later entry; the entered set can differ;
   `roc_annual` and every guard use the matching path.

#### Revision 8: the colleague's full pass (six problems and a sync note)

All were checked against the text and were real. A lookback of 30 days from the earliest
`V1` entry (2024-10-08) starts on 2024-09-08, inside the discarded dates.

1. **Lookback outside the window (2, 3, 7.2, 7.3, 7.4, 8).** No feature, benchmark or pool
   membership reads data dated before the window start; such an episode is
   `lookback_censored` (counted, not in the primary sample) and the lookback is never
   shortened. It does not widen data access. The planned sample is recomputed with a formula
   and a planning guess only (about 17 censored; about 205 resolved; MDE table row added);
   a count below the floor is `insufficient_events`.
2. **Funds (5.4, A7, A7b).** An entry needs margin plus the entry fee from cash that
   includes realised losses, else `skipped_insufficient_funds`, per scenario. A7 was wrong
   under this rule (W could not afford the entry in H) and was redone with `k = 2`;
   A7b shows the funds check with the original toy.
3. **Funding coverage (M4).** A selection condition decided from metadata, status
   `funding_uncovered`, never entered; unknown funding is never zero; the survivorship limit
   is stated.
4. **`exec_net` (7.3, 8, 6.4).** Every mandatory condition is evaluated on `net` and on
   `exec_net` (positive `EVT` set to zero), in L and H (example A8).
5. **Missing entry bar (3, M5, A9).** `entry_unavailable`, decided from bar existence only,
   never scored, counted in the 5% gate.
6. **Benchmark end of an unresolved episode (5.2, 7.3, A6).** In H only, `t_last`; in L none.
7. **Sync (Q17).** The first-exit wording.

#### Revision 7: the colleague's third read (three problems)

All three were checked against the text and were real.

1. **`EVT` of a stop (7.3).** `EVT` was described only as the last-trade result or
   `-margin`, but the formula is `price_usd / (q * F_e)` and a stop whose planned-exit bar is
   not covered has its own known price loss. Stated once: for every `no_reference_exit`
   episode `EVT` takes `price_usd` from the 6.1 row of its actual exit (event L last-trade
   result; event H, liquidation and unresolved H `-margin`; stop its stop-fill result in
   that scenario). Example A2g. The coverage rule of 5.3 item 5 now says the gap is
   looked for up to the first exit, so a gap after a stop does not make it unresolved.
2. **Zero-trade minute against liquidation (5.3 item 3).** "Cannot trigger anything" held
   only for the trade-price stop. A covered mark bar with a high at or above `T_liq`
   liquidates even if the trade bar of that minute has no volume; a missing mark bar is a
   gap and is never rebuilt from trade prices, a missing trade bar never from mark prices
   (example A2h).
3. **A6 contrast (A6, M2, M5, section 8).** A `Closed` status with a delivery at day 9.5
   cannot confirm an end when the missing bars start at day 9.0: the event is dated after
   the gap began, so the episode stays `unresolved`; an event at day 8.5, at or before the
   first missing minute, is an event after entry. M2, M5, 5.3 item 4 and the section 8
   gate now say the confirming source must be dated at or before the first missing minute.

#### Revision 6: the colleague's second read (four problems)

All four were checked against the text and were real. Each is handled once, where the rule
lives, and pinned by an example.

1. **Trigger after a gap (5.3 items 3 and 5, real gap).** A trigger is the first exit only
   on bars continuously covered from the entry bar, trade bars and mark bars. After the
   first missing bar nothing later is read and the episode is `unresolved` from there, even
   if a later bar shows a stop level; only a trigger before the first gap is an exit. A
   zero-trade minute inside a covered source is not missing, and a gap at or after a
   confirmed event is the event (example A2f).
2. **Unresolved in the portfolio (6.1, 5.4, 6.4, real gap).** Slot and loss timing are set
   per scenario and the slot-limited portfolio is computed separately in L and in H. H:
   whole margin lost at `t_last`, slot released then. L: no pnl, slot blocked until `X`.
   The entered sets can differ; both `roc_annual` figures are reported; a portfolio that
   cannot be computed means no result for that scenario (example A7). `not modelled` is gone
   from the table.
3. **Entry clock (5.2).** `T0` is the exact time of the first public trade;
   `E = T0 + age` rounded up to the next whole minute, `X = E + 7 days`; the path cut,
   funding window, benchmark interval and slot timing use these (clock check in A3).
4. **Decomposition without `P_x` (7.3).** A sixth part `EVT` carries the whole price result
   of an episode with no reference exit (confirmed event, unresolved gap); `M`, `S`, `STOP`
   and `EXE` are zero with a flag, and the identity `net = M + S + FND + STOP + EVT - EXE -
FEE` still holds exactly. A positive result carried only by `EVT` is
   `positive_not_robust` (section 8 guard) and the share of net from `EVT` episodes is
   reported (example A4b). One extension beyond the request: a stop or liquidation whose
   planned-exit bar is not covered also has no `P_x` and goes to `EVT`, otherwise the
   identity would not hold for it.

#### Revision 5: the colleague's first read of the rules

The colleague could not open the document and gave conditions on the rules as described.
Each was checked against the text; two were real gaps and two needed clearer wording.

1. **Sign map (section 8, wording).** The map from the signs of L and H to a verdict is one
   table now, and zero is explicitly not positive. A scenario with **no result** (cohort
   below the evidence floor, every episode excluded, no bootstrap) has one status,
   `insufficient_events`, checked before any sign is read.
2. **Trigger window (section 5.3 item 3, real gap).** Triggers (stop, `T_liq`) are
   evaluated only on bars while the position is open, from the entry bar through the
   planned-exit bar inclusive; nothing after the first exit is read. A trigger in the
   planned-exit bar beats the planned exit, in L and in H. New example A2e.
3. **Margin-loss note (section 6.1, wording).** `-margin` is the whole price loss; the
   entry fee is added once, outside the margin; funding is one signed line counted once.
   Checked A2a, A4 (H) and A6 (H) against it. A6 (H) counted 6 settlements where the
   last covered bar allows 5 under the strict rule; corrected (-5,006 bps).
4. **Cost wording (section 6.2, wording).** The 9.5 bps is a spread-and-slippage
   allowance per fill added to the fee, not a fee (L per fill is `phi` + 9.5 bps); 11 bps
   is the VIP 0 taker rate for one fill from the zone FAQ and historical rates are not
   recoverable.

### 13.1 Revision 4: consistency pass, wording and worked examples

Asked for after the second review: neutral contract wording in 5.3, the title in step with
the revision, the conditional-results rule in the verdict section, **Appendix A** (worked
synthetic examples), and a check along one chain: selection (what is known at `E`),
entry, events of an open position (stop, liquidation, contract change, halt), exit
(`actual_exit`), funding and fees on that exit, the verdict and its stop codes.

Found and fixed by the pass (each now says the same thing everywhere):

1. **Path cut anchor (section 2).** The cut counted from launch, the clock from `T0`. Both
   now use `T0`.
2. **`t0_ambiguous` (M1).** It was "unresolved in the denominator", which belongs to the
   after-entry gate. It has no `T0`, is not entered, is excluded at selection and is
   counted in the M1 gate only.
3. **M5 coverage (M4 to M6).** It covered the V1 path; V2 and V3 reach day 21. It now
   covers the planned path of every variant.
4. **Exit rules (6.1, new table).** Liquidation, events after entry and `unresolved`
   episodes had no stated fee, funding or slot rule, and a liquidation was charged an exit
   fee in one reading. One table now gives `price_usd`, `fee_usd`, the funding window and
   the slot release for every exit type; a liquidation or margin-loss scenario charges the
   entry fee only.
5. **Decomposition intervals (7.3).** `M` and `S` used the actual interval while `R_ref` used
   the planned one, so the split did not add up for an early exit. `M` and `S` now use the
   planned window, the rules and the early exit sit in `STOP`, and the guards' `net_adj`
   uses the actual interval.
6. **Slots and the hurdle (5.4, 6.4).** `k` assumes planned holds (early exits only free a
   slot sooner), and `roc_annual` is computed on the slot-limited portfolio while the
   statistics of section 7 use every episode.
7. **Verdict (section 8).** The sign convention is explicit (zero is not positive), and the
   conditional-results rule is written in words: a negative result under the chosen
   assumptions is a reason to stop spending on this protocol, not proof that no edge
   exists.

Rules and examples are tied: the section that states a rule names the example that pins
it, and a later change of a rule must change that example in the same commit.

### 13.2 Revision 3: what changed after the second review (`1f43ade`)

Four defects remained after revision 2. Where each is now handled:

1. **Look-ahead survives without prices.** An evidenced change "dated inside the path"
   removed the whole launch. Now (section 4, M3 and the principle at its start;
   section 5.3, item 4): only evidence **published or dated at or before `E`** may
   exclude a launch, before entry (`change_known_at_entry`). A change that becomes known
   after `E` is an **event of an open position** and is scored in scenarios L and H,
   never deleted.
2. **Liquidation formula.** The formula in the earlier text was wrong. Bybit's primary pages
   (section 5.3) show: in the short formula the maintenance rate sits in the **denominator**,
   `E * (1 + 1/lev) / (1 + MMR)`, about `1.4925 * E` at 2x and 0.5%; Bybit changed the
   margin calculation from 2025-09-02, rolled out gradually with no stated per-account
   date, so no single historical formula applies. Liquidation is now stated plainly as a
   **scenario model** with `T_liq = 1.49 * F_e` primary and `1.46 * F_e` sensitivity on
   the mark-price high, without the words "per the exchange formula"; whole margin lost
   follows from settlement at the bankruptcy price. No loss ceiling is promised.
3. **Scenario H can be cheaper than L.** Right: a flat bar gives H a better fill. The
   scenarios are now **independent assumptions with no ordering** (section 6.2, with the
   synthetic example). Section 8 declares `negative_net_after_costs` only when both are
   at or below zero, `scenario_dependent` when the sign differs in either direction, and
   says a fail under assumed costs is not proof of no edge.
4. **End of archive is not end of trading.** M2's missing-day count no longer treats days
   after the last file as a status exit, M5 says a missing last file is not an end of
   trading, and section 5.3, item 4 requires a **separate source** (instrument status
   or a machine-timed announcement) before any halt or delisting is called an event.
   Without it the episode is `unresolved`, counts against the 95% bars coverage and the
   5% gate, and ends in `incomplete_coverage` when too many.

Also corrected after reading the primary pages: the Innovation Zone fee page was opened
(non-VIP taker 0.1100%, maker 0.0400%, updated 2026-08-07) and says a contract can
migrate between zones in either direction (section 6.2). The earlier statement that
the pages could not be read is replaced by the list of what was read and what was not
(section 5.3).

### 13.3 Revision 2: what changed after the first review (`80802d3`)

1. **Look-ahead selection in M3.** The price-based flag is gone. Identity is judged by
   non-price evidence, and a launch no source can speak for stays in. A price-scale list
   remains only as an outcome-level diagnostic that gates nothing and never excludes.
2. **Loss bound (section 5.3).** The "bounds one episode to about 3,000 bps" claim was
   removed. No loss ceiling is promised; the loss distribution beyond 30% and the worst
   simultaneous stop-out are reported without a cap.
3. **One actual exit (sections 5.2, 6.1).** `actual_exit` drives the funding window, the
   exit fill and fee, the benchmark interval and slot release.
4. **Tape extreme is not a bound (sections 6.2, 8).** `tape_ceiling` is gone; fills are
   scenario assumptions in L and H; order-book coverage is not a gate.
5. **Fee on each fill (section 6.1).** `10,000 * phi * (1 + F_x / F_e)` bps of entry
   notional, so `11 + 11 * 1.3 = 25.3 bps` after a 30% rise.

Staging, as the colleague asked: stage 1 is a data-recoverability check on the
**discovery year only**; the backward holdout is touched only by stage 1b, after stage 1
passes and with the owner's separate go (sections 2, 9, 10 and 11).

### 13.4 Open questions

1. **Q1. Age grid.** V1 at 7 days, V2 at 3, V3 at 14, hold fixed at 7 days. Is a fixed
   hold right, or should the primary be a longer hold (14 days) given funding and cost
   wall? My position: 7 and 7; a longer hold raises overlap and squeeze exposure.
2. **Q2. Right-tail rule (changed).** Isolated margin at 50% of notional, a stop at +30%
   on the last trade price, fills in scenarios L and H, liquidation as a **scenario
   model** (`T_liq` of 1.49 or 1.46 times entry on the mark-price high). Is the stop level right, and should a no-stop variant
   be a counted variant instead of a diagnostic? Should the stop trigger on the mark
   price instead of the last price?
3. **Q3. Eligibility.** All first perp launches (my position) or only new tokens (spot
   history younger than a fixed age)? The second fits the mechanism better but is
   chosen from information I have not looked at and reduces `n` below the floor if
   spot ages are old. I propose the stratum as a diagnostic that gates nothing.
4. **Q4. Clock anchor.** T0 = first public trade (my position) against `launchTime`.
   How should pre-market perps be treated: excluded as `t0_ambiguous` (my position) or
   given their own stratum with a regular-launch anchor?
5. **Q5. Power gate.** MDE of at most 400 bps per weekly episode, `DEFF` from the largest
   of three block lengths. Is the limit right, and is a pre-entry proxy calibration
   acceptable for a return with different volatility after day 7?
6. **Q6. Laboratory strictness.** A 90% block-bootstrap lower bound above zero plus
   guards, against a point-estimate sign rule as in HYP-036. A stricter laboratory
   protects the single exam shot; a laxer one risks wasting it.
7. **Q7. Path cut at the window end.** Censoring launches whose path would leave the
   window (about 5 to 12 launches) to keep the test year unread. Agree, or allow
   tails into the test year?
8. **Q8. Confirmation route.** Is the shared holdout acceptable as I25's only route? Is
   the owner and colleague's view that the test year could be reassigned to I25, with a
   new metadata count, or kept closed? Is a two-quarter forward cohort after v2
   terminal worth planning now, given it falls after the 2027-02-11 gate?
9. **Q9. Costs (changed).** Are L (book VWAP, else a fixed 9.5 bps per side) and H (the
   minute's extreme) the right pair of independent assumptions, with `negative_net` only
   when both fail and `scenario_dependent` when the sign differs? Is 9.5 bps per side
   the right floor for a days-old perp? Is 11 bps per side the right Innovation Zone
   assumption, given that zone membership of a historical launch is unknown and a
   contract can migrate?
10. **Q10. Market adjustment.** `beta = 1` against BTCUSDT and the median of 20 liquid
    perps. Is a pool of 20 right, and is the pooled `beta_hat` enough as a check?
11. **Q11. Staging (settled by the review).** Stage 1 on the discovery year only, then
    stage 1b on the holdout after a pass and a separate go. Open only: should stage 3
    (power calibration) wait for stage 1b, or run on the discovery year first?
12. **Q12. Independent identity evidence.** Contract changes sit in a feed with no
    machine time, and catalog fields exist only as a current snapshot. Is
    `identity_unverifiable` staying in the sample (up to 20%) the right treatment, or is
    a larger unverifiable share a stop?
13. **Q13. Events after entry.** The scenario pair for a confirmed halt, delisting or
    conversion (last trade in L, whole margin lost in H), and the rule that an
    unconfirmed end of archive is a data gap. Is a recorded delivery price better as
    the primary where it exists?
14. **Q14. Exchange rules as sources (changed).** The margin-change notice, the Insurance
    Fund page and the Innovation Zone fee page were read in full; two older liquidation
    pages are no longer served. Agree that stage 2 snapshots the pages with a hash, and
    that the liquidation scenario is kept as a scenario model rather than reproduced from
    a formula whose parameters vary by symbol, tier and date?
15. **Q15. Liquidation scenario values.** `T_liq` of 1.49 (primary) and 1.46
    (sensitivity). Is the maintenance rate assumption of 0.5% and 2.5% reasonable, and
    should the stage 2 contract read the symbol's risk-limit tiers where they are
    available?

16. **Q16. Worked examples.** Are A1 to A9 (with A2e to A2h, A4b and A7b) the right pins, and which further cases should
    become fixtures (for example a status event during a settlement bar, or a stop at
    the entry bar)?

17. **Q17. Strictness of the gap rule.** One missing minute anywhere between the entry bar
    and the first exit (the planned-exit bar when there is no earlier exit) makes the
    episode `unresolved` (5.3 item 3), so the 5% gate
    measures bar coverage at the level of single minutes. My position: keep it, because a
    single missing minute can hide the trigger bar and a milder rule would need a
    judgement made after seeing prices; the share is measured at stage 1, before any
    return, and a zero-trade minute is explicitly not missing. Do you want a tolerated
    gap length?

18. **Q18. Lookback, funding span and funds.** (a) Each variant is cut by its own age
    (`V1` from 2024-10-24, `V2` from 2024-10-28, `V3` from 2024-10-17) and the shape
    across age is read on the common set; would you rather use one set (the latest cut,
    2024-10-28) for all three? (b) Funding coverage is required over days 3 to 21 after
    `T0`, the union of the variants' paths, so one set stays eligible for all; is that the
    right span? (c) The funds check counts realised pnl only; should unrealised losses
    reduce the cash as well? My positions: (a) own cuts, shape on the common set, (b)
    yes, (c) realised only, because isolated margin already caps each position at its own
    margin.

## 14. Not in this document

- any fetch, price, return, spread, depth, funding or open-interest value;
- any implementation, contract JSON or request list (stage 2);
- a second venue, Hyperliquid launches (`I22`) or a cross-venue leg;
- spot-margin shorts, options, or any instrument other than Bybit linear USDT
  perpetuals as the only execution venue;
- any order, paper run or LIVE_PROBE.

## Appendix A. Worked synthetic examples (rules pinned by numbers)

Invented prices, **no market data**, no code. Each example fixes a rule of the protocol
with a result computed by hand, so that a later change of a rule has to change an example
in the same commit. They are small on purpose: a later test can use them as fixtures.

**Common set-up** unless an example says otherwise: entry sell at `F_e = P_e = 100`,
quantity `q = 10` (notional 1,000, margin 500), `phi = 0.0011` (11 bps per side),
stop level `S = 130`, liquidation scenario `T_liq = 149` (1.49 times entry, on the
mark-price high), allowance 9.5 bps per side (`0.00095`) in scenario L, funding rate
`r = 0.0001` where funding appears, `borrow = 0`. Results are in USD and in bps of the
entry notional (`net_bps = 10,000 * net_usd / 1,000`).

### A1. The fee on each fill (section 6.1)

Planned exit at the reference price, no funding, to isolate the fee.

| Case     | `F_x` | `price_usd` | `fee_usd`                        | `fee_bps` | `net_usd` | `net_bps` |
| -------- | ----: | ----------: | -------------------------------- | --------: | --------: | --------: |
| 30% rise |   130 |        -300 | `1.1 + 0.0011 * 10 * 130 = 2.53` |      25.3 |   -302.53 |  -3,025.3 |
| 30% fall |    70 |        +300 | `1.1 + 0.0011 * 10 * 70 = 1.87`  |      18.7 |   +298.13 |  +2,981.3 |

Check: `10,000 * phi * (1 + F_x / F_e)` gives `11 * 2.3 = 25.3` and `11 * 1.7 = 18.7`.
A fixed 22 bps round trip would be wrong in both rows.

### A2. Stop and liquidation inside one bar (section 5.3, items 2 and 3)

Trade-price OHLC and mark-price high of the bar after entry, no funding.

| Case                                             | Bar                                             | Rule applied                                           | Result L                                                                                                          | Result H                                                                             |
| ------------------------------------------------ | ----------------------------------------------- | ------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------ |
| A2a. Same bar reaches stop and liquidation       | trade high 135, mark high 150 (at or above 149) | liquidation applies, whole margin lost, entry fee only | `-500 - 1.1 = -501.1` USD, **-5,011 bps**                                                                         | same: **-5,011 bps**                                                                 |
| A2b. Gap open beyond the stop, below liquidation | trade open 132, high 140, mark high 141         | the stop triggers at that bar                          | `F_x = 132 * 1.00095 = 132.1254`; price `-321.254`; fee `1.1 + 1.45338 = 2.55338`; **-323.81 USD, -3,238.07 bps** | `F_x = 140`; price `-400`; fee `1.1 + 1.54 = 2.64`; **-402.64 USD, -4,026.4 bps**    |
| A2c. Gap open beyond the liquidation level       | mark open 155 (at or above 149)                 | liquidation applies at once                            | **-5,011 bps** as in A2a                                                                                          | **-5,011 bps**                                                                       |
| A2d. Stop reached inside a bar, open below stop  | trade open 110, high 131, mark high 120         | the stop triggers, fill `max(S, open)` in L            | `F_x = 130 * 1.00095 = 130.1235`; price `-301.235`; fee `1.1 + 1.43136 = 2.53136`; **-303.77 USD, -3,037.66 bps** | `F_x = 131`; price `-310`; fee `1.1 + 1.441 = 2.541`; **-312.54 USD, -3,125.41 bps** |

In A2b and A2d the loss exceeds 30% of notional (3,000 bps), which is why no loss ceiling
is promised. In A2a and A2c the loss is the whole margin, whatever the bar's high.

**A2e. The trigger window (section 5.3, item 3).** The planned-exit bar (the bar that
ends at `X`) has trade open 110, high 131, close 112, mark high 120; no funding, as in A2d.
Position open, so the bar is evaluated and the trigger wins over the planned exit:

- **Result L:** the stop applies at `max(S, open)` plus the allowance, exactly as A2d:
  **-303.77 USD, -3,037.66 bps**, exit type stop, `t_exit = X`. **Result H:** fill at the
  bar high 131, as A2d: **-312.54 USD, -3,125.41 bps**. Had the planned exit been used
  instead, L would be `F_x = 112 * 1.00095 = 112.1064`, price `-121.064`, fee
  `1.1 + 1.23317`, net **-123.40 USD, -1,233.97 bps** (H would be the same as the stop,
  the bar high): the trigger rule is what separates the two in L.
- **After the first exit nothing is read.** A mark high of 155 in a bar **after** the
  planned-exit bar, or after the stop of A3 fired at hour 30:05, does not liquidate and
  does not change the result: the episode already ended, its funding window and slot are
  those of the first exit.

**A2f. A gap hides the trigger (section 5.3, items 3 and 5).** Trade and mark bars are
present from the entry bar to hour 30:00 after `E` with no trigger. The bars of hour 30:00
to 31:00 are **missing** (the source is absent, no confirmed event). The bar that starts at
hour 31:00 has trade open 110, high 131, mark high 120, as in A2d. No funding, as in A2d.

- **Classification:** the first missing bar starts at hour 30:00, so the episode is
  `unresolved` from there, `t_last = 30:00`. The stop-level bar after the gap is **not
  read**. **L:** out of the mean, its entry fee (`-1.1` USD) stays in the L ledger, no funding in
  this toy, slot blocked until `X`. **H:** whole margin lost
  at `t_last`, entry fee only: `-500 - 1.1 = -501.1` USD, **-5,011 bps**.
- **Why not the stop.** Reading it would give `-303.77` USD (L) and `-312.54` USD (H) as
  in A2d, but an unseen liquidation inside the gap could have given `-501.1` (A2a). The
  rule does not guess the milder outcome.

**A2g. A stop with no planned-exit bar (sections 5.3 and 7.3).** The stop of A2d fires in
the bar that starts at hour 30:05 (covered, first exit); no funding, as in A2d. The
planned-exit bar at `X` is **not covered**, so there is no `P_x`: the episode is **not
unresolved** (the stop came first and nothing after it is read) but it has **no reference
exit**, flag `no_reference_exit`, `M = S = STOP = EXE = 0`, and its whole price result is
`EVT` from the stop row of 6.1 in that scenario.

| Scenario | `price_usd` | `EVT` bps | `FND` | `FEE` bps | `net = EVT + FND - FEE`                                 |
| -------- | ----------: | --------: | ----: | --------: | ------------------------------------------------------- |
| L        |    -301.235 | -3,012.35 |     0 |   25.3136 | `-3,012.35 - 25.3136 = -3,037.66` bps = **-303.77 USD** |
| H        |        -310 |    -3,100 |     0 |     25.41 | `-3,100 - 25.41 = -3,125.41` bps = **-312.54 USD**      |

Fee check: L `1.1 + 1.43136 = 2.53136` USD (25.3136 bps of 1,000); H `1.1 + 1.441 = 2.541`
USD (25.41 bps). The nets equal A2d exactly, so the identity holds with `EVT` carrying the
stop's price result.

**A2h. A minute with no trades, a mark high above `T_liq` (section 5.3, item 3).** In the
minute after entry no trade occurs: the trade bar is the flat zero-volume bar at 100; the
mark-price bar of the same minute is covered with high 150, at or above `T_liq = 149`.

- **Result:** liquidation applies, whole margin lost, entry fee only: `-500 - 1.1 = -501.1`
  USD, **-5,011 bps** in L and in H (as A2a). The empty trade bar does not cancel the mark
  test.
- **If the mark bar of that minute were missing** the episode would be a gap (`unresolved`),
  not rebuilt from the flat trade bar, and a missing trade bar is likewise never rebuilt
  from mark prices.

### A3. An early `actual_exit` (sections 5.2 and 6.1)

The stop of A2d fires in the bar that starts at hour 30:05 after `E`, so
`t_exit = 30:06`. Settlements every 8 hours after `E`; marks at the settlements 101, 103
and 104; `r = 0.0001`.

- **Funding counted:** settlements at hours 8, 16 and 24 (`E < s < t_exit`); the one at
  hour 32 is after the exit. `funding_usd = 0.0001 * 10 * (101 + 103 + 104) = +0.308`. A
  planned exit at `X` = 168 h would have counted 20 settlements (8 to 160; hour 168 is not
  `< X`), so the early exit must not use them.
- **Fees:** the entry fee `1.1` and the exit fee on the stop fill only; no planned-exit
  fill and no second exit fee.
- **Slot:** released at hour 30:06, not at hour 168; the capital-time of section 6.4 is
  30.1 hours of margin for this episode.
- **Result L:** `-301.235 + 0.308 - 2.53136 = -303.458` USD, **-3,034.58 bps**.
  **Result H:** `-310 + 0.308 - 2.541 = -312.233` USD, **-3,122.33 bps**.
- **Clock check (section 5.2).** `T0 = 12:34:17.250` on day 0 with age 3 days gives
  `T0 + age = 12:34:17.250` on day 3, rounded **up** to the next whole minute:
  `E = 12:35:00`. `T0 = 12:34:00.000` exactly gives `E = 12:34:00` (unchanged). For
  `E = 12:35:00` on day 3, `X = 12:35:00` on day 10, the entry bar starts at `E`, the
  planned-exit bar starts at `12:34:00` on day 10 (it ends at `X`) and the settlements
  counted satisfy `12:35:00 < s < t_exit`.

### A4. A contract change announced after entry (section 4 M3, section 5.3 item 4)

Entry at day 7.0. A machine-timed announcement, **published at day 10.0**, says the
contract converts at day 10, 12:00. The conversion is a confirmed event after entry, and the bars are covered up to it.

- **Selection:** the evidence is dated after `E`, so it cannot exclude the launch. The
  episode **is not deleted**. Had the same announcement been published at day 6.0, before
  `E`, the launch would be `change_known_at_entry`, excluded before entry and counted in
  M3 (no episode exists, so nothing is scored).
- **Scoring.** Entry at day 7.0 and the event at day 10, 12:00 are 84 hours apart, so on
  the 8-hour schedule of A3 the settlements at hours 8, 16, ..., 80 count (**10**; the next,
  at hour 88, is after `t_exit`). Marks at those settlements: 99, 99, 98, 98, 97, 97, 96,
  96, 95, 95 (sum 970). `funding_usd = 0.0001 * 10 * 970 = +0.97`. Last trade before the
  event `F_last = 92`.
  - **L:** price `10 * (100 - 92) = +80`; fee `1.1 + 0.0011 * 10 * 92 = 2.112`; net
    `80 + 0.97 - 2.112 = +78.858` USD, **+788.58 bps** (labelled non-executable).
  - **H:** whole margin lost: `-500 + 0.97 - 1.1 = -500.13` USD, **-5,001.3 bps**.
- **Verdict effect.** If this were the whole cohort, L is positive and H is negative:
  `scenario_dependent` (A5 explains the label). It is neither dropped nor a pass.

### A4b. The decomposition of the A4 episode (section 7.3)

The event ends the episode at day 10, 12:00, before `X` (day 14.0): there is **no `P_x`**,
so the episode is flagged `no_reference_exit` and `M = S = STOP = EXE = 0`. Entry notional
`q * F_e = 1,000` USD, so 1 USD is 10 bps.

| Scenario | `M` | `S` | `FND` | `STOP` |  `EVT` | `EXE` | `FEE` | `net = M + S + FND + STOP + EVT - EXE - FEE` |
| -------- | --: | --: | ----: | -----: | -----: | ----: | ----: | -------------------------------------------- |
| L        |   0 |   0 |  +9.7 |      0 |   +800 |     0 | 21.12 | `9.7 + 800 - 21.12 = +788.58` bps            |
| H        |   0 |   0 |  +9.7 |      0 | -5,000 |     0 | 11.00 | `9.7 - 5,000 - 11 = -5,001.3` bps            |

These equal the A4 results exactly (`+788.58` and `-5,001.3`). `FND = 10,000 * 0.97 / 1,000
= 9.7`; `FEE` in L is `2.112` USD (`0.0011 * 10 * (100 + 92)`), in H the entry fee `1.1` USD.
**Guard.** In L the whole positive result is `EVT`, a non-executable last-trade gain: with
the positive `EVT` set to zero the net is `9.7 - 21.12 = -11.42` bps, not above zero, so a
cohort that passed on the strength of such episodes would be `positive_not_robust`, never
`lab_positive_hypothesis`.

### A5. The flat candle where H is cheaper than L (section 6.2, section 8)

All bars flat at 100, no order book, planned exit with 20 settlements (hours 8 to 160)
at `r = 0.00015` and marks of 100: `funding_usd = 0.00015 * 10 * 100 * 20 = +3.0`.

| Scenario |  `F_e` |   `F_x` | `price_usd` | `fee_usd`                                 | `net_usd` | `net_bps` |
| -------- | -----: | ------: | ----------: | ----------------------------------------- | --------: | --------: |
| L        | 99.905 | 100.095 |       -1.90 | `0.0011 * 10 * (99.905 + 100.095) = 2.20` |     -1.10 |    -11.01 |
| H        |    100 |     100 |        0.00 | `0.0011 * 10 * (100 + 100) = 2.20`        |     +0.80 |     +8.00 |

H is cheaper than L here, and the sign differs. If every episode of the cohort looked
like this, the verdict is **`scenario_dependent`**: not `negative_net_after_costs` (that
needs both at or below zero) and not a pass. In a wide bar the order reverses, which is
why neither scenario is called the lower-cost one.

### A6. End of archive without a confirming halt (section 5.3, items 4 and 5, M5)

Entry at day 7.0. The archive's last file for the symbol ends at day 9.0; there are no
files for days 10 to 14. `instruments-info` still shows `Trading` and no machine-timed
announcement exists.

- **Classification:** a **data gap**, not a status exit and not an event after entry. The
  episode is `unresolved`, counts against the 95% bars coverage of M5 and the 5% gate.
- **Scoring.** **L:** out of the mean (counted in the coverage denominator), its known cash flows stay
  in the L ledger (entry fee `-1.1` and the same 5 settlements `+0.5`, so `-0.6` USD as a
  portfolio quantity, price pnl not credited), slot blocked until `X`. **H:** whole
  margin lost, entry fee only, funding up to the last covered bar. That bar ends at day
  9.0, 48 hours after `E`: the settlements at hours 8, 16, 24, 32 and 40 count (**5**,
  marks of 100); the one at hour 48 has no covered mark bar and is not counted (strict
  `s < t_exit`). `-500 + 0.0001 * 10 * 100 * 5 - 1.1 = -500.6` USD, **-5,006 bps**.
- **Benchmark interval (section 7.3).** An `unresolved` episode has no actual exit; in H
  its market-adjusted interval ends at `t_last` (day 9.0), the same time as the loss. It
  has no reference exit, so `M = S = 0` and only `net_adj` uses the interval: with BTCUSDT
  at `R_bench = -0.02` over `[E, t_last]`, `net_adj = -5,006 + 10,000 * (-0.02) = -5,206`
  bps. In L the episode is out of the mean and has no interval.
- **Gate.** With 200 episodes the limit is 10 unresolved; 12 unresolved is 6% and ends
  the study as `incomplete_coverage` (section 8, order 2); `entry_unavailable` episodes
  count in the same share (A9).
- **Contrast, event after the gap began.** The first missing minute starts at day 9.0. If
  `instruments-info` showed `Closed` with a delivery time at day **9.5**, the event is
  dated **after** the gap began, so it does not rescue the episode: it **remains
  `unresolved`** (5.3 item 3; the last trade before the event lies beyond the gap).
- **Contrast, event at the gap.** If the delivery time were day **8.5** and the first
  missing minute were at or after it, the missing bars are the event, not a gap: the
  episode is an **event after entry**, scored as in A4 (L: the last-trade result of the
  covered bars, non-executable; H: whole margin lost).

### A7. An unresolved episode in the portfolio (sections 5.4, 6.1 and 6.4)

A toy portfolio with `C` = USD 1,000, **`k = 2`**, `N = 500` (`q = 5` at 100, margin 250,
entry fee `0.0011 * 5 * 100 = 0.55`), window of 14 days. **U** enters at day 2.0 in slot 1
(`X_U` = day 9.0); its trade and mark bars are present until day 2.5 (`t_last`, 12 hours
after entry) and missing after, with no confirmed event: `unresolved`. **V** enters at day
2.2 in slot 2 and **W** has a signal at day 3.0; V and W have the bars of A5 (flat 100, no
book, 20 settlements at `r = 0.00015`), so with `q = 5` each gives **-0.55** USD in L and
**+0.40** USD in H (half of A5: price `-0.95` and `0`, funding `+1.5`, fee `1.10`).

- **L portfolio.** U's price result is unknown and not credited, but its cash flows are
  real: entry fee `-0.55` and funding `+0.05` (one settlement, as in H) give `-0.50` USD in
  the ledger; its slot stays **blocked until day 9.0**; V holds slot 2. W's signal at day
  3.0 finds no free slot: skipped, reason `slot_blocked_by_unresolved`. Entered set
  `{U, V}`, **result included in the mean** `{V}`. `sum net = -0.50 - 0.55 = -1.05` USD,
  `roc_annual = -1.05 / 1,000 * 365 / 14 = -0.027375`, **-2.7375% a year**.
- **H portfolio.** U loses its whole margin at `t_last` and frees slot 1 then. Funding: one
  settlement counts (hour 8; the next is after `t_last`), marks of 100, `r = 0.0001`:
  `0.0001 * 5 * 100 = +0.05`. `net_U = -250 + 0.05 - 0.55 = -250.50` USD, **-5,010 bps**.
  Cash at W's entry from the ledger: `1,000 - 250.50` (U closed) `- 0.55` (V's entry fee)
  `+ 0.15` (V's two settlements, `2 * 0.00015 * 5 * 100`) `- 250` (V's margin reserved)
  `= 499.10`, at least the `250.55` needed, so W enters and gives `+0.40`. Entered set `{U, V, W}`,
  `sum net = -250.50 + 0.40 + 0.40 = -249.70` USD,
  `roc_annual = -249.70 / 1,000 * 365 / 14 = -6.51`, that is **-651% a year**.
- **Why both figures.** The entered sets differ (`{U, V}` against `{U, V, W}`), so one
  portfolio cannot stand for both scenarios: each is computed on its own and both
  `roc_annual` figures are reported.

### A7b. The funds check (section 5.4)

The first toy again: `C` = USD 1,000, **`k = 1`**, `N = 1,000` (`q = 10`, margin 500, entry
fee 1.1). **U** enters at day 2.0 and is `unresolved` from day 2.5; **W** has a signal at
day 3.0.

- **H.** U loses its whole margin at `t_last`: funding `0.0001 * 10 * 100 * 1 = +0.1`,
  `net_U = -500 + 0.1 - 1.1 = -501.0` USD, **-5,010 bps**. Cash after it
  `1,000 - 501.0 = 499.0`, below the `500 + 1.1 = 501.1` that W needs: W is
  **`skipped_insufficient_funds`**, size is not reduced and nothing is topped up. Entered
  set `{U}`, `roc_annual = -501.0 / 1,000 * 365 / 14 = -13.06`, that is **-1,306% a year**.
- **L.** U's price result is not credited; its entry fee and funding stay in the ledger
  (`-1.1 + 0.1 = -1.0` USD) and it keeps the slot until day 9.0; W is skipped for the slot
  (`slot_blocked_by_unresolved`; the cash `1,000 - 1.0 - 500` reserved `= 499.0` would
  also be below 501.1, and the first reason found, the slot, is recorded). Entered set
  `{U}`, result included in the mean: empty. `sum = -1.0` USD,
  `roc_annual = -1.0 / 1,000 * 365 / 14 = -0.02607`, **-2.607% a year**.
- W is skipped in both, for different reasons, and the reason is recorded per scenario.
  The two entered sets are the same here, `{U}`, with very different results.

### A7c. The cash ledger (section 5.4)

`C` = USD 1,000, `k = 2`, `N = 500` (`q = 5`, margin 250, entry fee 0.55). The ledger holds
closed episodes with a net of `-499`. One position is open, **its entry fee 0.55 already
debited**, no funding yet, with margin 250 reserved.

- `available_cash = 1,000 - 499 - 0.55 - 250 = 250.45`. Counting only the margin
  (`1,000 - 499 - 250 = 251`) would be wrong.
- The next entry needs `250 + 0.55 = 250.55`, above `250.45`: **`skipped_insufficient_funds`**.

### A8. A pass carried by a non-executable gain (sections 7.3 and 8)

A portfolio with `C` = USD 1,000 over a window of 365 days in scenario L. `sum net_usd =
+60.0`, of which **+20.0** is the positive `EVT` of confirmed-event episodes (non-executable
last-trade gains), so `sum exec_net_usd = 60.0 - 20.0 = +40.0` (the entered set is the same
in this first toy; A8b shows a case where it is not).

- `roc_annual` on `net` is `60.0 / 1,000 = 6.0%`, above the 5% barrier. On `exec_net` it
  is `40.0 / 1,000 = 4.0%`: **2 points come from the non-executable gain, 4 points do not**.
- The 5% barrier **fails on `exec_net`**, so the verdict is **`positive_not_robust`** even
  though `net` passes (section 8, order 9). The same two-sided evaluation applies to the
  90% bound, the top-5 removals, the half-year signs and the market-adjusted guard.

### A8b. The money path of `exec_net` (sections 5.4 and 7.3)

Scenario L, `C` = USD 1,000, `k = 1`, `N = 1,000` (margin 500, entry fee 1.1), window of 365
days, episodes one after another. **E1** is a confirmed event: `price_usd = +100` (the
non-executable last-trade gain, `EVT = +100`), `net_usd = +97.91` (fees less funding 2.09).
**E2** is a liquidation: `-500 - 1.1 = -501.10`. **E3** needs `500 + 1.1 = 501.10`; if
entered it nets `+30.0`.

| Path            | E1 in the ledger      | After E2 cash                     | E3 (needs 501.10)                                      |
| --------------- | --------------------- | --------------------------------- | ------------------------------------------------------ |
| L on `net`      | `+97.91`              | `1,000 + 97.91 - 501.10 = 596.81` | **entered** (596.81 at least 501.10)                   |
| L on `exec_net` | `97.91 - 100 = -2.09` | `1,000 - 2.09 - 501.10 = 496.81`  | **`skipped_insufficient_funds`** (496.81 below 501.10) |

- **Entered sets:** `{E1, E2, E3}` on `net`, `{E1, E2}` on `exec_net`. The gain of E1 would
  have funded E3 only on the `net` path.
- **`roc_annual`:** on `net` `sum = 97.91 - 501.10 + 30.0 = -373.19`, `-373.19 / 1,000 =`
  **-37.32%**; on `exec_net` `sum = -2.09 - 501.10 = -503.19`, **-50.32%**. Each guard uses
  its own path.

### A9. A missing entry bar (sections 3 and 4, M5)

A launch reaches `E = 12:35:00` on day 7. The trade bar that starts at `E` is **missing**
from the bar listing (existence only), while the bars from `12:36:00` on exist. The episode
is **`entry_unavailable`**: no `P_e`, `F_e`, `q`, fee or margin is defined, no position is
modelled, no slot is taken, nothing is scored and it is not in the denominator of net. It
counts in the 5% gate: with 200 eligible episodes, 4 `entry_unavailable` plus 7
`unresolved` is 11, that is 5.5%, above 5%, so the study ends as `incomplete_coverage`.
