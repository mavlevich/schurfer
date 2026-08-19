# Architecture decision records

Status: current ADR index.

ADRs preserve why a consequential decision was made. Accepted records are not edited
to make history resemble the current system. When a decision changes, mark the old
record `Superseded`, link the replacement, and add a new ADR. For drift that already
predates this index, the affected records link current runtime evidence and the
bounded replacement-ADR follow-up instead of pretending the old decision remains
active.

| ADR                                                   | Status     | Subject                               |
| ----------------------------------------------------- | ---------- | ------------------------------------- |
| [0001](0001-monorepo-structure.md)                    | Accepted   | Monorepo structure                    |
| [0002](0002-private-product-only.md)                  | Accepted   | Single private product                |
| [0003](0003-go-workspaces.md)                         | Accepted   | Go workspaces                         |
| [0004](0004-self-hosted-ci.md)                        | Superseded | Original self-hosted CI choice        |
| [0005](0005-frontend-stack.md)                        | Superseded | Original Redux-based frontend stack   |
| [0006](0006-backend-languages.md)                     | Superseded | Original service-language allocation  |
| [0007](0007-trade-journal-first.md)                   | Accepted   | Journal-first measurement             |
| [0008](0008-aws-frankfurt-hosting.md)                 | Superseded | Original AWS hosting choice           |
| [0009](0009-separate-public-market-events-project.md) | Proposed   | Separate public market-events project |

The repository currently uses GitHub-hosted CI, TanStack Query instead of Redux, a
Python execution service, a Go notifier, and a Hetzner production host. Replacement
ADRs are intentionally deferred to `docs/current-architecture-refresh-v1`, where the
current boundaries can be recorded together instead of manufacturing several tiny
retroactive decisions in this indexing PR.
