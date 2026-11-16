# Catalyst feasibility v1 (contract, 2026-11-15)

Status: **design discussed with the colleague on 2026-11-15; their fixes are folded in
(section 11).** It authorizes no fetch. The code and the seen-windows registry row go
to one colleague review. The fetch itself needs a separate go from the owner.

Study id: `catalyst-feasibility-v1`. No HYP id is assigned. One is assigned only if a
laboratory protocol follows (section 9).

**Naming.** This is direction A of the 2026-11-15 discussion: external catalysts, with
spread and depth as the execution layer. It is not the [program plan](program-plan-2026-11.md)'s
track A, which was the closed family of candle signals.

## 1. Question

Do enough catalyst events exist for a laboratory read on the discovery year and an exam
on the backward holdout? An event counts only if:

- its announcement time is reproducible to the second;
- its base asset had a live Bybit USDT perpetual at that time;
- its identity is unambiguous.

This study reads **no market values**: no prices, trades, spreads, order books, funding
rates, open interest or returns. It counts events and checks their timestamps. Under the
[registry](seen-windows-registry.md)'s levels, both windows stay at **coverage** level
and remain usable for confirmation.

**Why this class.** Every short-horizon pattern on 1-minute candles and volume of
alt-perps has failed after retail taker costs (HYP-012, -015, -029, -031, -036 and the
2026-11-11 series). A catalyst is a different data class: the signal is an outside
event, not the price path. Its effects, if real, are large enough to be powered on tens
of events rather than thousands. This study first checks whether those tens exist and
can be timed.

## 2. Windows

| Window           | Dates (announcement time, UTC)                               | Role here                             |
| ---------------- | ------------------------------------------------------------ | ------------------------------------- |
| Discovery year   | 2024-10-01T00:00:00Z to 2025-09-30T23:59:59.999Z             | counts; future laboratory             |
| Backward holdout | 2023-02-01T00:00:00Z to 2024-09-15T23:59:59.999Z             | counts only; its outcomes stay unread |
| Anything else    | before 2023-02-01; 2024-09-16 to 2024-09-30; from 2025-10-01 | discarded unparsed (section 6)        |

The HYP-031 test year (2025-10-01 to 2026-09-28) is excluded even though it is before
the v2 blind window. It is partly exposed and unassigned, and this study does not need
it.

## 3. Buckets

Fixed before counting. An event is one base asset in one article, and it goes into
exactly one bucket. Every bucket is counted and judged separately; buckets are never
pooled for a verdict.

| Bucket | Event                                                                                      | Sources                                                              |
| ------ | ------------------------------------------------------------------------------------------ | -------------------------------------------------------------------- |
| A1     | External **spot** listing of a base that already has a live Bybit USDT perp                | Binance spot, Upbit (KRW and USDT markets), Coinbase                 |
| A2     | **Binance USDⓈ-M futures** listing of a base that already has a live Bybit USDT perp       | Binance                                                              |
| A3     | External **delisting** or **monitoring/warning tag** on a base with a live Bybit USDT perp | Binance (delist, monitoring tag), Upbit (investment warning, delist) |
| A4d    | **Bybit delisting** notice for a live perp                                                 | Bybit announcements                                                  |
| A4f    | **Bybit funding interval change** on a live perp                                           | Bybit announcements                                                  |
| A4r    | **Bybit leverage or risk limit change** on a live perp                                     | Bybit announcements                                                  |

**Priority.** If one article announces both a spot and a USDⓈ-M futures listing of the
same base, the event is A1: the spot listing is the wider catalyst, and A2 stays the
futures-only case. Each source's own catalog or type decides the bucket, and a title
pattern only splits it further. Both are fixed in the code PR before any count.

**Why A4 is three buckets.** A delisting, a funding interval change and a risk limit
change are different mechanisms. One service article on risk limits for 30 to 50 perps
must not hide a small number of real delistings, so each has its own verdict.

**Excluded and counted apart:**

