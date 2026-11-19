# I25 Stage 1b: executable metadata proposal v1

**Not authorized to fetch.** This is the code/contract proposal following the
architectural review, not a completed holdout read. Body/coverage fetching and
the final M1-M6 verdict are not implemented by this proposal. There is no price,
funding, candle, book, AWS, deployment or trading action here.

## Prerequisite and frozen scope

The [architectural draft](new-perp-stage1b-contract-draft-v1.md) was approved with
four mandatory clarifications; all four are reflected below. The prerequisite
discovery v3 report is `stage1_pass_partial`, SHA-256
`38b569aa5dc713179ded3db70f7cdf788cea560db7c0c99c65d9332a879070b8`, with evidence-map
SHA-256 `532d101483c2691c422e1ddce2187423dfce5d460c4de1b719d626b66645ad03`.
The CLI verifies this source and the three sealed prerequisite roots before and
after HTTP. None can be an ancestor of the new output root.

The only scientific window is `[2023-02-01T00:00:00Z, 2024-09-16T00:00:00Z)`.
Window configuration is an explicit frozen value, not changed discovery globals.
The original discovery runner and its executable contract are unchanged.

`X` denotes the end of the exit bar `[X - 1 minute, X)`: only `X > cut` censors
the path. `X == cut` is allowed. Launch/trade/bar/funding start times remain below
cut; realized funding will require `E < settlement < X`. Exact T0, E/X eligibility
and the 100-episode/26-week floor await the separate body contract. Metadata cannot
prove the exact clock from a daily filename alone.

## Requests and hard ceilings

| Class | Method and endpoint                                                                                   | Logical maximum | Per-response cap |                       Class byte cap |
| ----- | ----------------------------------------------------------------------------------------------------- | --------------: | ---------------: | -----------------------------------: |
| C1    | GET `api.bybit.com/v5/market/instruments-info`, linear, Trading/Delivering/Closed, limit=1000, cursor |               9 |            3 MiB |                               28 MiB |
| C3    | GET `public.bybit.com/trading/`                                                                       |               1 |            4 MiB |                                9 MiB |
| D1    | GET `public.bybit.com/trading/{directory}/`, one unique union of catalog and unknown directories      |            1500 |            1 MiB |                              384 MiB |
| D2h   | HEAD `public.bybit.com/trading/{symbol}/{symbol}{day}.csv.gz`, in-window first file only              |             300 |     0 body bytes | shared stage cap, integer sentinel 0 |

At most 300 in-window catalog rows and 1200 outside-cohort directories. The first
attempt ceiling is `9 + 1 + 1500 + 300 = 1810`. All first sends and retries consume
the same **2400-attempt** ceiling; at most three sends per opaque request key.
Total response-byte ceiling: **469,762,048 bytes (448 MiB)**. A GET starts only if
its whole response cap plus the 65,536-byte stopping-block reserve fits both its
class and stage. HEAD reads no body. C1's 28 MiB includes reserve for the ninth
full 3 MiB response; 27 MiB would stop before that send. Class caps overlap the
stage cap and do not guarantee all possible full-response retries can finish.

Pacing and bounded Retry-After use the existing durable fetcher. No redirects or
compressed response encoding are followed. Actual received stopping-block bytes
are charged. Declared length over the response cap is rejected before body read;
unknown length is bounded by the response cap plus reserved last block. An
unreceipted attempt consumes its full reservation. A stop seals the root; the
Stage 1b CLI cannot resume it or reuse prior budgets.

No exact private queue can be known before C1/C3 complete. This contract freezes
the deterministic planner and endpoints/caps; the emitted sealed manifest and its
hash identify the admitted cohort and first-file preflight, not a replay list of
discarded target names or cursor tokens. It never selects a prefix of an oversized
queue. More than three pages per catalog status, a repeated cursor, conflicting
duplicate row, or a full page with no cursor leaves pagination unproven and stops.

## What is read in RAM and what survives

C1 reads launchTime first; post-cut rows are dropped before their other scientific
fields are interpreted. Pre-window symbol/base/delivery information lives in RAM
only to compute relist and multiplier ambiguity. In-window rows have fixed static
numeric fields (min-notional, quantity step/minimum, max leverage), funding-interval
snapshot and presence of a pre-listing field. Catalog values are current snapshots,
not information proved known at historical entry. Unknown numeric strings stop
with a fixed schema reason. Price filters, rates and all other values are discarded.

Every unique in-window C1 row remains in N, including HTTP 404 `no_archive`, mixed
filename evidence, or an unconfirmed name. A private opaque slot preserves its
launch timestamp and predicates; the string symbol and static row are retained
only after clean matching in-window filename evidence. Prior labels and bases are
not persisted. No missing row is silently removed from N or reason/M3 counters.

