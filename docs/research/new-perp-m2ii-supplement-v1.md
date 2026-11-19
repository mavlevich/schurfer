# I25 M2(ii) archive metadata supplement v2

Proposed contract only: no request is authorized. Execution requires review,
merged clean code and the owner's go for this contract's exact SHA-256 and caps.
Unused Go 2 budget is not reused.

## Frozen source

The source run on `8748d9ef664b4b0c65f885420692166dd8ad7a54` has 228 primary
episodes over 48 ISO weeks. Its only unassessed check is M2(ii). The root reducer
found no dates; raw HTML was not retained, so the server formatting cause is unknown.

| Source binding           | SHA-256                                                            |
| ------------------------ | ------------------------------------------------------------------ |
| Report                   | `400f635ea29ac52dc5999caa866b152fa14cea1b82381ce91270012821a853ca` |
| Manifest                 | `759305ceaf5730ff0b7f23963a2c17292590ecb590d0336376cc139a0b5ca76d` |
| Canonical input hash map | `cc20f7d5b06208a18772d3df4f0a3427e1975750aea6f94a17845e5f627b4c5e` |

All source fingerprints and code publication are verified before HTTP and before
publishing a result, including after a failed scan. The source is never written
or resumed. The comparison base is its frozen 263 in-window catalog symbols.
There is no fresh C1 subtraction, which could double-count a newly absent catalog
member or hide an older archive behind a post-cut reused ticker.

## Requests and ceilings

1. GET `https://public.bybit.com/trading/`, response cap 4 MiB.
2. In RAM subtract the frozen 263 symbols from observed USDT directories.
   Check the whole set against 1,200 directories before D1; no prefix is taken.
   Every catalog archive directory observed in the source must still be present.
3. GET `https://public.bybit.com/trading/{SYMBOL}/` for each remaining directory,
   response cap 1 MiB. Only HTML file names are parsed.

**Hard caps: 1,500 attempts and 134,217,728 bytes (128 MiB)**, retries included,
with the existing 64 KiB last-block reserve. There are at most 1,201 first
attempts, leaving 299 retries at the largest plan. At most three sends per
logical request are allowed. Completion under the caps is not guaranteed.

The earlier 305-attempt/35-MiB estimate is replaced explicitly. Avoiding fresh
catalog subtraction requires scanning all unknown archive directories, including
those that might otherwise be hidden by ticker reuse. The prior v1 scan stopped before completing this directory cohort; its
operational attempts do not establish a full unknown-directory count. The original 6.29 GiB run is preserved.

The existing fetcher supplies pacing, Retry-After, durable stops, pre-send
reservations and byte accounting, with class specs bound to this new contract.
Redirects and compressed transfer encoding are refused. An oversized declared
length is not read; unknown lengths are charged through the stopping block.
Limits, incomplete pages, unknown formats, HTTP 403 and unavailable listings
produce an incomplete result. No CSV, HEAD, bar, mark, funding or order-book
request is included. No other venue or market outcome is read.

## Admission and privacy

Unknown names and URLs stay in RAM until admitted. Each request has a random
256-bit journal key, not an unsalted hash of a guessable URL. Identity metadata
and routes outside the fixed archive endpoints are rejected. There is no persisted
candidate-name plan. Attempt and receipt identity fields are empty. HTTP logging
is disabled during the run; fixed errors remain journalled. Key-only schema probes
and raw-body hashes are also removed: keys or small-page fingerprints can identify
an unknown symbol.

The parser requires complete HTML and recognized link grammar. Parent links alone,
malformed dates or unsupported anchors do not establish a post-cut folder and
leave the scan incomplete. Valid dated USDT CSV-gzip filenames may have a prefix
different from the directory. This is format recognition only, never evidence of
a rename or common instrument identity.

- Valid listings with files only from `2025-10-01` onward retain no name, date,
  fact or post-cut classification count. Only operational attempts and bytes change.
- If **any** valid pre-cut filename has a different prefix, no identity or date
  is admitted from that directory, including otherwise matching filenames. Only
  an opaque `identity_status: unverified_pre_cut_filename` issue is snapshotted.
  The scan continues, but M2(ii) remains unassessed and cannot produce a positive
  verdict. This also applies to pre-window foreign-prefix files. A foreign prefix
  is never treated as a second instrument or silently discarded.
