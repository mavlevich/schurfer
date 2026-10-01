# HYP-012 v2 administrative stop and blind-window closure

Status: **amendment to [source-lead-forward-cohort-v2.md](source-lead-forward-cohort-v2.md),
registered before any v2 return was read.** No v2 formal claim exists, and the cohort
had zero qualified Bybit episodes at the
[2026-10-01 audit](source-lead-v2-identity-accrual-audit-2026-10-01.md). This amendment
changes neither the v2 estimand, cost model, evidence floor, concentration caps nor its
single-read rule. It adds three things the contract lacked: a capture deadline, an
outcome-blind accrual rule that can close the cohort before that deadline, and an exact
end to the v2 blind window.

## Why

v2 waits for its first 100 resolved episodes over 4 UTC weeks, however long that takes.
The audit found the first 59 hours produced two source-eligible captures on registered
Bybit routes and no qualified episode. Without an end, the cohort can hold the blind
window open indefinitely while it is very unlikely to reach its floor.

The [cost and power planning report](cost-power-planning-v1.md) is context, not the
source of this rule. How many episodes a given effect needs depends on the dispersion and
dependence of returns: for 100 bps net at 80% power its HYP-012c scenario needs 151-250
resolved episodes, while its HYP-012b scenario is only bounded to 100-1,500. Flow sets
the calendar time. The report does not redefine the registered v2 floor of 100.

## The rule

### Capture deadline

A capture whose `source_first_observed_at` is at or after
`COHORT_CAPTURE_DEADLINE = 2027-03-31T00:00:00Z` (cohort start plus 183 days) never enters
v2. The formal reader's qualified-episode query enforces this upper bound
(`source_lead_forward_cohort_qualified_episode_v2`). 183 days is an administrative
ceiling chosen here; it does not follow automatically from the planning report.

### Checkpoints

At each checkpoint instant C (00:00 UTC), the rule counts the qualified v2 episodes on a
tradable venue whose exit bar had closed by C (`episode_is_matured(entry, C)`). The cohort
continues when that count reaches the frozen minimum and is stopped otherwise:

| Checkpoint (00:00 UTC) | Days since start | Days to deadline | Minimum to continue |
| ---------------------- | ---------------: | ---------------: | ------------------: |
| 2026-10-31             |               32 |              151 |                  12 |
| 2026-11-30             |               62 |              121 |                  28 |
| 2026-12-31             |               93 |               90 |                  45 |
| 2027-01-31             |              124 |               59 |                  64 |
| 2027-02-28             |              152 |               31 |                  81 |
| 2027-03-31 (deadline)  |              183 |                0 |                 100 |

**The table is the rule.** Its justification is a Poisson projection: k continues if
`k + U(k) / elapsed_days x remaining_days >= 100`, with U(k) the one-sided 95% upper
bound on a Poisson mean after k events. That projection is optimistic (it also assumes
every qualified episode resolves), and it is a model: clustered pumps, regime changes and
outages can break it. It is not a 95% guarantee about future accrual, and the repeated
monthly checks carry no joint guarantee. A test pins the table to its justification so
the two cannot drift.

### What is counted, and when

- **Outcome-blind.** The snapshot selects capture ids, capture and entry timestamps,
  canonical asset and target venue only (`source_lead_forward_cohort_accrual_snapshot_v1`).
  No book, liquidity, instrument, price, exit bar or return is read. Resolution status,
  which needs exit bars, is deliberately not used: a matured qualified episode is not
  yet a resolved one, so the count is an optimistic upper bound on resolved episodes.
- **Fixed snapshot per checkpoint.** The counted set is the qualified rows with
  `created_at < C` and capture time in `[2026-09-29, C)`. It is defined by the stamp, not
  by what was visible at C: qualification rows are insert-only, but `created_at` is
  `NOW()`, the start of the inserting transaction, so a transaction begun before C and
  committed after it adds a row stamped before C. The set is therefore **final only
  once no transaction that began before C is still open** in the database. Before
  evaluating C, the rule checks `pg_stat_activity` for open transactions started before
  C, and for sessions whose state its role cannot see. If there is either, it decides
  nothing (`snapshot_not_final`) and must be run again later. Once final, every
  snapshot of C is identical, however late it is taken.
- **Missed and repeated runs.** A run evaluates every due checkpoint in order, each on its
  own final snapshot, and records the first one that stops. A late run therefore reaches
  the same decision as an on-time run, and a rerun replays the stored decision artifacts
  or refuses.
- **The reader cannot skip a checkpoint.** Before its first read the formal reader runs
  the same evaluation of every due checkpoint, before it loads any episode, book or quote
  and before it can claim. A checkpoint that stops the cohort stops it there; one that
  is not yet final refuses the read. The outcome no longer depends on which command runs
  first. A claim that already exists is resumed without re-evaluation, because a
  started read is never rewritten.
- **Non-tradable qualified rows.** The v2 reader refuses a cohort with a qualified episode
  outside `TRADABLE_VENUES`; the rule refuses the same way rather than counting around it.