- Bybit's own new perp listings: at announcement time the perp does not exist yet or
  its book is empty. Their type (`new_crypto`) is not requested at all;
- events on a base with no live Bybit USDT perp at announcement time;
- launchpools, airdrops, competitions, earn and campaign notices, unless the same
  article is itself a listing in A1 or A2;
- articles that list several bases count once per base. Each base is a separate event
  and its own cluster, but the bases of one article are one announcement (below).

**Clusters.** One cluster per base asset and UTC day, within a bucket. If Binance and
Upbit list the same base on the same day, that is two events and one cluster.

**Announcements.** One announcement per article (`source`, article id), within a
bucket. One article about 25 perps is 25 events, 25 clusters and one announcement: it
is one piece of news at one moment. Clusters guard against one base counted twice;
announcements guard against one article counted many times. The minimums are on both
(section 7).

## 4. Timestamps and provenance

Each event carries:

- `announced_at`: the source's publication time for the article;
- `effective_at`: when the stated change takes effect (trading opens, delisting, new
  interval). It is optional, taken only from a structured field or a fixed title
  pattern, never from free-form text;
- `source_id`: the article id;
- every other timestamp the source returns, kept raw.

For every source the study reports, per window:

1. **Precision.** The share of articles with `announced_at` at second or finer
   precision. An article without it is `not_timed` and does not count. A
   millisecond field can still hold a rounded time, so precision is also judged per
   source and window: if more than 20% of its articles fall on a whole minute (about
   1.7% would by chance), the field is treated as minute-rounded and all of that
   source's articles in the window are `not_timed`.
2. **Edits.** Some sources return more than one time. Bybit's `dateTimestamp` is
   described as filled in by the author, while `publishTime` is the publication time;
   Upbit returns first-listed and updated times. The canonical `announced_at` per
   source is fixed in the code, before any count is read. It is the earliest
   machine-set publication time, never an author-filled one. No source documents
   whether that field is rewritten on edit: Bybit's documentation says only "the
   published timestamp", and Binance and Upbit have no documentation (section 6). The
   order check below is therefore the evidence on edits. The report gives, for every
   other time of the same article, the share that differs from `announced_at` by more
   than 60 s. Only machine-set times gate the verdict (section 7); author-filled times
   are reported only.
3. **Order consistency.** The share of article pairs where id order and `announced_at`
   order disagree. Backdated or republished articles show up here. Pairs are compared
   only within one catalog or type of a source (for Binance: 48, 161 and 49 apart). Ids
   of different catalogs need not follow publication order: if an id is set when a
   draft is created, a notice drafted days ahead would disagree with a later listing
   without any backdating. How the publisher assigns ids is not known, so this is a
   precaution, not a finding. The gate is the largest per-catalog share; the pooled
   share is reported beside it and gates nothing.
4. **Effective after announced.** The share of events with `effective_at` before
   `announced_at`; such events are flagged, not dropped.

Checks a source cannot support are reported as `unobservable` and do not gate: Binance
and Bybit return one machine time per article, so edits are unobservable there, and
Bybit articles have no numeric id, so order consistency is unobservable for Bybit.

**What history cannot show.** History cannot show the delivery lag: when a bot polling
the source would really have seen the article, or a CDN that serves it late. Only a
live collector can measure that (section 10). This study therefore calls a source
`timed`, never `deliverable`.

## 5. Live Bybit perp at announcement time

A base has a live Bybit USDT perp at `announced_at` if a linear USDT contract for it:

- launched at least 24 hours before `announced_at`, by the instrument's `launchTime`;
- had not been delivered or closed by then, by `deliveryTime` for closed instruments.

The universe is all linear USDT instruments returned by `instruments-info` across
`Trading`, `Delivering` and `Closed`. The public trade archive's file listings are not
an input to this module: this study measures the coverage of Bybit's API catalog, and
any reconciliation against the trade archive remains a separate step before a
laboratory protocol relies on catalog completeness.

Instruments are cut the same way as announcements (section 6), so no symbol that
appeared in or near the blind window is kept.

