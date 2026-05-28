# ADR-0003: Go workspaces for backend services

Date: 2026-05-08
Status: Accepted

## Context

Multiple Go services in one repo: collectors, execution, api-gateway.
Need a model for managing Go modules.

## Decision

**Go workspaces** (`go.work`). Each service is a separate module.

## Alternatives considered

- Single go.mod for the whole repo - simpler, but dependency
  conflicts between services. Hard to extract a single service
  into its own repo.

## Consequences

- Pro: dependency isolation, modularity
- Pro: each service can be released independently
- Con: slightly more boilerplate (go.mod in each directory)
