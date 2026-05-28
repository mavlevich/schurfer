# ADR-0008: AWS EC2 Frankfurt для production hosting

Date: 2026-05-28
Status: Accepted

## Context

Нужен production сервер. Требования:
- Не супер дорогой (~$20-30/мес)
- Популярный провайдер для прокачки skills (AWS)
- IP не блокируется биржами
- Достаточная производительность для 6 сервисов + databases

## Decision

**AWS EC2 t4g.medium (ARM/Graviton)** в **Frankfurt (eu-central-1)**.

- Docker Compose для начала, миграция на ECS когда продукт стабилен
- ARM образы для всех сервисов
- Spot instance для CI runner

## Alternatives considered

- Hetzner Singapore (€7-15/мес) - дешевле, но AWS skills ценнее
- AWS Singapore (ap-southeast-1) - дороже, нужен только для HFT latency
- AWS EKS - +$73/мес за control plane, overkill для соло
- DigitalOcean - средний вариант, но AWS доминирует на рынке

## Why Frankfurt, not Singapore

Стратегия pump_short - позиции держатся дни-недели. Latency
150-200ms до бирж (Frankfurt -> Singapore) несущественна.
Frankfurt - ближайший к Польше AWS регион, один из самых дешёвых.

## Cost breakdown

- EC2 t4g.medium: ~$24/мес (on-demand), ~$15/мес (reserved 1yr)
- Spot instance CI: ~$3-5/мес
- Cloudflare Tunnel: бесплатно
- Tailscale: бесплатно
- Total: ~$20-30/мес

## Consequences

- Pro: AWS skills (IAM, VPC, CloudWatch, ECR, ECS path)
- Pro: Европейский IP, нет блоков бирж
- Pro: ARM - дешевле и быстрее чем x86
- Con: дороже Hetzner (x3-4), но приемлемо
- Con: ARM requires multi-arch Docker builds
- Revisit: если latency станет критичной (HFT/арбитраж) - добавить
  Singapore node для hot path