**Identity.**

- Multiplier tickers (`1000PEPE`, `1000000MOG`, `SHIB1000`) map to their base by
  stripping the multiplier, on both the source and the Bybit side.
- A base with more than one live Bybit USDT perp at `announced_at` (for example
  `PEPEUSDT` and `1000PEPEUSDT`) is `identity_ambiguous`. It is reported and does
  not count.
- The match is at ticker level. Under the [identity cases](instrument-identity-cases.md),
  a same-ticker match is not proof of the same asset, and a ticker reused by a
  different project cannot be detected from catalog metadata. The counts are
  therefore an upper bound on identity. A laboratory protocol must verify the
  identity of every event it reads, before any price.

## 6. Requests and the blind window

**Sources and endpoints.** Pinned in the code (`catalyst_feasibility.py`, `SERIES`):

- Bybit `GET /v5/announcements/index`, `locale=en-US`, types `delistings` and
  `product_updates`, 20 per page. It is documented, has no time filter and pages
  from the newest. Canonical time `publishTime`; `dateTimestamp` is author-filled
  and reported only.
- Bybit `GET /v5/market/instruments-info`, linear, statuses `Trading`, `Delivering`
  and `Closed`, by cursor.
- Binance's public announcement list (`bapi/composite/v1/public/cms/article/list/query`),
  catalogs 48 (new listings), 161 (delisting) and 49 (news, for monitoring tags), 20
  per page. Canonical time `releaseDate`.
- Upbit's public notice list (`api-manager.upbit.com/api/v1/announcements`), all
  categories, 20 per page. Canonical time is the earlier of `first_listed_at` and
  `listed_at`; the later one is the edit check.
- Coinbase is not requested. No second-precision listing history is known, so its
  source is `not_reconstructible` and A1 rests on Binance and Upbit. The negative is
  recorded in the report.

**Undocumented endpoints.** Binance and Upbit publish no documentation for these
lists; the documentation check on 2026-11-15 found none. Their schemas in the code
are therefore expected shapes, not confirmed ones. The guards:

- the first page of every series records a **schema probe**: the key paths and value
  types of the response, never a value;
- any mismatch stops the whole fetch closed, with the same key-only record and a
  `failure` record: the exception class, the stage (`envelope` or `item`), the item
  index and a reason from a fixed vocabulary (for example `missing:publishTime`). The
  key tree is a union over the items of a page, so one malformed item leaves it
  unchanged; the `failure` record is what names it. It holds no value, title or symbol;
- the fix is a reviewed code change. A stopped run does not resume: its run binding
  holds the code revision, so a corrected revision starts a **new run in a new root**,
  from the first page. The stopped run's receipts are kept, passed to the new run via
  `--prior-root` so their requests and bytes count against the same 3,000-request and
  64 MiB study budget, and named in the report's provenance. A transport failure on the
  same revision does resume, up to three attempts per page;
- for edits, the order check (section 4) is the evidence, and the report says so.

**Articles with no machine time.** The first live fetch stopped on a Bybit
announcements page with an article that has no usable `publishTime`. For the numeric-time
announcement feeds (Bybit and Binance only) an article whose canonical time is absent,
zero or negative is not a schema change: it is counted per page and dropped unparsed.
It is placed in no window, because a window needs a machine time, and it never sets the
time boundary of a feed. An author-filled time (`dateTimestamp`, `startDateTimestamp`)
is never used as a fallback: on one early page it differed from `publishTime` by up to
92 days. Any other malformed time (a string, null or bool) still stops the fetch. The
count per source is reported as `untimed_without_machine_time`. It is the number of
articles that may hold real events and could not be read, so a short count in a bucket
must be read against it. The fetch stops if more than 10% of one page's articles are
untimed (reason `untimed_share_above_limit`), which also caps the total at 10%.

**Blind window and other windows.** The announcement lists have no time filter, so
pages dated on or after 2026-09-29 pass through pagination. The guards:

