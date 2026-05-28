# ADR-0002: Single private product, no public component

Date: 2026-05-08
Status: Accepted

## Context

Originally considered splitting into a public analytics product
(Schurfer dashboard) and a private auto-trading engine.

## Decision

**One private product.** Web UI behind login, owner-only access.
No public components.

## Rationale

1. A public component trading third-party funds requires CASP
   licensing under MiCA. Not doing that.
2. Public analytics without trading is a distraction from the
   core product.
3. One deployment, one auth, one backup strategy - simpler.
4. If monetization is desired later, can open a subscription
   for signals (informational product, not a financial service).

## Consequences

- Pro: focus, simplicity, no licensing requirements
- Con: no portfolio effect (use other projects for CV)
- Revisit: if trading income stabilizes and there is desire to
  build a SaaS, review the legal structure
