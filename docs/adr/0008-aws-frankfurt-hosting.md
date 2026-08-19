# ADR-0008: AWS EC2 Frankfurt for production hosting

Date: 2026-05-28
Status: Superseded

Supersession note: production now runs on Hetzner. The current operational facts are
documented in [`docs/runbooks/README.md`](../runbooks/README.md). A replacement hosting
ADR will be recorded during `docs/current-architecture-refresh-v1`; the original
decision below is retained unchanged as history.

## Context

Need a production server. Requirements:

- Not too expensive (~$20-30/mo)
- Popular cloud provider for skill building (AWS)
- IP not blocked by exchanges
- Enough performance for 6 services + databases

## Decision

**AWS EC2 t4g.medium (ARM/Graviton)** in **Frankfurt (eu-central-1)**.

- Docker Compose initially, migrate to ECS when product is stable
- ARM images for all services
- Spot instance for CI runner

## Alternatives considered

- Hetzner Singapore (7-15 EUR/mo) - cheaper, but AWS skills are more valuable
- AWS Singapore (ap-southeast-1) - more expensive, only needed for HFT latency
- AWS EKS - +$73/mo for control plane, overkill for solo project
- DigitalOcean - middle ground, but AWS dominates the market

## Why Frankfurt, not Singapore

The pump_short strategy holds positions for days to weeks. 150-200ms
latency to exchanges (Frankfurt to Singapore) is irrelevant.
Frankfurt is the closest AWS region to Poland and one of the cheapest.

## Cost breakdown

- EC2 t4g.medium: ~$24/mo (on-demand), ~$15/mo (reserved 1yr)
- Spot instance CI: ~$3-5/mo
- Cloudflare Tunnel: free
- Tailscale: free
- Total: ~$20-30/mo

## Consequences

- Pro: AWS skills (IAM, VPC, CloudWatch, ECR, ECS path)
- Pro: European IP, no exchange blocks
- Pro: ARM is cheaper and faster than x86
- Con: more expensive than Hetzner (3-4x), but acceptable
- Con: ARM requires multi-arch Docker builds
- Revisit: if latency becomes critical (HFT/arbitrage), add a
  Singapore node for hot path only