1. **Timestamp first.** The fetcher reads only each item's canonical machine
   timestamp. If it is outside the two windows, the whole item is dropped before
   anything else is parsed: no title, symbol or log line. Only a count of dropped
   items per page is kept.
2. **Instruments too.** For `instruments-info` only `symbol`, `launchTime`,
   `deliveryTime` and `status` are read. An instrument with `launchTime` on or after
   2025-10-01 is dropped the same way, and its symbol is neither kept nor logged. A
   `deliveryTime` on or after 2025-10-01 is stored only as "not delivered before
   2025-10-01", and then `status` is not kept either, so no delisting in the test year
   or the blind window is recorded.
3. **No raw bodies.** A response body is never persisted. The ledger keeps the
   SHA-256 of the full body and, per kept item, its original JSON object. That allows
   a corrective re-parse without a re-fetch.
4. **No dumps on errors.** A parse or validation error records the request, the error
   class and the body hash, never the body or an item. The attempt fails closed.
5. **No manual look.** During development no live page is opened by hand, the newest
   page included. The parser is built and tested on documented schemas and synthetic
   fixtures. A schema mismatch at fetch time fails closed, and the fix goes through a
   code change.

**Budget.**

- At most **3,000 requests** in total, **64 MiB** of response bytes and **1 request per
  second** per host.
- An announcement series ends in one of three ways that count as read to its end: an
  empty page, the time boundary, or the end of a cursor. The time boundary is reached
  when the **last** item of a page is older than 2023-02-01; the feeds run from the
  newest, and an old notice pinned above newer ones must not end a series.
- The instrument catalog has no time order. It is followed by cursor to its end and
  never stops on a launch time.
- 600 pages per series is a limit, not an end. A series that reaches it is
  `page_budget`: the fetch is not complete and the report refuses to run. Missing
  history must never be counted as missing events.
- No market-data endpoint is called.
- A seen-windows registry row (`event metadata, coverage`) and the request rules are on
  main before the first request.

## 7. Report and verdicts

Per bucket, source and window, the report gives:

- articles, events, clusters and announcements;
- the number of events whose bases came from the free-text title parser (below);
- distinct bases and ISO weeks;
- the counts for `not_timed`, `no_live_perp` and `identity_ambiguous`;
- the precision, edit, order and effective-time shares from section 4.

The report shows no market value. Because the fetch already reads the Bybit linear
USDT instrument catalog, the report also records `instrument_launches` per window
(distinct symbols, bases and ISO launch weeks from the deduplicated catalog) so the
post-listing drift screen (`I25`) is sized without a second fetch.

**Holdout privacy.** For the backward holdout the report gives only these aggregate
counts and shares: no list of tickers and no calendar distribution, such as events by
month or week. The holdout's event list is sealed outside Git, and only its hash goes
into the evidence manifest. The discovery year's event list (time, source, article id,
base, bucket) is published, since it is the laboratory.

**Denominators.** `timed` is a share of all in-window articles of the bucket. The
ambiguous share is `identity_ambiguous / (matched_live_perp + identity_ambiguous)`,
counted on `timed` events whose base has a Bybit USDT ticker candidate. Events with no
candidate at all are `no_live_perp` and are outside this denominator.

**Minimums.** Fixed now. They are not tuned after counting.

| Per bucket                                     | Discovery year | Backward holdout |
| ---------------------------------------------- | -------------: | ---------------: |
| Countable clusters                             |           ≥ 40 |             ≥ 30 |
| Distinct announcements                         |           ≥ 40 |             ≥ 30 |
| Distinct bases                                 |           ≥ 20 |             ≥ 15 |
| ISO weeks with at least one cluster            |            ≥ 8 |              ≥ 8 |
| Share of articles `timed`                      |          ≥ 90% |            ≥ 90% |
| Share `identity_ambiguous` (denominator above) |          ≤ 10% |            ≤ 10% |

