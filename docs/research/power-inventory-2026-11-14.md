# Power inventory after track B, 2026-11-14

Status: **planning note; registers nothing, reads no data and no return.** All
figures come from the published counts and power reports of each line and from
the [seen-windows registry](seen-windows-registry.md). The owner asked for it
after [track B closed](track-b-closure-2026-11-14.md), to decide between a new
attempt and maintenance until HYP-012 v2's read.

Follow-up: the separately frozen [bounded audit](hyp036-power-audit-result-2026-11-14.md)
reproduced raw projected MDE 19.88 bps; the BTC-adjusted event diagnostic is
14.61 bps. These are conditional discovery projections from four observed
weeks. No exam power or strategy outcome was read, and no full fetch is released.

## What changed since the families were stopped

1. **A new design can make controls diagnostic.** Under the [program plan](program-plan-2026-11.md),
   rule 2, the primary measure is net as traded; "a matched control and its excess
   are diagnostics and gate nothing". Several families stopped before that rule,
   on control coverage or balance, not on the power of their net. This does not
   retroactively pass their old contracts or authorize a new strategy read.
2. **Two calendars are unassigned; they are not two clean exams.** The backward holdout (2023-02-01 to 2024-09-15)
   has only coverage read. The HYP-031 test year (2025-10-01 to 2026-09-28) is
   unassigned again, but it is not fully clean:
   - 2026-07-22 to 2026-09-28 is the own-capture period, read at outcome level by
     the pump, source-lead, momentum-flow and radar studies, including
     abnormal-flow v1;
   - 4 h klines and funding of live spot-perp pairs were read at outcome level for
     the carry screen over 2024-10-01 to 2026-09-28 (delta-neutral positions).
3. **The discovery year is a laboratory.** A stopped family's returns may be read
   there under a written protocol. Families whose returns were already read in the
   2026-11-11 exploratory series have a spent laboratory.

## The families against the money question

MDE is the registered planning MDE of the net leg (a) on the discovery year, with
each study's own inflation and design effect. "Returns read" means on any window.

| Line                              | Stopped at                                  |    MDE (a) net, bps | Returns read                      | Usable now                                        |
| --------------------------------- | ------------------------------------------- | ------------------: | --------------------------------- | ------------------------------------------------- |
| HYP-036 absorbed buying, short 4h | control coverage 56.4 / 73.2%               | **19.9, projected** | **no** (features of pilot blocks) | candidate for a bounded planning audit            |
| HYP-034 premium discount, long 4h | (b) excess MDE 50.13                        |                38.9 | yes, 16 exploratory variants      | no: laboratory spent, track A closed              |
| HYP-033 squeeze continuation, 4h  | control share 61.2%, (b) 61.5               |                43.7 | yes, 33 exploratory variants      | no: laboratory spent, track A closed              |
| Majors, top 10 (pilot)            | MDE 48.4 / 46.8 against 45                  |                48.4 | no                                | borderline; independent exam power unknown        |
| HYP-035 relative strength (daily) | balance 0.132, MDE                          |                88.7 | no                                | no: net power fails on its own                    |
| HYP-037 impulse policies A / B    | calibrated power gate                       |        123.0 / 73.2 | yes, exploratory                  | no                                                |
| Family 5, post-pump short OI turn | MDE on paper                                |               110.8 | partly (54-config pump lab)       | no                                                |
| HYP-032 compressed spring         | (b) not estimable, then concentrated losses | 44.7 / 85.0 / 130.1 | yes, 6 exploratory variants       | closed priority; 4h (a) passed its old power gate |
| BTC long/cash trend               | MDE 31 to 50% a year                        |                 n/a | no                                | no                                                |
| Carry, HYP-031 family             | economics fail                              |                 n/a | yes                               | no: closed on money                               |

Exam power scales roughly with the square root of the window length at an
unchanged event rate: about x0.78 on the holdout (19.5 months), x1.0 on the whole
test year, x1.11 on its part before the own-capture period (9.7 months) and x0.62
on holdout and test year together. These are calendar-only scenarios, not
empirical exam power. Combining exposed and clean periods does not make the
combined sample a clean exam. Event rates in 2023 are uncertain: the
registry's coverage table lists 182 instruments in 2023-01 against 397 in 2024-07,
and a top-150 pool is thinner then.

