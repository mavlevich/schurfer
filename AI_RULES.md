# Schurfer AI Engineering Rules

This is the repository-wide source of truth for AI assistants. Apply these
rules in addition to the user's request. Keep tool-specific entrypoints thin;
they should point here instead of copying and drifting from these rules.

## Evidence and scope

- Inspect `git status`, `HEAD`, the merge base, staged changes, unstaged
  changes, and untracked files before reviewing or editing. A clean working
  tree does not mean a branch has no diff, and staged content can differ from
  the working tree.
- Preserve unrelated and user-owned changes. Do not delete or rewrite
  untracked files, generated helpers, backups, or local configuration unless
  the user explicitly authorizes it.
- Never claim a test, lint, migration, deploy, or production health check
  passed unless the exact command was run and its result was observed. State
  what was not run and why.
- Every bug fix needs a regression test that fails for the original bug.
  Prefer boundary and integration tests when the failure crossed a database,
  Redis, exchange, container, or API boundary.
- Reuse an existing abstraction when its contract fits. Introduce a shared
  abstraction only when its invariants and at least two real consumers are
  clear; avoid speculative frameworks and copy-pasted strategy forks.
- Temporary rewrite scripts and `*.bak` files are development artifacts, not
  product code. Do not include them in commits.
- Never expose credentials in commands, logs, reports, or review output. Rules
  and skills describe safe workflows; they never grant permission to mutate
  production, place orders, run repairs, or delete data.

## Trading and execution invariants

- Resolve instruments through the canonical identity and route layer. Never
  reconstruct derivatives symbols with string concatenation such as
  `f"{base}/USDT:USDT"`, and never mix exchange-native and CCXT symbols.
- Read normalized strategy identity through `journal.strategy_identity(...)`;
  do not infer it from ad-hoc `setup_context` keys in APIs, statistics, or UI.
- New strategy entries go through `ExecutionIntent` and the `Broker` protocol.
  Check `TradingMode.DISABLED` before claims, durable decisions, or side
  effects. Unsupported or unsafe modes fail at startup, not on first signal.
- Entry is an idempotent, atomic lifecycle transition. Persist episode/intent
  identity and the idempotency key before execution; do not use
  check-then-insert. Redis is an accelerator, not the sole accounting source.
- A live order path requires durable intent, deterministic exchange
  `clientOrderId`, restart-safe reconciliation, bounded retries, exposure
  limits, a circuit breaker, and a kill switch. A paper implementation alone
  does not make a live mode safe.
- Paper and live accounting must be directional and evidence-based: correct
  bid/ask side, filled notional, fees, funding, and entry/exit slippage.
  `accounting_status='complete'` is allowed only when required evidence exists.
- A market-quality timeout or missing route must reject or defer the entry
  explicitly; it must not silently fall back to blind execution.
- A computed safety/mode decision must be the same value the actual
  branch/gate reads, not two independently-computed things that happen to
  usually agree. Test that a resolved mode and the code path it is meant to
  control cannot silently disagree, not just that the resolver itself
  computes correctly in isolation.
- Gate a disabled strategy or mode before expensive work starts (DB queries,
  liquidity/order-book fetches, exchange calls), not only at the final
  broker/order call. A late-stage reject still burns a full tick's cost for
  nothing.
- A deliberate operator/config choice (disabled, off, paused) is not an
  incident. Classify and report it as intentional state; do not claim or
  burn incident-tracking resources (leases, alerts, retries) over an
  intentional no-op.

## Research invariants

- Build features point-in-time. Partition market windows by exchange,
  instrument, market type, and data/capture version as applicable. Never
  interleave venues or use future-known identity, listing, or outcome data.
- Normalize venue data through a shared provenance envelope, not by pretending
  every venue has identical semantics. Preserve the native payload or immutable
  source artifact, declare unsupported capabilities and coverage explicitly,
  and keep venue-specific typed fields out of fabricated zero/default values.