**Source gating.** Precision and provenance are judged per source and window (section 4).
A bucket counts events only from its sources that are `timed` and pass provenance in
that window. A source that fails contributes its events as `not_timed` or
`provenance_failed`. The report refuses to give any verdict if a source has 50 or
more discovery-year articles and none of them is classified into a bucket: that
means a broken pattern or a wrong locale, not an absence of events. The fix may look
at discovery-year titles only.

**Verdict per bucket.** Applied in this order:

1. `not_reconstructible`: no source of the bucket is `timed` in the discovery year.
2. `provenance_unproven`: every `timed` source of the bucket fails provenance in the
   discovery year. A source fails if its canonical time differs from another
   machine-set time of the same article by more than 60 s in over 10% of articles
   (`edit_check_failed`), or if id order and time order disagree in over 5% of article
   pairs within any one catalog (`order_check_failed`). The report names which check
   failed. A failed order check is evidence that ids and times disagree, not proof of
   an edit. The source is still dropped from this study; whether a second check on a
   fixed sample of articles may readmit it is a separate amendment, written before
   that check is run.
3. `insufficient_events`: the discovery-year minimums fail.
4. `lab_only`: the discovery-year minimums pass but the holdout minimums fail. A
   laboratory read could then be confirmed only on forward data after v2's terminal
   state, which does not fit before 2027-02-11.
5. `feasible`: both columns pass.

**Study outcome.**

- `continue_to_lab_protocol` if at least one bucket is `feasible`;
- `stop_direction_a` otherwise.

A `lab_only` bucket goes forward only on the owner's separate decision.

**Title parsing.** A base is read in one of two ways, fixed in the code. `explicit`: a
ticker in parentheses or a `...USDT` pair. `caps`: every all-caps token after the
title's key phrase (`Will List`, `Will Delist`, `Monitoring Tag`), used where the title
has no explicit form. The free-text parser can take an ordinary all-caps word for a
ticker, and that word becomes an event if a live Bybit perp has the same name. Three
limits: it reads only the text after the key phrase; non-asset acronyms are dropped by
a fixed list; and the report counts its events per bucket and marks them in the
discovery-year list, so its exposure is visible before any verdict is trusted.

**Corrections.** One corrective recount is allowed after the verdict, for a parser or
identity defect only. It is disclosed, and it must not change the minimums.

## 8. Why these minimums

- **40 clusters.** A catalyst effect worth trading at retail cost must be large. The
  [cost and power note](cost-power-planning-v1.md) found nothing below 50 bps net
  testable on any flow seen so far.
  On 40 clusters, a cluster standard deviation of 300 bps gives a planning MDE of about
  2.49 × 300 / √40 ≈ 118 bps. That is the bar a laboratory read must clear. With fewer
  clusters, even a large effect is not testable.
- **30 in the holdout.** The exam is read once. Below 30 clusters it cannot confirm an
  effect of that size, so the bucket is `lab_only`.
- **The 300 bps standard deviation** is a planning assumption, not a measurement. The
  laboratory protocol replaces it with one fixed before its read.
- **Announcements.** The same floors as clusters. A bucket of bulk service articles
  (A4r, A4f, monitoring tags) can reach 40 clusters from a handful of moments; the
  planning MDE above assumes 40 independent observations, which only distinct
  announcements can supply. This is a floor on the material, not a claim that 40
  announcements are independent of the market day; the laboratory protocol fixes its
  own dependence model.
- **Bases and weeks.** These guard against one hot coin or one hot month.

## 9. What happens next

| Step                         | Gate                                                                                |
| ---------------------------- | ----------------------------------------------------------------------------------- |
| This contract                | colleague's design discussion; fixes folded in                                      |
| Registry row + code PR       | one colleague review; merged on green                                               |
| Fetch                        | owner's go; clean detached worktree at the merge commit                             |
| Count report + verdict       | owner's go for the read; result record PR                                           |
| Laboratory protocol (if any) | new HYP id; horizons, delay grid, costs, exits and threshold fixed before any price |