Both catalog and unknown D1 folders use the same strict HTML parser: complete
UTF-8 HTML, full matching relative filename grammar and valid ISO dates. Any
foreign pre-cut prefix in a catalog folder marks its identity unverified; no HEAD
is planned from that folder. All its canonical pre-cut prefixes enter the frozen
M2(ii) collision set, even if foreign or mixed. Unsupported links/date/schema
stop the snapshot rather than being silently ignored.

Catalog D1 reducers compute `has_pre_window_file`, `first_file_within_1d_of_launch`,
`first_file_class_from_day` and `ticker_reuse_suspected` in RAM before discarding
earlier dates. Ticker reuse uses the existing 30-day predicates. An earliest file
before 2023-02-01 has `t0_pre_window_unreadable`; no exact T0 is invented and no
HEAD/CSV GET for that file is possible. A later lookback exclusion does not erase
that M1 uncertainty. Matching filenames inside the holdout can still establish
the label without storing the earlier date.

Unknown-directory admission uses one canonical pre-cut filename prefix per folder,
with no cross-folder merge or normalization. All canonical prefixes, including
those in mixed folders, participate in global collisions. Admission waits for the
whole scan, HEAD phase and final source/code validation. Pre-window dates survive
only as `has_pre_window_file`; a label requires its own in-window file. A solely
pre-window series persists only that boolean under an opaque key. Later filenames
produce no retained names, dates, scientific counts or collision contribution.
The RAM prefix-slot cap is 16,384 across catalog and unknown folders together.

## Reports and sealed artifacts

The fresh root is mode 0700. Catalog slots, accepted facts and manifest are private
and outside Git. HTTP attempts and receipts use random opaque keys, empty symbol/
meta, no URLs or raw/schema/body hashes/retMsg. HTTP logging is disabled. A report
or review handoff exports only whole-window aggregate counts, fixed reasons, byte/
attempt use and provenance hashes. Operational request counts vary with responses;
they are not a count of post-cut launches. Private input-map paths are not exported.

`manifest.json` and `evidence-hashes.json` bind the private catalog/admission facts
and journal. `metadata-report.json` carries their hashes, the source fingerprint,
clean code/publication binding, aggregate states and known first-file sizes.
`metadata_done.json` means the scan completed; it does not authorize the next phase.
Source/code validation failure removes admission and blocks any positive result.

Successful metadata has outcome `metadata_complete_coverage_pending`, never
`stage1b_pass_partial`. An unresolved M2(ii) folder/collision gives
`metadata_complete_unassessed`; an incomplete HTTP scan gives `metadata_incomplete`.
N=0 is `insufficient_events`. Known M2(i) exact-prefix missing coverage or M2(ii)
archive-only shares above 5% give `incomplete_coverage`, even with other uncertainty.
Catalog folders with mixed/foreign prefixes are unproven for M2(i), not silently
cleaned or labelled known missing. M2(ii)'s share is null while blockers remain;
its threshold uses this holdout's N, not discovery's 263 or its 13/14 shortcut.

No M1/M3/M4/M5/M6 pass is emitted here. Exact clock/entry/funding/hourly checks need
their body contract; historical identity, minute continuity and execution retain
the existing explicit limitations. No holdout fact may tune Stage 3 parameters.

## Hash and future execution gate

Canonical request-list SHA-256:
`e4513b79bfc4e2f8b31d1e691a3879565ad3f0e7372ab20ffd9793c69f772c9f`.
The [checked-in JSON](evidence/new-perp-stage1b-metadata-v1/contract.json) must equal
`build_contract()` exactly. The `plan` command is offline. The only network CLI is
`fetch-metadata`; it rejects a stale approval, dirty/unmerged code, changed sources,
or an existing output root. No D2/body/funding/bar command exists in this proposal.

After code/contract review, green CI and merge, a future invocation would be:

```bash
PYTHONPATH=apps/analytics python -m schurfer_analytics.new_perp_stage1b fetch-metadata \
  --base /ABS/SEALED/i25-stage1-v3-8748d9e \
  --v2 /ABS/SEALED/i25-m2ii-v2-7e958b8 \
  --diagnostic /ABS/SEALED/i25-m2ii-identity-probe-v1-3cf37ca \
  --discovery /ABS/SEALED/i25-m2ii-v3-49edd5d \
  --root /ABS/NEW-PRIVATE-ROOT \
  --approved-sha256 e4513b79bfc4e2f8b31d1e691a3879565ad3f0e7372ab20ffd9793c69f772c9f
```

This is a template, not permission to run it. The owner must approve this exact
hash and the new 2400-attempt/448-MiB budget first. After metadata, a separately
reviewed initial body queue, bounded rules for requests depending on the as-yet
unknown T0, and a size-based budget need another owner go. The holdout exam,
outcome read, AWS, paper/live path and production deployment remain excluded.