### At the deadline

- Fewer than 100 matured qualified episodes at 2027-03-31: the rule stops the cohort.
- 100 or more: the rule does not stop it, because only resolution status can tell whether
  the registered checkpoint is reachable. The formal reader decides, on captures before
  the deadline only. It runs once; if, 24 hours after the deadline (so every such capture
  has qualified and matured), it still cannot find the checkpoint of 100 resolved
  episodes over 4 weeks, it records the administrative stop
  `checkpoint_unreached_at_deadline` instead of waiting. That decision uses resolution
  status only, never a return.

## The terminal state

A stop is recorded once in `app.formal_read_claims` with status `admin_stopped`, a
`terminal_reason`, the closed capture ids and the SHA-256 of its decision artifact
(migration 0057). It uses the same unique key as a formal claim:

- **Atomic choice.** The stop is an insert that does nothing on conflict. Whichever of
  the stop and the formal claim is inserted first wins; the loser is refused.
- **A started or completed read is never rewritten.** If a formal claim exists, the stop
  is refused and the claim is untouched. A database trigger also forbids any update that
  turns a claim into `admin_stopped` and any update of a terminal row.
- **The reader refuses first.** The formal reader checks for a recorded stop, then
  evaluates the due checkpoints, before it loads any qualified episode, book or quote;
  its claim attempt still refuses a stopped cohort if a stop lands in between.
- **The closing checkpoint never moves.** A rerun with a different reason or artifact is
  refused.

Decision artifacts are written once under `runtime/research/hyp012-v2-administrative-stop/`
(`checkpoint-YYYY-MM-DD.json`, or `deadline.json` for the reader's deadline stop), with
SHA-256 sidecars, and are covered by the nightly research backup.

## What a stop means

- **Not a `fail`.** It is a throughput no-go: the cohort could not plausibly reach its
  registered floor in time. It says nothing about the sign or size of the edge.
- **The closed episodes stay closed.** Every qualified v2 capture before the stop's
  boundary (C for a checkpoint stop, the deadline for a deadline stop) is listed in the
  stop row and its artifact, together with its **closed data window**: its canonical
  asset, from its entry to the close of its 30-minute exit bar. Their returns are never
  computed, by v2 or by any other study.
- **No reuse as forward evidence.** Closed v2 episodes are not a future cohort's
  confirmatory sample just because their returns were never read. A successor that
  widens the identity universe, adds venues or changes the rule is a new version with its
  own registration, activation date, power calculation and stopping rule. Its
  prospective sample starts after that activation, and old captures are never
  requalified under the new universe.

## End of the v2 blind window

Until now, nothing on or after 2026-09-29 could be read for any venue before the v2
formal read, because pumps are shared across venues and reading them would unblind v2.
If v2 is stopped, that read never happens, so the condition is replaced:

1. **The blind window ends when v2 reaches a terminal state:** its completed formal read,
   or a recorded administrative stop. Until then it stands exactly as before.
2. **After a stop, the closed data stays unread, through any venue and any window.** No
   later study may use data of a closed episode's canonical asset, from any venue,
   timestamped inside a closed data window. An episode of a later study is excluded
   when **any** window it uses on that asset intersects a closed window: feature
   lookbacks, labels and outcome horizons alike, not only its decision time. A decision
   at 12:32 with a one-hour lookback reads 11:32 to 12:32 and is excluded by a closed
   window 12:00 to 12:31; so is a decision at 11:50 whose outcome horizon reaches 12:10.
   Excluded episodes are dropped, never imputed. A study that cannot map an instrument to
   a canonical asset applies the exclusion by base ticker. Cross-sectional inputs mask
   the closed windows of the affected assets. The windows are listed in the stop
   artifact (`closed_windows`) so this can be enforced mechanically.
3. **Other data on or after 2026-09-29 becomes available only to a study registered after
   the terminal state**, under that study's own protocol. Data dated before its
   registration is historical for it (discovery or validation), never prospective.
4. **A stop authorizes nothing else.** It does not start a new data feed or canary, does
   not change the 2026-10-31 research-slot decision, and does not allow reading HYP-015
   or any other cohort outside its own registered rules.

## Operations

- The rule runs with `make prod-hyp012-v2-administrative-stop` from a clean `main`, on
  or after each checkpoint. It is idempotent; a run before 2026-10-31 evaluates nothing.
- A run that reports `snapshot_not_final` decided nothing: a transaction begun before
  the checkpoint (for example a long backup) is still open. Run it again later; if it
  keeps refusing, inspect `pg_stat_activity`. Every production service connects as the
  same database role, so the rule can see every session's state.
- The capture and shadow workers are a separate operational decision for the owner.
  Their continuation never extends v2: the deadline bounds the reader's query.
- Waiting for passive v2 accrual does not hold the main development slot. The 2026-10-31
  reassessment remains a decision about spending and research direction.
