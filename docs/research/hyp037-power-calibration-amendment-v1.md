# HYP-037 power calibration amendment proposal v1

Date: 2026-11-11. Status: design proposal after preflight review. No additional
historical calculation has run under this proposal. Review and commit the
amendment, machine registration and code before the first calibration invocation.

## Why the old estimate cannot decide readiness

The initial report recorded A/B MDE of 12.79/49.23 bps under the assumed design
effect 1.2. The count check stands. Clustered power is pending. There are 51 ISO
weeks, with synchronized and overlapping holds, while the intended outcome
inference resamples complete weeks. A per-event variance sum times an assumed
constant does not establish that inference's power.

The original run remains preserved with its source bytes and evidence in local
commit `b1ef6c7`. Its rules version and seed retain the legacy `hyp033` name.
The original registration document, JSON and evidence hashes stay fixed. This
proposal adds a separate calibration stage and future registration, rather than
replacing or rerunning those files.

## Proposed calculation, fixed before the additional read

Use the same hash-pinned features and exact frozen A/B event identities. Derive
seven sets of historical return proxies for every event, ending 0, 24, 48, 72,
96, 120 and 144 hours before the impulse bar starts. For an impulse bar [t-1h,t)
and decision d=t+1h, each proxy is a four-hour close-to-close return ending at
`d - 2h - lag`. Both closes must precede the impulse bar. The impulse bar and
future path are excluded from that event's proxies.

The data read is pre-entry return **dispersion and dependence**. A proxy for a
later event can overlap an earlier event's path, as was already possible in the
v1 volatility histories. Register that exposure explicitly. No setup mean, net,
proxy mean, directional diagnostic or individual proxy is emitted.

For each lag and candidate:

1. Retain the frozen intended denominator. Require at least 95% defined proxies,
   at least 150 defined symbol-days, and representation of every original event
   ISO week. Otherwise calibration is `not_estimable`, with the missing counts.
2. Compute the independent proxy-mean variance as sample variance divided by the
   defined proxy count. A zero or non-finite variance is `not_estimable`.
3. Use 10,000 complete-week resamples. Recompute the event-weighted proxy mean in
   each draw, with common week draws for A and B. Require at least 95% defined
   draws. Emit the variance across draws, never the proxy means.
4. The empirical design-effect proxy is bootstrap variance divided by independent
   variance. Use the maximum across all seven predetermined lags and the original
   floor 1.2. Every lag must be estimable. No lag is selected by its favorable MDE.
5. Multiply the original planning MDE by `sqrt(calibrated_effect / 1.2)`, retaining
   inflation 1.5 and the two-candidate one-sided tail 0.025. Report whether the
   adjusted estimate meets 50 bps, conditional on transferring pre-entry dependence
   and the registered inflation to post-entry returns.

Seven lags provide a fixed sensitivity envelope, not seven times more independent
events. The calculation estimates a historical dependence proxy; actual conditional
post-impulse dispersion can differ. Its verdict must carry that limitation. Any
failure or uncertainty is reported before deciding the empirical-read contract.

## Economic threshold and statistical power

Distinguish the +30 bps economic minimum from the effect used for planning power.
MDE here is the effect detectable against zero at 80% planning power. For a true
mean of +30 bps, the original B estimate gives only about 40% normal-approximation
power against zero. Requiring the observed mean to reach +30 bps is an additional
condition. Even as uncertainty tends to zero, a true mean exactly on that boundary
does not imply an 80% probability of passing the point-estimate condition.

Report normal-approximation power at assumed true net +30 and +50 bps using the
adjusted standard error, both for the bound above zero and for the joint economic
conditions. Preserve the fixed +30 bps minimum. No outcome has been read that could
justify changing it, and this amendment changes no entry, filter, cost or exit.

## Evidence and registration requirements

The future machine registration must bind the frozen counts and event hashes,
features SHA, this protocol SHA, lag list, completeness rule, bootstrap method,
seed, alpha, inflation and aggregation rule. Commit it before reading proxies.
Run from that clean revision, record the full Git porcelain list, code hashes and
proxy coverage, then seal a new output directory. The prototype source file and
its tests belong to that revision.

The calibration is a new recorded method stage after seeing the original planning
numbers. Its result cannot be presented as an independently selected power model.
Confirmation multiplicity includes HYP-033 v1, both HYP-037 candidates and relevant
screened predecessor variants. The final correction is fixed for the whole batch.