## HYP-036 merits a bounded planning audit

- **Projected power without controls.** Observed: 784 events, 750 symbol-day
  clusters, 194 symbols, four ISO weeks and 28 decision days. The pilot projects
  9,312 events after the independence rule; top five symbols are 6.8% of observed clusters. Its net leg MDE of
  19.9 bps already counts same-hour dependence: the block model uses the measured
  common share rho of 0.39 to 0.57 by month, plus inflation 1.5 and design effect
  1.2. Both pilot gates that failed (coverage and balance) concern the controls,
  which would become diagnostics in a new design. The 19.9 bps estimate scales
  variance by 28/334 and assumes the pilot's dependence per day transfers to the
  year. It is about 68.7 bps at the pilot's own size. Four observed weeks cannot
  establish the dependence of a full year or of an exam in 2023.
- **Unread returns on every window.** Only the two pilot blocks' features were
  seen (2024-12-29 to 2025-01-26 and 2025-06-22 to 2025-07-20).
- **A proposed mechanism.** Heavy taker buying with little price response might
  reflect absorption. It does not establish that price will fall afterward.
  Ordinary hours sharing a price path were not sufficient to produce usable
  matched controls in the old designs.
- **Bybit only, public data back to 2023:** trade archive, 1-minute klines,
  funding and mark klines. No open interest is needed, so the holdout can be built.

**Risks to put in the design, not to wave away:**

1. **Mirror of a known loss.** Abnormal-flow v1's long on buy dominance with a
   contained price lost at every K on 2026-07 to 2026-09. HYP-036 v1 kept the
   matched excess as its test for that reason. With net as the primary, a pass
   would partly mirror that known result. Different window and different trigger,
   but the selection history must say so, and the excess stays a reported
   diagnostic.
2. **The 19.9 bps planning leg is already unadjusted.** In the actual pilot code
   (`0564134`), `a_net` uses `relative=False`; only matched `b_excess` uses BTC-relative
   scales. The old reading text describes both legs as BTC-relative, so its
   estimand and power proxy disagree. A successor must resolve that explicitly.
   This price-dispersion proxy does not measure net outcomes including variable
   funding. A BTC-adjusted event leg is a separate diagnostic, not `b_excess`.
   For a short, adjustment against a short BTC benchmark is short-asset return
   plus BTC's underlying return. No executed BTC hedge is assumed.
3. **Data size.** The full top-150 tape for the discovery year is about 513 to 526
   GB, streamed and reduced to per-minute parts (HYP-032 measured 11 MB/s, about 13
   hours). The holdout needs its own fetch later.
4. **Short side costs.** Funding is paid or received by the short; the protocol
   counts actual settlements, as B3a did.

## Proposed order

1. **Bounded planning audit on the pilot blocks already held.** Reproduce the
   existing unadjusted planning MDE, compute the BTC-adjusted event diagnostic,
   report each block separately and fixed dependence sensitivities. The
   [audit contract](hyp036-power-audit-v1.md) is fixed before that calculation.
   No fetch, strategy outcome or exam read. A 30 bps comparison applies only to
   the conditional discovery projection; it cannot certify exam power or release
   a larger fetch. The four observed weeks leave longer dependence unidentified.
2. **Design discussion with the colleague**, then a new registration (a new
   hypothesis ID, HYP-036's event definition unchanged): net as traded primary,
   matched excess as a diagnostic, abnormal-flow v1 in the selection history, and
   the exam window assigned before any read. My position: the **backward holdout**
   as the exam, because it is the only window with nothing beyond coverage read; the
   test year stays in reserve.
3. **Registry row in main, then the discovery-year tape fetch** on the owner's go.
4. **Laboratory read** on the owner's go, with B3a's guards: floors, week
   bootstrap, halves, top-five removals, BTC guard, portfolio report.
5. **Exam** only on a laboratory pass, as one frozen batch.

If this audit is unfavorable, do not change its model to obtain a pass. Return
to the owner with the budget result. This inventory does not prove that all
possible strategies fail; it compares the listed designs under different
planning models. A cheap economic read on the existing pilot could be proposed
under a separate fixed contract before buying the full tape. It would be
discovery screening, not confirmation, and is not authorized by this note.
No exam is assigned or combined here.
