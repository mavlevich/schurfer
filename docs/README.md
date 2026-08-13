# Schurfer documentation

Status: current documentation index and governance contract.

Last reviewed: 2026-08-13.

This index answers two questions: where a fact belongs, and which document wins when
two descriptions disagree. It does not make every older document current. Known drift
is listed explicitly so historical plans cannot be mistaken for deployed behavior.

## Start here

| Need                                           | Read                                                              |
| ---------------------------------------------- | ----------------------------------------------------------------- |
| Install and run locally                        | [Root README](../README.md)                                       |
| Current priorities and research gates          | [ROADMAP](../ROADMAP.md)                                          |
| Browse uncommitted ideas                       | [IDEAS](../IDEAS.md), which is not an implementation queue        |
| Current service overview                       | [Architecture](../ARCHITECTURE.md) and the deployed Compose files |
| Deploy, recover, inspect production            | [Runbooks](runbooks/README.md)                                    |
| Understand an accepted design decision         | [ADR index](adr/README.md)                                        |
| Review frozen research methods and results     | [Research](#research)                                             |
| Review strategy lifecycle and production rules | [Strategies](strategies/README.md)                                |
| Review interface and delivery contracts        | [Contracts](contracts/)                                           |
| Plan UI evolution                              | [Web UI evolution plan](architecture/web-ui-evolution-v1.md)      |

## Source-of-truth map

No Markdown file overrides running code, a database constraint, or an exchange API.
The table identifies the maintained documentation authority for each kind of claim.

| Subject                         | Documentation authority                                         | Runtime authority                                        | Conflict rule                                                                 |
| ------------------------------- | --------------------------------------------------------------- | -------------------------------------------------------- | ----------------------------------------------------------------------------- |
| Product priority and sequencing | [ROADMAP](../ROADMAP.md)                                        | n/a                                                      | New work follows the latest dated active-course section and its gates.        |
| Uncommitted idea backlog        | [IDEAS](../IDEAS.md)                                            | n/a                                                      | An idea becomes work only after ROADMAP gives it a gate and priority.         |
| Service topology and data flow  | [Architecture](../ARCHITECTURE.md)                              | `infra/docker/docker-compose.*.yml`, service entrypoints | Compose and entrypoints win; record drift before changing architecture prose. |
| Local setup and common commands | [Root README](../README.md), [Contributing](../CONTRIBUTING.md) | `Makefile`, package manifests                            | Commands must exist in the Makefile or package scripts.                       |
| Production operations           | [Runbooks](runbooks/README.md)                                  | prod Compose, systemd units, operational scripts         | A runbook change ships with the operational change it describes.              |
| Durable architecture decisions  | [ADRs](adr/README.md)                                           | accepted implementation                                  | Never rewrite decision history; supersede an ADR and link the replacement.    |
| Frozen research methodology     | versioned files in [`research/`](research/)                     | report constants, manifests, cohort tables               | The registered contract and code must agree before a canonical read.          |
| Research-family status          | [Discovery ledger](research/discovery-ledger.md)                | archived report manifest                                 | A descriptive number does not override the ledger verdict.                    |
| Strategy behavior               | versioned files in [`strategies/`](strategies/)                 | strategy version constants and execution code            | Code changes require an explicit strategy/contract version decision.          |
| Wire and delivery behavior      | versioned files in [`contracts/`](contracts/)                   | schemas, migrations, producer/consumer tests             | Consumers fail closed on unsupported versions.                                |
| UI information architecture     | [Web UI evolution plan](architecture/web-ui-evolution-v1.md)    | routes, API contracts, component tests                   | The plan is a target, not a claim that an unfinished route exists.            |

## Document states

Use one of these labels near the top of a new or materially revised document:

- `current`: describes behavior that exists now;
- `active contract`: frozen rules for an active prospective or operational process;
- `target`: reviewed future design, not deployed behavior;
- `historical`: preserves context but is not an active instruction;
- `retired`: the lane or procedure is stopped and must not be restarted implicitly;
- `superseded`: replaced by a linked decision or document.

An ADR additionally uses `Proposed`, `Accepted`, `Rejected`, or `Superseded`. Change
only its status and supersession links after acceptance; preserve its original
context and decision text. If implementation drift predates this policy and no
replacement ADR exists yet, link the current runtime evidence and name the bounded
follow-up that will record the replacement.

## Research

Research documents have different roles and must not be blended:

- [`research/discovery-ledger.md`](research/discovery-ledger.md) records family budget,
  verdict, cutoff, and whether a real statistical comparison ran;
- versioned protocol documents freeze eligibility, outcomes, inference, and promotion
  gates before a canonical read;
- feasibility and calibration documents describe data availability or choose bounded
  measurement parameters, not strategy profitability;
- archived JSON and manifests under the ignored backup area are the evidence artifact;
  prose summaries are derived from that artifact, never from a second live run;
- [`research/onchain-intelligence-roadmap.md`](research/onchain-intelligence-roadmap.md)
  is a target plan. It does not authorize wallet attribution or production trading.

## Directory map

- [`architecture/`](architecture/) contains reviewed current or target architecture
  plans. Each document must say which one it is.
- [`adr/`](adr/) preserves durable decisions and supersession history.
- [`contracts/`](contracts/) contains versioned wire, persistence, and delivery rules.
- [`research/`](research/) contains frozen protocols, feasibility studies, and the
  discovery ledger.
- [`runbooks/`](runbooks/) contains production procedures and recovery checks.
- [`strategies/`](strategies/) contains versioned strategy specifications and lifecycle
  rules.
- [`tasks/`](tasks/) contains bounded upstream or external engineering tasks; it is not
  the product roadmap.

## Known drift and bounded follow-ups

| Area                       | Current classification                                                                                                                     | Follow-up                                                                                                               |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------- |
| `ARCHITECTURE.md`          | Partially current. Core services are useful, but the bounded order-flow section is retired and momentum capture is not fully integrated.   | `docs/current-architecture-refresh-v1`: regenerate current service/data-flow diagrams from Compose and entrypoints.     |
| ADR-0004/0005/0006/0008    | Superseded in practice by hosted CI, TanStack Query, changed service-language boundaries, and Hetzner hosting.                             | Record replacement decisions during the architecture refresh; do not rewrite the old rationale.                         |
| Empty scaffold directories | `apps/collectors`, `apps/telegram-bot`, `packages/core`, `packages/exchanges`, and `packages/indicators` are not active services/packages. | Remove or formally activate them in a separate repository-hygiene PR after import/build references are checked.         |
| Retired order-flow runtime | Code, Compose profile, reports, and old docs remain for auditability, but the research verdict is no-go.                                   | Move the operational path to an explicit retired profile/archive only after verifying no active consumer depends on it. |
| Web UI                     | The target structure is reviewed; current pages remain incremental MVP implementations.                                                    | `refactor/web-design-contract-v1`, then one user workflow per PR.                                                       |
| Notification delivery      | Contract v1 exists; migration to one gateway/outbox is incomplete.                                                                         | Follow the staged producer migration in the contract and ROADMAP.                                                       |

## Change rules

1. Update documentation in the same PR when code changes a command, schema, service,
   operational procedure, research contract, or user-visible behavior.
2. Keep one owner for a fact. Link to it instead of copying long configuration tables
   or research rules into multiple READMEs.
3. Put current behavior and target architecture in separate sections or documents.
4. Include a concrete revisit trigger for deferred work. Avoid undated "later" items.
5. Treat Mermaid diagrams as maintained interfaces: node names should match real
   services, stores, and boundaries.
6. Do not place secrets, real hostnames, private IPs, account identifiers, or API keys
   in documentation or command examples.
7. A documentation-only PR stays bounded. Inventory and navigation do not become a
   reason to rewrite services, UI, or research code in the same branch.

## Review checklist

- Links and commands resolve from the repository root.
- Status is explicit and future work is not written in present tense.
- Service names match Compose and executable entrypoints.
- Research claims identify their contract, cutoff, sample, and verdict.
- Operational steps state whether they mutate production and whether backup is part
  of the path.
- Diagrams agree with the accompanying prose.
