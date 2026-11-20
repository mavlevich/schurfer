# I25 M2(ii) v3: observed pre-cut filename cohort

Proposed contract and code only. No archive request is authorized now. A future
fetch requires review, clean merged code, a new owner go for the exact contract
hash/caps and a fresh sibling root. No old allowance is reused.

## Definition and limits of the claim

The proposed M2(ii) unit is `observed_pre_cut_file_prefix_cohort`, not verified
instrument identity. The observed archive can contain a filename prefix that
differs from its directory label. The observations do not establish when either
label was assigned, a rename, a multiplier or the actual trading-open date.

A fresh full HTML snapshot may assess the first archived day of an unambiguous
canonical filename series outside the frozen catalog. Canonical means the lexical
shape `^[A-Z0-9]+USDT$` only. It does not prove that the series is the same asset,
a linear perpetual, or every historical launch of that label. Coverage remains
partial and limited to the archive currently observed.

The unchanged reference base is the frozen 263 catalog symbols. No fresh catalog
subtraction or reinterpretation of the original 228 primary episodes is allowed.
M3, M4, M5 and M6 retain their original partial checks and limitations.
The cohort is conditional on directories outside the frozen catalog. Known
directory contents are not rescanned, so this is not a complete census of all
filename prefixes in the whole archive either.

## Sources and immutable history

All original Stage 1 inputs are verified by the existing source loader. The
sealed v2 input map and the completed targeted diagnostic are independently
bound and verified too. Their files are never used as cached v3 listings or as
proof of continuity with current directories.

| Sealed evidence               | SHA-256                                                            |
| ----------------------------- | ------------------------------------------------------------------ |
| Original Stage 1 report       | `400f635ea29ac52dc5999caa866b152fa14cea1b82381ce91270012821a853ca` |
| Original canonical input map  | `cc20f7d5b06208a18772d3df4f0a3427e1975750aea6f94a17845e5f627b4c5e` |
| M2(ii) v2 report              | `0590a619d8bc5ddac11e928a958038f360ad4616d3685b11c53dc11be31a1f23` |
| M2(ii) v2 input map           | `751a2f1bb1af744d26df25e68b928e729da93e122477abba0513b5ca9da408fe` |
| Targeted diagnostic report    | `4449921d31dc9efdb61925d8f83b33c5ca0a39885f80de910cd5344b96017b49` |
| Targeted diagnostic input map | `9a47070688e546eaf44715c37ed48cf6e9ac19407e0da7a9d37cecddd1ee8492` |

Validation occurs before HTTP and again before admission/publication, even on a
failed scan. V2 remains `stage1_incomplete`; the diagnostic remains
`diagnostic_complete_unlinked` with `identity_continuity=not_proven`. Only a new
v3 companion may receive the proposed cohort-based result. No report is rewritten.

## Requests and ceilings

1. GET `https://public.bybit.com/trading/`, response cap 4 MiB.
2. In RAM subtract the frozen catalog from its USDT-shaped directory names.
   Require every catalog archive directory observed in the original source to
   remain present. Check the whole unknown-directory plan before any D1 request.
3. GET every unknown directory's HTML, response cap 1 MiB per directory.
   No prefix of the directory plan is selected, no CSV body is requested.

Hard caps: **1,200 unknown directories, 1,500 HTTP attempts and 128 MiB
(134,217,728 bytes)**. Retries and the existing 64 KiB stopping-block reserve are
included. At most three sends per logical request. The largest plan needs 1,201
first attempts, leaving 299 retry attempts; completion under the caps is not
promised. The previous full HTML scan used 889 attempts and 42,631,655 bytes,
which is an estimate for planning only.

A separate **16,384 pre-cut prefix-slot** ceiling bounds retained RAM metadata.
It counts distinct pre-cut prefixes per directory, summed across directories;
duplicate file links/days within a series do not add slots. Exceeding it stops
the entire snapshot without truncation or admission. This is a safety cap, not
a smaller selected cohort. Post-cut-only prefixes do not consume these slots.

The tested fetcher enforces pacing, Retry-After, durable stops, pre-send reserves,
byte accounting, no redirects and no compressed transfer encoding. Incomplete
HTML, unsupported links/dates, unavailable listings and any budget failure leave
Stage 1 incomplete. No HEAD, CSV, price, funding, bar, mark, book, announcement or
AWS request is included. The original 6.29 GiB fetch is not repeated.

## Admission and collisions

Only valid filenames dated **before `2025-10-01`** contribute to a directory's
prefix set. Valid later filenames disappear in RAM without an identity, date,
classification count or contribution to collisions.

A series is admissible only after the entire scan completes and all conditions
are met:

- Its directory has exactly one pre-cut prefix P, lexically canonical by
  `s.SYMBOL.fullmatch(P)`.
- P is absent from the frozen 263 symbols.
- P appears in exactly one unknown directory of this snapshot. No normalization,
  cross-directory merge or inferred alias/rename is allowed.

Two or more pre-cut prefixes, or any noncanonical pre-cut prefix, create only
`unverified_pre_cut_filename`. Any canonical prefix shared across unknown
directories or colliding with the frozen catalog creates only
`prefix_collision_unverified`, for **all** owners. Canonical prefixes inside
mixed directories also participate in collision detection. Both types may apply
to one directory; their directory counters may overlap and are not summed as a
unique total. Pre-window collisions still block a positive result.

No series is admitted during HTTP. A collision in a late directory invalidates
the earlier candidate before either is persisted. An incomplete scan admits no
provisional candidate: an unseen directory might collide with it. Known opaque
issues may still be retained as fixed statuses, never with their names.

For each globally admitted series P, private `admitted/{opaque_key}.json` keeps
`file_prefix`, `first_day`, `last_day_before_cut` and unique pre-cut day count.
`directory_name` is present **only if it exactly equals P**. Different directory
names are never retained. Blocked prefix names are not persisted. Journals use
random opaque keys, empty symbol/meta fields, no raw-body hash or schema probe;
HTTP logging is disabled. Private facts remain outside Git.

## New report and verdict

Let a be the admitted series with first_day in `[2024-10-01, 2025-10-01)`.
A series whose first_day is earlier remains `archive_only_pre_window`, even if
its later files continue inside the discovery window.

The proposed threshold is unchanged: `a / (263 + a) <= 0.05`, so 13 passes and
14 fails. The exact share is null while any pre-cut format/identity or collision
blocker remains. Blockers add explicit `not_assessed` reasons and prevent a
positive result. A known numerator of 14 or more from globally admitted series
still produces `incomplete_coverage`; uncertainty cannot mask that known breach.

A complete, unblocked snapshot may produce `stage1_pass_partial` only if all
other inherited conditions remain satisfied. M2 explicitly records its unit,
`archive_only_proven=false`, `archive_only_status=observed_archive_file_prefix_partial`
and the coverage/identity limitations in `partial_checks`. It does not claim
historical instrument identity, no renames or all real launches. Primary sample
selection and M3/M4/M5/M6 are unchanged.

`supplement.complete` / `supplement_done.json` indicate HTTP scan completion.
`file_prefix_check_complete` additionally requires no unverified/collision issue.
The CLI returns nonzero for any remaining unassessed check. There is no resume:
a stopped root remains sealed and another run needs a fresh owner go/root.

The five recorded source/diagnostic roots have already spent 2,701 attempts and
6,812,186,591 bytes, separately from other historical project access. Those costs
remain spent. V3 would add a new capped run; no v3 request has been made.