- Treat instrument creation, announced listing, trading-open, first-observed,
  suspension, resumption, and delisting as distinct lifecycle timestamps. Map
  exchange fields from verified endpoint semantics; a conveniently named field
  such as `createTime` is not evidence that trading opened at that time.
- Classify external market paths as exact native, same-asset cross-venue proxy,
  third-party, or unrecoverable. Persist provenance and checksums, and never mix
  proxy outcomes with native execution evidence in one denominator.
- Separate discovery, validation, test, and prospective cohorts. Do not tune
  on validation/test results. Register parameter changes before reading the
  prospective cohort. Use the registered evidence floor (100 resolved
  episodes by default) and document any justified exception.
- Evaluate net economics, not Telegram wins or gross PnL alone. Include fees,
  funding, spread/slippage, unresolved outcomes, concurrency, capital
  occupancy, drawdown, losing streaks, and asset/time concentration.
- A negative mature test EV is `FAIL`; insufficient diversity is additional
  context and must not mask a demonstrated negative result. Positive but
  immature evidence remains `insufficient_data`, not a promotion.
- Formal reports record cohort boundaries, strategy/version, code revision,
  dirty-tree state, data/capture versions, cost model, and stable input/path
  fingerprints. Mutable live-exchange data must be snapshotted durably before
  it can support a reproducibility claim.
- Optimize performance only after measuring the actual bottleneck. Preserve
  semantics with before/after benchmarks and equivalence tests; prefer SQL
  pushdown and bounded batching when evidence supports them.
- Pure selection/business logic factored out for easy unit testing against
  hand-built rows is good, but keep at least one test exercising the real
  query or repository path end to end. A real query can carry a bug (wrong
  ordering, wrong join, wrong filter) that many green tests against
  hand-built rows will never see.

## Observability and diagnosability

- A zero or empty result must be explainable through explicit reason or
  rejection counters, not presented as a bare unexplained number.
  Distinguish "nothing qualified" from "a quality gate rejected everything"
  from "the pipeline did not run at all".
- A frequently-polled read or health endpoint must stay idempotent in its
  side effects under concurrent or repeated reads. Dedupe by the underlying
  event's own identity (e.g. its completed-at timestamp), not a bare
  increment, so polling itself can never inflate a counter.

## Database and distributed-state invariants

- Schema changes use Alembic migrations, explicit `app`/`timeseries` schemas,
  and real PostgreSQL integration tests. Verify upgrade behavior and rollback
  where rollback is supported.
- Enforce uniqueness and ownership in the database for claims, episodes,
  entries, and closes. In-memory checks and Redis locks are not substitutes
  for constraints and atomic SQL.
- Any Redis/PostgreSQL dual write needs a declared source of truth,
  idempotency, crash-window analysis, and reconciliation/repair behavior.
- Treat timestamps as timezone-aware UTC and distinguish event time, receive
  time, persistence time, and processing time.

## Review, Git, and delivery

- During a review, do not modify the branch unless the user asks for fixes.
  Report findings by severity with concrete file/line evidence, then list
  executed gates and remaining uncertainty.
- Do not use `git rebase` in this repository. Start from updated `main`; update
  stale feature branches with `git merge origin/main`.
- PR titles use Conventional Commits. PR descriptions are English raw
  Markdown and include rationale, behavioral impact, migrations/deployment,
  risks, rollback, and exact verification performed.
- Deploy only merged code from a clean `main`. Use
  `make prod-deploy-svc SERVICE=<name>` only for code-only changes without
  migrations; schema or cross-service changes use the migration/full-deploy
  path and a backup.
  Always verify the deployed revision, container restart count, logs, health,
  database state, and relevant Redis/database consistency.

## Task-specific skills

Use the repo-local skills in `.agents/skills/` when applicable:

- `schurfer-pr-review`: branch/PR correctness and merge readiness.
- `schurfer-execution-safety`: brokers, orders, episodes, accounting, and live
  trading paths.
- `schurfer-research-integrity`: datasets, backtests, evidence, and promotion
  gates.
- `schurfer-production-deploy`: scoped deployment and production verification.