- A directory whose pre-cut filenames all match the directory admits its symbol. Private service material outside
  Git keeps only the name, first archived day, last pre-cut day and pre-cut file count.
  Later dates and counts vanish. Pre-window first days do not count as window launches.
- Only first archived days in `[2024-10-01, 2025-10-01)` contribute to M2(ii).
  This is an archive-cohort definition, not proof of actual instrument trading-open.

Opaque operational accounting for undated directories is explicitly allowed by
this supplement; it replaces the old root-date prerequisite for this scan only.
Under non-binding caps, adding a valid post-cut folder leaves scientific M2
aggregates unchanged; operational requests and bytes may increase.

## Completion and failure

Use a new sibling root. Existing roots, even incomplete ones, are refused before
HTTP. There is no resume: unknown mappings are ephemeral. A crash cannot be repaired
by clearing a stop or choosing another directory under the old authorization.
A fresh run needs a new owner go that acknowledges already spent attempts/bytes.

Every admitted pre-cut listing is snapshotted privately. Only aggregate reports
are eligible for Git. An incomplete scan or budget stop preserves
`stage1_incomplete`. Input/code mutation is an integrity error and produces no
completion marker. An unknown listing is never silently counted as zero.

For a complete scan, let `a` be the observed first-archive-day symbols in the
window outside the frozen 263. The share is `a/(263+a)`, threshold 5%: 13 passes,
14 fails. Above the threshold the companion reports `incomplete_coverage`;
otherwise it may report `stage1_pass_partial`, **only if no pre-cut filename
identity issues remain**. With such issues, the exact share and inherited
`archive_only_unlisted` / `candidates_listed` totals are null. The report retains
the known matching-prefix in-window count and the number of opaque unresolved
directories. It reports `stage1_incomplete`, with
`m2_archive_filename_identity_unverified` in `not_assessed`. A known matching-prefix
count of 14 or more still reports `incomplete_coverage`; unresolved identity cannot
mask a known threshold breach. No upper-bound shortcut is used to grant a pass.

`complete` and `supplement_done.json` describe HTTP scan completion only.
`identity_check_complete` is false if any pre-cut prefix issue exists. The CLI
returns nonzero while `not_assessed` is nonempty, even after a complete scan.
The original report remains immutable.

M2 still does not prove market-wide completeness, unseen/retired archives, true
launch times or historical identity. M4 stays coarse, M5 minute continuity remains
at Stage 4 and M6 stays static. No price, funding-rate or profitability calculation
is authorized by this contract.

## Invocation

`new_perp_m2ii plan` prints the exact contract. Fetch refuses a different approval,
a different checked-in contract or a dirty/uncommitted publication.

```bash
uv run python -m schurfer_analytics.new_perp_m2ii plan
# Only after review, merge and the owner's separate go:
uv run python -m schurfer_analytics.new_perp_m2ii fetch \
  --source /absolute/sealed/i25-stage1-v3-8748d9e \
  --root /absolute/new/m2ii-run \
  --approved-sha256 <supplement request_list_sha256>
```

A SOCKS environment needs httpx's optional SOCKS transport support. Keep the
configured proxy; a setup error is not permission to change network routing.

## Actual prior exposure and proposed v2

The v1 contract (`4ca2124139eb28e4c38dc00e40b4a245eb7a5c357c5729028842f61221854a7c`)
was approved and run on clean merged `84f2ba5`. It stopped after 81 attempts and
4,421,748 bytes on `unsupported_file_anchor`; no completed M2(ii) result exists.
A separately authorized category-only probe made two metadata GETs and read
174,885 bytes. It observed `foreign_prefix_dated_csv_gz` at the current ordinal
80, without keeping the names, dates or raw HTML. The root could have changed;
this does not prove continuity with the failed directory or explain its identity.
Both roots remain sealed. Thus the two activities spent 83 attempts and
4,596,633 bytes separately from the original completed Stage 1 run.

V2 changes filename classification and report semantics, so the old approval
hash is invalid. V2 remains a proposal requiring review, merge and a new owner go.
No v2 archive request was made during implementation. The same 1,500-attempt /
128-MiB ceiling would be **new** allowance, not a refund of spent attempts.
A fresh sibling root is required; neither sealed root may be resumed.
