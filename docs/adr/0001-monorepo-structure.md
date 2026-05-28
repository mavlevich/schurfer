# ADR-0001: Monorepo structure

Date: 2026-05-08
Status: Accepted

## Context

Need a repo structure for a multi-service trading platform with
different languages (Go, Python, TypeScript).

## Decision

**Monorepo** with `apps/` (runnable services) and `packages/` (shared
libraries) split.

## Alternatives considered

- Polyrepo - separate repos. Rejected: cross-service refactoring
  becomes painful, shared lib versions drift apart.
- Hybrid (public + private + shared) - considered when a public
  product was planned. Rejected when the product became fully
  private (see ADR-0002).

## Consequences

- Pro: atomic changes across multiple services, single CI
- Con: repo grows, need CI that supports partial builds
- Revisit: if repo exceeds 1GB or team grows beyond 5 people
