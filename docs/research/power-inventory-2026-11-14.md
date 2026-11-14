# Power inventory after track B, 2026-11-14

Status: **planning note; registers nothing, reads no data and no return.** All
figures come from the published counts and power reports of each line and from
the [seen-windows registry](seen-windows-registry.md). The owner asked for it
after [track B closed](track-b-closure-2026-11-14.md), to decide between a new
attempt and maintenance until HYP-012 v2's read.

## What changed since the families were stopped

1. **Controls no longer gate.** Under the [program plan](program-plan-2026-11.md),
   rule 2, the primary measure is net as traded; "a matched control and its excess
   are diagnostics and gate nothing". Several families stopped before that rule,
   on control coverage or balance, not on the power of their net.
2. **Two exam windows are free.** The backward holdout (2023-02-01 to 2024-09-15)
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

| Line                              | Stopped at                    | MDE (a) net, bps | Returns read                      | Usable now                              |
| --------------------------------- | ----------------------------- | ---------------: | --------------------------------- | --------------------------------------- |
| HYP-036 absorbed buying, short 4h | control coverage 56.4 / 73.2% |         **19.9** | **no** (features of pilot blocks) | **yes, the only clear fit**             |
| HYP-034 premium discount, long 4h | (b) excess MDE 50.13          |             38.9 | yes, 16 exploratory variants      | no: laboratory spent, track A closed    |
| HYP-033 squeeze continuation, 4h  | control share 61.2%, (b) 61.5 |             43.7 | yes, 33 exploratory variants      | no: laboratory spent, track A closed    |
| Majors, top 10 (pilot)            | MDE 48.4 / 46.8 against 45    |             48.4 | no                                | borderline; only with both exam windows |
| HYP-035 relative strength (daily) | balance 0.132, MDE            |             88.7 | no                                | no: net power fails on its own          |
| HYP-037 impulse policies A / B    | calibrated power gate         |     123.0 / 73.2 | yes, exploratory                  | no                                      |
| Family 5, post-pump short OI turn | MDE on paper                  |            110.8 | partly (54-config pump lab)       | no                                      |
| HYP-032 compressed spring         | insufficient power            |   34 to 130 by H | yes, 6 exploratory variants       | no                                      |
| BTC long/cash trend               | MDE 31 to 50% a year          |              n/a | no                                | no                                      |
| Carry, HYP-031 family             | economics fail                |              n/a | yes                               | no: closed on money                     |

Exam power scales roughly with the square root of the window length at an
unchanged event rate: about x0.78 on the holdout (19.5 months), x1.0 on the whole
test year, x1.11 on its part before the own-capture period (9.7 months) and x0.62
on holdout and test year together. Event rates in 2023 are uncertain: the
registry's coverage table lists 182 instruments in 2023-01 against 397 in 2024-07,
and a top-150 pool is thinner then.

## HYP-036 is the only clear candidate

- **Power without controls.** The pilot projects 9,312 events after the
  independence rule (194 symbols, top five 6.8% of clusters). Its net leg MDE of
  19.9 bps already counts same-hour dependence: the block model uses the measured
  common share rho of 0.39 to 0.57 by month, plus inflation 1.5 and design effect
  1.2. Both pilot gates that failed (coverage and balance) concern the controls,
  which now gate nothing.
- **Unread returns on every window.** Only the two pilot blocks' features were
  seen (2024-12-29 to 2025-01-26 and 2025-06-22 to 2025-07-20).
- **A mechanism, not a price pattern.** Heavy taker buying that fails to move the
  price means passive sellers absorbed it. The treatment is flow, so ordinary
  hours share its price path.
- **Bybit only, public data back to 2023:** trade archive, 1-minute klines,
  funding and mark klines. No open interest is needed, so the holdout can be built.

**Risks to put in the design, not to wave away:**

1. **Mirror of a known loss.** Abnormal-flow v1's long on buy dominance with a
   contained price lost at every K on 2026-07 to 2026-09. HYP-036 v1 kept the
   matched excess as its test for that reason. With net as the primary, a pass
   would partly mirror that known result. Different window and different trigger,
   but the selection history must say so, and the excess stays a reported
   diagnostic.
2. **The 19.9 bps leg is BTC-adjusted.** HYP-036 measured each leg as its hold
   minus BTCUSDT. The program's primary is net as traded, with BTC adjustment as a
   guard. The MDE of the unadjusted net is not computed yet and will be larger.
3. **Data size.** The full top-150 tape for the discovery year is about 513 to 526
   GB, streamed and reduced to per-minute parts (HYP-032 measured 11 MB/s, about 13
   hours). The holdout needs its own fetch later.
4. **Short side costs.** Funding is paid or received by the short; the protocol
   counts actual settlements, as B3a did.

## Proposed order

1. **Outcome-blind power recheck on the pilot blocks already held.** Compute the
   MDE of the unadjusted net and of the BTC-adjusted net with HYP-036's block model
   (pre-t scales only), for discovery and for each exam window above. No fetch, no
   return. Gate: unadjusted net MDE at most 30 bps on the discovery year and on the
   chosen exam window.
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

If step 1 fails, no family fits the money question on the free windows, and the
honest move is maintenance until the v2 read in March. The majors line is the
only fallback, and only with both exam windows spent on it together.
