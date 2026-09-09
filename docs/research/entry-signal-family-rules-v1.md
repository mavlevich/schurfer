# Entry-signal family: rules that bind all three hypotheses

**Status: registered 2026-09-08, before any outcome was joined to any feature.**

HYP-023, HYP-024 and HYP-025 all search the same recorded data for something an
entry could be built on. These rules apply to every one of them and were added
after a colleague found that the three, as first written, would have counted
evidence they do not have.

## 1. The unit of observation is the episode, not the decision

The first draft set an evidence floor of 500 outcomes per quintile and counted
rows in `app.trade_decisions`. That is not 500 observations.

The scanner evaluates a live pump roughly once a minute, so a single episode
produces hundreds of decisions whose 60-minute outcomes overlap almost
completely. A quintile could clear 500 rows while containing a handful of market
events, and the arithmetic would look fine.

**One decision per episode enters any metric.** The selection rule, fixed here
so it cannot be chosen after seeing which choice helps:

- the first decision in the episode whose action is in `TAKEN_ACTIONS`
  (`opened`, `opened_dry_run`), if any;
- otherwise the episode's earliest decision.

That is `select_episode_decision` in `virtual_strategy.py`, unchanged, so this
family selects the same point in time the replay already selects. Reusing it is
the point: two research lines that disagree about which decision represents an
episode cannot be compared.

## 2. Evidence floors count episodes and clusters

Every floor in every one of the three contracts is **episodes**, and every one
is paired with a diversity floor.

- **150 episodes** per quintile compared, not 500 decisions.
- **30 distinct asset clusters** across the compared quintiles, using the same
  `cluster_key` the replay uses.

The cluster floor matters more than the episode floor. Fifty episodes of one
token that pumped repeatedly is one observation about that token, not fifty
about the market, and no amount of row counting distinguishes them.

## 3. A negative verdict needs as much data as a positive one

The first draft required 500 outcomes to call something a candidate and nothing
at all to call it dead. A sparse component with poor coverage would then produce
"no signal" from the absence of data rather than from the presence of noise, and
`mad_score` is recorded on 4,067 of 62,168 decisions.

**Sufficiency is checked before any verdict, in either direction.** Below the
floors the result is `inconclusive`, never "rejected" and never "not worth its
cost".

## 4. No outcome may straddle a window boundary

A decision taken forty minutes before the discovery window closes has a
60-minute outcome measured partly inside the held-out window. The two windows
then share data at the seam, silently, exactly where nobody looks.

**A decision enters a window only if `decision_at + horizon <= window_end`.**
That is `crosses_window_boundary` in `research_contract.py`, and it is applied
to both windows rather than only to the holdout.

## 5. A feature counts only if it was available when the decision was made

Stated in full for HYP-025, where it decides the whole result, and binding on
all three.

A timestamp on a row is not evidence that the value existed at that time.
`app.pump_derivatives_context_samples` carries `source_at`, `created_at` and a
run's own `resolved_at`, and holds historically reconstructed values. A feature
whose `source_at` precedes a decision may have been written hours later.

**Availability, not recency, is the test.** For every field used:

- the exchange and instrument must match the decision's own;
- the publication semantics must be stated: what the venue's timestamp means and
  when the value became observable;
- a maximum tolerated delay must be declared before the field is used.

Reconstructed values whose availability cannot be established stay a separate,
clearly labelled research approximation. They may not enter a metric that is
read as evidence about a tradeable signal, because a signal you could not have
seen is not a signal.

## 6. A partition the feature does not impose is not a partition

Added 2026-09-09, from HYP-023. It reported `pump_age` as a candidate on a
top-to-bottom quintile spread of -2.89 points that was monotone across all five
quintiles, and the candidate was withdrawn before publication.

The reason is arithmetic rather than judgement. `pump_age` is recorded to a tenth
of a minute and the scanner reaches most pumps within a minute of detection, so
821 episodes carry only **142 distinct values** and 368 of them read exactly 0.6.
A quintile is 164 episodes, which put **all four** boundaries inside a tied value
and made quintile two one value wide. Which side of a cut a tied episode landed on
was decided by the sort's tie order, so the medians of quintiles two and three
were two arbitrary halves of one group of identical measurements. Their difference
pointed the way the neighbours already pointed, and that is what produced the
`monotone` verdict. A different tie order breaks it with no data changing.

`oi_trend` is worse on the same measure: **606 of 821** episodes share one value.

So, binding on all of these hypotheses:

- **A candidate requires that every adjacent pair of compared buckets differ in
  the feature.** A boundary inside a tied value imposes no ordering, so the
  monotonicity read across it can be neither believed nor disbelieved.
- **Every report states, per feature, the number of distinct values and the size
  of the largest tied group.** A coverage count of 821 must not be able to stand
  in for 821 measurements.
- **A rank-based partition is the thing to distrust first** on any feature
  recorded at coarse resolution, which includes anything discretised, rounded,
  clipped, or sampled on a schedule. Where the data has natural tied groups, they
  are better buckets than quantiles, and their cutoffs are absolute rather than
  refitted per window.

This check is strictly stricter than what it replaces: it can withdraw a
candidate and can never create one. That asymmetry is what allowed it to be
applied to a window that had already been read, and `score_component_study.py`
carries a test asserting it.

## 7. The family multiplies, and the reader is told so

Three independent searches produce a finding at a 5% threshold about 14% of the
time by chance. Every one of the three states this, and a positive result in one
of them is weaker evidence than the same result from a single registered search.

None of the three promotes anything on a discovery-window result. Each requires
its own held-out window, and after 2026-09-08 the only genuinely unread data is
data that does not exist yet.
