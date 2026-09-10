"""Which decision represents an episode, expressed once.

The rule is the replay's: the first decision that opened something, else the
earliest. Both entry-signal studies need it, and until now both applied it in
Python to rows the SQL had already filtered down to decisions **with a completed
outcome**. That is not the same rule.

A colleague reproduced the consequence on a real Postgres: an episode whose first
`opened_paper` decision was still unresolved, and whose later `skipped` decision
had a complete outcome, came back represented by the skipped one -- a different
decision, at a different `pump_age`, in a different comparison group. On the
HYP-023 discovery window the substitution fired on **24 of 822 episodes**, and
**19** of those crossed the 0.6-minute boundary the HYP-027 groups are split on.

It is also not stable in time. The substitution disappears once the first
decision's outcome resolves, so the same contract over the same window measures
different episodes depending on when it is run, and the sample manifest that is
supposed to make a re-read detectable would be comparing the wrong thing.

So the selection happens first, over every decision in the window, and the
outcome of exactly that decision is joined afterwards. An episode whose chosen
decision has no completed outcome is **coverage**, reported as such, never
replaced by a decision that does.
"""

from __future__ import annotations

# `left(d.action, 6) = 'opened'` rather than `LIKE 'opened%'`: identical rule,
# and no per-cent sign to survive two layers of parameter interpolation.
EPISODE_DECISION_CTE = """
    WITH episode_decision AS (
        SELECT DISTINCT ON (d.pump_event_id)
               d.decision_id,
               d.pump_event_id,
               d.base,
               d.ts,
               d.action,
               d.features->'signal'->'components' AS components
        FROM app.trade_decisions d
        WHERE d.strategy_version = ANY(:strategies)
          AND d.ts >= :since
          AND d.ts < :until
          AND d.pump_event_id IS NOT NULL
        ORDER BY d.pump_event_id, (left(d.action, 6) = 'opened') DESC, d.ts
    )
"""

_OUTCOME_JOIN = """
    FROM episode_decision e
    LEFT JOIN app.trade_decision_outcomes o
      ON o.decision_id = e.decision_id
     AND o.horizon_minutes = :horizon
     AND o.resolver_version = :resolver_version
     AND o.status = 'complete'
     AND o.short_return_pct IS NOT NULL
    ORDER BY e.pump_event_id
"""


def episode_decision_query(*outcome_columns: str) -> str:
    """The episode's decision, with that decision's own outcome columns.

    `outcome_columns` are taken from `app.trade_decision_outcomes` and are NULL
    when the chosen decision has no completed outcome from the requested
    resolver version. They are nullable on purpose: a study must decide what to
    do with an incomplete episode, and silently swapping in another decision or
    resolver is not one of the options.
    """
    selected = ", ".join(f"o.{column}" for column in outcome_columns)
    return (
        EPISODE_DECISION_CTE
        + "    SELECT e.decision_id, e.pump_event_id, e.base, e.ts, e.action, e.components,\n"
        + f"           {selected}\n"
        + _OUTCOME_JOIN
    )
