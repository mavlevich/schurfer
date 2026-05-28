# Strategy: pump_short_v1

Status: draft (formalization in progress)
Author: mavlevich
Created: 2026-05-08

## Hypothesis

Low-liquidity tokens pumped 50-100%+ in a short period (hours to days)
with exhaustion signs (near peak, rising OI, extreme funding) tend to
retrace to pre-pump levels within days to weeks.

## Trigger conditions

- `price_change_24h > 50%` AND `< 130%` (typical range)
- Price is holding near the top - recent peak in the last ~6 hours
- Symbol is available on perps on at least one supported exchange

## Entry rules

- Open SHORT on perps
- Position size: manually chosen "psychologically acceptable amount"
  for now -> TODO: replace with % of capital using risk-based sizing
- Leverage: up to 10x historically used with wide stop

## Stop loss

- Current approach: "wide stop, enough margin"
- Implicit stop ~+200% from entry (on high leverage)
- TODO: formalize to a technical level
  (e.g. above recent ATH +15-20%)

## Exit rules (take profit)

- Price retraces to pre-pump level (approx. price 24-48h before pump start)
- Decision based on feel/intuition
- TODO: formalize via `target_price = price_t-48h * 1.05`

## Position management

- Current: single entry, manual exit
- TODO: consider scaled entry in 2-3 tranches

## Risk management gaps (for next iteration)

1. **Risk per trade as % of capital** - not defined yet
2. **Funding rate filter** - not checked before entry
   (important: pumped tokens often have extreme funding,
   can eat profit over days of holding)
3. **OI as trigger condition** - not used yet,
   but provides high-confidence signals
4. **Stop loss formalization** - replace "wide stop"
   with a technical level
5. **Exit formalization** - pre-pump price as a concrete number

## Historical performance (paper-tracked)

- Pre-Schurfer: successful trades based on intuition, no clear
  statistics were kept
- TODO: reconstruct ~10 recent trades from memory/CSV for
  baseline winrate

## Refinement TODO

- [ ] Backtest on historical pumps Q1 2026 (M, MEGA, RAVE,
      SIREN, KAT, SPK style setups)
- [ ] Determine optimal price_change_24h thresholds
- [ ] Funding rate as trigger / filter
- [ ] OI growth as confidence multiplier
- [ ] Position sizing formula (risk-based)
- [ ] Stop loss rule based on technical levels
- [ ] Exit price target formula
