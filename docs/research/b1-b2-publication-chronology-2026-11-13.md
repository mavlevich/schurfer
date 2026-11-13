# B1/B2 publication chronology, 2026-11-13

The specific four-hour supplemental-fetch registration and B2 registrations were
committed locally before their requests, but were not published in main before
those requests. This PR publishes the actual history retrospectively. It is not
a claim of main-before-fetch compliance and does not backdate a registration.

| Own operation                            | Local registration commit, UTC | Invocation / first request, UTC       |
| ---------------------------------------- | ------------------------------ | ------------------------------------- |
| B1 fixed four-hour supplement, `1cc0949` | 2026-11-13 00:17:07            | invocation 2026-11-13 00:17:39.541556 |
| B2 metadata HEAD, `e3e33c6`              | 2026-11-13 07:58:30            | first HEAD 2026-11-13 07:58:50.959258 |
| B2 native source pilot, `c1c9fae`        | 2026-11-13 12:24:03            | first GET 2026-11-13 12:24:26.312257  |

[Machine chronology](evidence/b2-archive-scope-2026-11-13/publication-chronology.json)
binds these times to retained invocation/attempt artifacts. Prior source revisions,
failed reads, original hashes and local registrations remain preserved. The B2
day-boundary correction was likewise committed before its second inspection.

Main already records the other agent's all-class third B1 fetch (#590). That
35,217-interval run is separate from this 11,660-interval four-hour supplement;
the histories must not be merged into a fabricated single fetch. Preserve the
other branch and snapshot. Its unrun all-class report is not an independent
confirmation and does not replace this frozen four-hour result or its thresholds.

For the next computation, the owner accepted publication first. The
[entry-pilot contract](b2-entry-pilot-contract-v1.md) and its registry scope must be
present in main before that read. It requires no new download and does not open
reserved exit minutes or strategy returns. The source pilot is already complete;
this publication does not erase any prior exposure.
