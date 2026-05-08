# Strategy: pump_short_v1

Status: draft (formalization in progress)
Author: mavlevich
Created: 2026-05-08

## Hypothesis

Низколиквидные токены, запампленные на 50-100%+ за короткий
период (часы - сутки) с признаками exhaustion (близость к peak'у,
рост OI, экстремальный funding) часто откатываются к pre-pump
уровню в течение дней - недель.

## Trigger conditions

- `price_change_24h > 50%` AND `< 130%` (типичный диапазон)
- Цена держится near top - recent peak в последние ~6 часов
- Symbol доступен на perp хотя бы на одной из бирж в твоей юрисдикции

## Entry rules

- Open SHORT на perp
- Размер позиции: пока вручную выбираешь "психологически приемлемую сумму"
  → TODO: заменить на % от капитала с risk-based sizing
- Плечо: до 10x исторически использовалось при широком стопе

## Stop loss

- Текущий подход: "большой стоп, маржи хватает"
- Implicit stop ~+200% от entry (на широких плечах)
- TODO: формализовать на технический уровень
  (например: above recent ATH +15-20%)

## Exit rules (take profit)

- Цена откат к pre-pump уровню (≈ цена за 24-48h до начала pump'а)
- Решение "по ощущениям", по интуиции
- TODO: формализовать через `target_price = price_t-48h × 1.05`

## Position management

- Текущий: одно entry, manual exit
- TODO: рассмотреть scaled entry в 2-3 транша

## Risk management gaps (для следующей итерации)

1. **Risk per trade в % от капитала** - сейчас не задано
2. **Funding rate filter** - не учитывается до входа
   (важно: на pumped токенах часто extreme funding,
   может съесть профит за дни holding)
3. **OI как trigger condition** - не используется,
   но даёт high-confidence сигналы
4. **Stop loss formalization** - заменить "большой стоп"
   на технический уровень
5. **Exit formalization** - pre-pump price как конкретное число

## Historical performance (paper-tracked)

- Pre-Schurfer: успешные сделки по интуиции, чёткая статистика
  не вёлась
- TODO: восстановить ~10 последних трейдов из памяти/CSV для
  baseline winrate

## Refinement TODO

- [ ] Backtest на исторических pumps Q1 2026 (M, MEGA, RAVE,
      SIREN, KAT, SPK style setups)
- [ ] Определить optimal price_change_24h thresholds
- [ ] Funding rate as trigger / filter
- [ ] OI growth as confidence multiplier
- [ ] Position sizing formula (risk-based)
- [ ] Stop loss rule based on technical levels
- [ ] Exit price target formula
