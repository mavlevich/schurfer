# HYP-015 formal read on 2026-11-04 (runbook v1)

The single registered read of the hold12h verdict
([registration](../research/momentum-flow-hold12h-verdict-v1.md)): cohort
`[2026-10-05, 2026-11-02)` UTC, read opening **2026-11-04 12:00 UTC** (60 h after the
cohort end). There is one read. A completed claim refuses every later run, so this page
is followed in order.

A `candidate` result authorizes only the next research stage (the episode study or a
real shadow), never live trading.

## Before the read

1. **Owner's go for the read itself.** Formal reads are never started on a standing
   permission.
2. **Host checkout:** on `main`, pulled, clean. If the analytics image is older than
   the checkout, rebuild it first: `make prod-deploy-svc SERVICE=analytics` (no
   migration is involved). The read refuses an image whose sources differ from the
   checkout.
3. **Paper worker:** `make prod-momentum-paper-hold12h-health` shows it healthy through
   the cohort end. Every position from a decision before 2026-11-02 closes within its
   720 minutes, so by 2026-11-02 12:00 UTC.
4. **Funding:** `make prod-hold12h-funding-capture-health`. The capture must have run
   after the last exit plus its 8 h settlement lag (from about 2026-11-02 20:00 UTC).
   If the last summary still lists pending windows, run
   `make prod-hold12h-funding-capture-run` and check again.
5. **Inputs preserved** ([archive design](hyp015-inputs-archive-design-v1.md)): the
   final snapshot set is taken, verified and restore-checked:
   `make prod-hyp015-snapshot-set`, `make prod-hyp015-verify-set SET=<id>`,
   `make prod-hyp015-restore-check SET=<id>`.
6. **Disk:** `make prod-disk-usage-health`; free space stays above the 10 GiB reserve.

## Run

From 2026-11-04 12:00 UTC, on the host in `/opt/schurfer`:

```bash
make prod-hyp015-formal-read
```

In order, and nothing is written until all of them pass:

- `main`, clean tree;
- the analytics image's `source_digest` equals the checkout's;
- the reader's `--preflight` (no database): the contract is registered, the prefix is
  the frozen one and the read is open now, with the date taken from the contract;
- then the formal run, which refuses before its claim if the cohort is incomplete,
  commits the claim before any return is read, pins the input snapshot in the claim,
  computes the verdict from that snapshot only and publishes it once.

The verdict JSON is printed at the end.

## Where the result is

- `/opt/schurfer/runtime/research/hyp015-verdict/` (`/verdict` inside the container,
  which is what the claim records as its output directory):
  - `hold12h_verdict.json` and `hold12h_verdict.sha256`: the result;
  - `inputs.<digest>.json`: the pinned snapshot it was computed from;
  - `hold12h_verdict.attempt-<owner>.json` (+ `.sha256`): each attempt's own copy.
- `app.hold12h_formal_read_claims`: one row, `status = 'completed'`, naming the
  artifact and its sha256 and the snapshot digest.
- The directory is under `runtime/research`, so the nightly `research-*` backup takes
  it. The result is then committed as evidence with its hash in a PR.

## When it refuses or fails

| What it says                                                                                                                  | Meaning and action                                                                                                                                                                                                          |
| ----------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `not on main`, `dirty working tree`                                                                                           | Nothing written. Fix the checkout, retry.                                                                                                                                                                                   |
| `the analytics image is not built from ...`                                                                                   | Nothing written. Rebuild the image (step 2), retry.                                                                                                                                                                         |
| `too early, the read opens at ...`                                                                                            | Nothing written. Wait.                                                                                                                                                                                                      |
| `cohort not complete yet (...)`                                                                                               | Nothing claimed. Positions still open, or funding or accounting missing: finish steps 3 and 4, retry. Reading it as it is (`ACCEPT_INCOMPLETE=yes`) is an owner decision; the claim records it.                             |
| `another run holds the open claim's lease`                                                                                    | A run is in flight, or one crashed after its claim. Wait for its lease (60 minutes), retry: it resumes on the same pinned snapshot and publishes the same result.                                                           |
| a crash or lost connection during the run                                                                                     | First look at the claim (below). Claim `claimed`: retry after the lease. Claim `completed` but `hold12h_verdict.json` or its `.sha256` missing: republish (below). Never delete the claim row or any file in the directory. |
| `inputs changed since the claim was pinned`, `WATCH set changed`, `does not match its digest`, republish `integrity incident` | Integrity incident. Stop, do not retry, do not edit the claim. Report with the claim row and the directory listing.                                                                                                         |
| `already claimed and completed`                                                                                               | The read is done. If both `hold12h_verdict.json` and `hold12h_verdict.sha256` are there, the result is final: never run it again. If one is missing, republish (below).                                                     |

## A completed claim without the published result

The read writes its attempt file, then completes the claim, then publishes
`hold12h_verdict.json` and `hold12h_verdict.sha256`. A crash between the last two steps
leaves the claim `completed` with the result only in the attempt file, and a new
`--formal-run` refuses for good. Check the state:

```bash
docker exec schurfer-postgres psql -U schurfer -d schurfer -c "SELECT id, status, artifact_name, artifact_sha256, completed_at FROM app.hold12h_formal_read_claims WHERE contract_version = 'hold12h_verdict_v1'"
ls -la /opt/schurfer/runtime/research/hyp015-verdict/
```

If the status is `completed`, the named attempt file is there and one of the two result
files is missing, finish the publication:

```bash
make prod-hyp015-formal-republish
```

It reads only the claim row and the attempt file the claim names, checks that file
against the claim's sha256 and its own `.sha256`, and publishes those exact bytes. It
recomputes nothing, reads no cohort row and does not write the claim; running it again
changes nothing. If it reports an `integrity incident` (the attempt file or an existing
result differs from the claim), stop and report as in the table above. Never reset or
edit the claim to read again.

## Rehearsal

`apps/analytics/tests/test_hyp015_formal_read_rehearsal_integration.py` runs this exact
CLI on the disposable database of `make verify` (every migration applied) with a
synthetic cohort and the registered funding capture against a pinned Bybit fixture. It
covers the published content and provenance, the final claim, a second run, a crash
before and after the attempt file, a crash after the completed claim before each result
file (finished by republishing), a tampered attempt file, an early run, and both
incomplete-cohort paths.
