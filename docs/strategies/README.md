# Trading strategies

Каждая стратегия — один markdown файл.

## Лайфцикл стратегии

1. **draft** — идея, неформальное описание (текущий уровень
   pump_short_v1)
2. **paper** — формализована, торгуется в paper-mode
3. **shadow** — paper рядом с real markets, логируется но не
   исполняется в live
4. **live_micro** — live trading с минимальными размерами
5. **live** — полный размер
6. **deprecated** — отключена

## Naming

`{strategy_type}_v{N}.md` — pump_short_v1, funding_arb_v1, etc.
Major изменения в правилах — bump версии (v2).

## Формат

См. `pump_short_v1.md` как пример.