The laboratory protocol is out of scope here. Points for it, recorded now so they are
not chosen after seeing prices:

- **Bybit's trade tape is a ceiling.** It shows trades, not the spread or depth at
  the announcement second. If the move left after a realistic delay, minus 30 bps,
  is not positive even on trades, the bucket stops. If it is positive, real spread and
  depth must still be measured live before any order.
- **The delay grid comes from delivery, not from wishes.** Polling plus CDN lag from
  a single server is likely seconds to tens of seconds, not milliseconds. My position
  is a grid of 5 s, 30 s and 60 s after `announced_at`, with 1-minute bars to 4 h as
  the horizon class. The colleague's draft had 1 to 5 s on the trade tape. To decide
  in the protocol discussion.
- **Execution is on Bybit only.** Binance, Upbit and Coinbase are event sources.

Proposed calendar:

- 11-16 to 11-22: this contract, code, fetch and count report;
- 11-23 to 12-06: laboratory protocol and read, if any bucket is `feasible`;
- 11-30: HYP-012 v2 checkpoint (unchanged);
- December: the holdout exam, if the laboratory passes;
- 2027-02-11: the program stop rule (unchanged).

## 10. Not in this study

- **A live delivery collector**, recording `published_at` against `received_at` and
  HTTP `Date`/`Age` headers, with no market data. It would measure the delivery lag
  that history cannot. It is a separate contract, written only after this count
  report and only if a bucket is `feasible`, or `lab_only` on the owner's decision. It
  needs a prod or host change on the owner's go. Before v2's terminal state it may run
  only with no market data, and what is read is only the lag distribution
  (`received_at - published_at`, `Age`) without tickers; anything with tickers stays
  sealed until v2 is terminal.
- Any price, return, spread, depth or funding value.
- External Telegram channels or third-party news feeds. They are never a data or label
  source.
- Onchain data (direction B) and new infrastructure (direction D), deferred until
  direction A shows something.
- HYP-031 at the seconds horizon, which stays closed.

## 11. Design discussion record, 2026-11-15

The draft (`d3a0c47`) went to the colleague with six questions. Their answers, and
what changed:

1. **Buckets.** Agreed: Coinbase stays so a negative is recorded; no OKX or Bithumb in
   v1. They asked for a priority rule when one article has both spot and futures, and
   for A4 sub-counts. Done: A1 wins over A2 (section 3). A4 is split further than
   asked, into three buckets with their own verdicts, not only sub-counts.
2. **Holdout at coverage level.** Agreed that counting does not spend it, provided the
   report holds no ticker lists or calendar distribution for the holdout. Done
   (section 7, holdout privacy).
3. **Pagination through the blind window.** Accepted as better than losing A4, with
   two required guards: the same cut for `instruments-info`, and no raw dumps or
   manual inspection of fresh pages. Done, and extended: no response body is ever
   persisted, and a `deliveryTime` from 2025-10-01 is not recorded (section 6).
4. **Minimums.** Kept unchanged. The ambiguous share's denominator is now explicit
   (section 7).
5. **Live delivery collector.** Agreed as a separate contract after the count report,
   with the blind-window conditions now in section 10.
6. **Canonical time.** Agreed: the earliest machine-set publication time. No source documents it, so the order check is the evidence instead (sections 4 and 6). My own
   addition: only machine-set times gate `provenance_unproven`, because an
   author-filled time such as Bybit's `dateTimestamp` would otherwise fail a sound
   source.

The delay grid for a later laboratory read (1 to 5 s on the trade tape against 5, 30
and 60 s) stays for the protocol discussion.

**Review of the code PR, 2026-11-15.** Three findings, all fixed before any fetch or
count:

1. **Bulk articles counted as independent clusters.** Section 3 said the cluster rule
   kept the bases of one article from counting as independent; it did not. Added the
   announcement count and its floors (sections 3, 7 and 8). The reviewer's example of
   two or three articles passing was too strong, since 8 ISO weeks were already
   required, but eight bulk articles over eight weeks did pass.
