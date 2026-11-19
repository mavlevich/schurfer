# I25 targeted filename identity diagnostic v1

Proposed code and contract only. No HTTP request is authorized by publication,
review or the previous v2 approval. Fetch requires reviewed, merged clean code,
the exact new approval hash and a fresh sibling output root.

## Question and frozen source

The completed M2(ii) v2 scan on `7e958b8` found one matching-prefix directory with
its first archived day in the discovery window and two unresolved pre-cut filename
identity cases. Stage 1 remains incomplete. The report is bound by SHA-256
`0590a619d8bc5ddac11e928a958038f360ad4616d3685b11c53dc11be31a1f23` and its private
captured-input map by `751a2f1bb1af744d26df25e68b928e729da93e122477abba0513b5ca9da408fe`.
The original completed Stage 1 inputs and all v2 captured inputs are verified
before HTTP and again before the final diagnostic report, including after failure.
Neither sealed source is changed or resumed.

The two opaque issues correspond to D1 positions 80 and 245. Their directory
identities were intentionally not retained. A new root may differ. The diagnostic
therefore asks only what pre-cut filename evidence exists at those _current_
positions; it cannot establish that either is the original directory.

## Requests and limits

1. GET `https://public.bybit.com/trading/`, maximum response 4 MiB.
2. In RAM, sort its USDT directories after subtracting the frozen 263 catalog
   symbols. Require all previously known catalog directories still present,
   at least 245 remaining positions and no more than 1,200. Do not search for
   replacements if these guards fail.
3. GET the directory HTML at each current position 80 and 245, maximum 1 MiB
   each. Require valid pre-cut foreign-prefix filenames; otherwise stop with
   `target_drift_unproven` or another fixed schema reason. No fallback scan.

There are three logical GETs. Hard ceilings are **9 HTTP attempts and 20 MiB
(20,971,520 bytes)** including retries, charged stopping blocks and the existing
64 KiB pre-send read reserve. At most three sends per logical request. Even three
maximum-size responses for each request plus their reserves fit under 20 MiB.
Pacing, Retry-After, durable stops, non-redirecting transport and oversized-response
handling come from the already tested fetcher. Completion is not guaranteed.
No CSV, HEAD, bar, mark, funding, order-book, announcement or AWS request is included.

## Private evidence retention

This proposal expands retention from a fixed opaque issue to **private pre-cut
filename evidence**, not accepted instrument identity. It needs explicit review.

- Parse the same complete HTML and validated filename/date grammar as v2.
- Keep only prefixes actually evidenced by valid filenames dated before
  `2025-10-01`, with first/last pre-cut day and unique pre-cut file count.
  Noncanonical prefixes remain strings, never inferred exchange symbols.
- Keep the directory name only if that exact name also has a valid matching
  pre-cut filename. Otherwise only the opaque request token identifies the folder.
- Discard all post-cut prefixes, dates and counts, raw HTML, full filenames,
  URLs, schema probes and raw-body fingerprints. HTTP logging is disabled.
- Private prefix facts stay outside Git. Journals keep random opaque keys and
  empty identity metadata; the public diagnostic report contains aggregates only.
- If a current position has no valid pre-cut foreign-prefix file, stop without
  saving that target's prefix facts. A later drift leaves the whole diagnostic
  incomplete, even if one earlier observation was saved privately.

Filename evidence does not prove the asset, contract multiplier, rename, launch
or first trade. Pre-window filenames remain coverage metadata, not market values.

## Result and next decision

The only completed result is `diagnostic_complete_unlinked`: two current
observations were captured under the limits, with `identity_continuity=not_proven`.
It does not update M2, its numerator/denominator, the cohort, any threshold or
any old report. Stage 1 remains `stage1_incomplete`. A format match or position
match cannot grant a pass. Any later identity decision or new full scan needs
its own reviewed amendment and owner approval.

The original Stage 1 had 1,726 attempts; the stopped v1/probe spent 83 more;
v2 spent 889. All remain spent. This proposal would add up to nine attempts,
not reuse an old unused allowance. No actual diagnostic attempt has been made.

The entry point offers `plan` and `fetch`. Fetch refuses a stale approval hash,
a dirty publication, code not merged into the local `origin/main` reference,
changed source evidence or an existing output root. The invoking session must
refresh/verify the remote main reference before executing an approved fetch.
