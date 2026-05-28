# ADR-0004: Self-hosted GitHub Actions runner

Date: 2026-05-08
Status: Accepted

## Context

GitHub Free gives 2000 Actions minutes/month for private repos.
With active development (3-5 pushes/day x 10-15 min CI) it's easy
to hit the limit.

## Decision

**Self-hosted runner** on the production VPS (AWS EC2 spot instance).
Free, no minute limits.

## Alternatives considered

- GitHub-hosted (paid upgrade) - $4/mo minimum for Pro
- Forgejo Actions - separate admin, more complexity
- Mix (lint hosted, heavy self-hosted) - may be useful later

## Consequences

- Pro: $0 cost, no limits
- Pro: cache is local on runner, builds are faster
- Con: maintain the runner ourselves, handle security
- Revisit: if runner overhead exceeds the benefit