2. **Order check pooled across catalogs.** It compared ids of all three Binance
   catalogs with each other. Now per catalog (section 4). The reviewer proposed
   lowering it to a diagnostic; it stays a gate, because for a source with one machine
   time it is the only evidence on edits.
3. **Free-text title parser.** For monitoring tags it read the whole title. Now it
   reads only after the key phrase, drops a fixed list of acronyms and is counted in
   the report (section 7). A ticker that is also a word (`ID`, `AI`) is not put on the
   stop list: it is a real listed asset and must stay readable.

**Second review of the code PR, 2026-11-15 (participant C, at `17e8f3b`).** Three
findings, all confirmed in the code and fixed before any fetch:

1. **An old instrument ended the catalog.** The time boundary of the announcement
   feeds was also applied to instruments, so one perp launched before 2023-02-01 on
   the first page stopped the cursor. Later instruments would have been counted as
   `no_live_perp`. The catalog now runs to the end of its cursor (section 6).
2. **A page budget was reported as a complete fetch.** Reaching 600 pages set the same
   terminal state as the end of history. It is now its own stop reason, the fetch is
   not complete and the report refuses (section 6).
3. **The promised resume after a schema fix did not exist.** The run binding forbids
   another revision, and a stopped run stays stopped. The code is right and the
   contract was wrong; it now says a corrected revision starts a new run (section 6).

One more change by the author, found while fixing the first: the time boundary is read
from a page's last item only, so an old notice pinned at the top of a feed cannot end
it early. It can only make the fetch read more pages, never fewer.

**Third review pass before first fetch, 2026-11-15.** Three pre-fetch items closed:

1. **Cumulative budget across a stopped run and its successor.** Passing `--prior-root`
   to `fetch` carries the stopped run's attempts and bytes into the new run binding,
   deducts them from the study's 3,000-request and 64 MiB caps, and records the prior
   run in `provenance.prior_runs` (section 6).
2. **Catalog coverage limitation.** Section 5 states explicitly that the trade-archive
   file listing is not an input to this module; the counts reflect Bybit's
   `instruments-info` catalog across `Trading`, `Delivering` and `Closed`.
3. **New-perp launch counts (`instrument_launches`).** The report records aggregate
   launch counts (symbols, bases and ISO weeks per window) from the deduplicated
   instrument catalog (section 7).

**First live fetch stop, 2026-11-16.** The fetch of revision `3c974cc` stopped on page
24 of `bybit_ann:delistings` with `schema_mismatch`, after 23 pages and 24 requests. The
receipt's key tree equalled page 1's, so it could not name the cause; no body is stored.
The corrected revision adds the `failure` record (section 6) and the untimed-article rule
above. Whether page 24 holds articles with a missing or zero `publishTime` is a
hypothesis until the rerun; a different cause stops the rerun again, now with a reason.
The rerun passes `--prior-root`, so the 24 requests count against the same budget. The
untimed rule and the 10% limit amend sections 4 and 6 before any count is read.

**Changes made while writing the code.** These come after the design discussion and
are for the colleague's review of the code PR:

- **Undocumented endpoints.** Binance's and Upbit's announcement lists have no public
  documentation. A key-only schema probe on the first page of every series stands in
  for it, and any mismatch stops the fetch closed (section 6).
- **Edits.** No source documents whether its publication time is rewritten on edit,
  Bybit included, so the order check is the evidence (section 4).
- **Precision per source.** A source with more than 20% of its articles on a whole
  minute is treated as minute-rounded (section 4).
- **Unobservable checks.** A check a source cannot support is reported and does not
  gate (section 4).
- **Identity.** Matching is at ticker level after multiplier stripping. The counts
  are an upper bound, and a laboratory protocol must verify identity per event
  (section 5).
- **Coinbase.** It is not requested and is `not_reconstructible` at source level
  (section 6).
- **No verdict on an unclassified source.** The report refuses any verdict if a
  source has 50 or more discovery-year articles and none classified (section 7).
