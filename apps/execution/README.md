# execution

Python/FastAPI service for exchange account management and order execution.
Uses isolated ccxt client scopes:

- public market clients cover the scanner's 17 venues during `DRY_RUN`;
- authenticated trading clients exist only for exchanges with complete API credentials.

Account, position, and order paths never receive public-only clients.

## Endpoints

- `GET /balance` - USDT balance across all configured exchanges
- `GET /positions` - open positions across all exchanges
- `GET /risk` - current slot usage, daily P&L, limits
- `POST /order` - place order (runs pre-trade risk checks first)
- `POST /stop` - emergency stop (sets `trading:enabled=0` in Redis)
- `POST /resume` - re-enable trading

## Configuration

All settings use environment variables. API keys activate authenticated trading
clients; they are not required for public dry-run measurement.

```
REDIS_ADDR=localhost:6379

BINANCE_API_KEY=
BINANCE_API_SECRET=
BYBIT_API_KEY=
BYBIT_API_SECRET=
OKX_API_KEY=
OKX_API_SECRET=
OKX_PASSPHRASE=
GATE_API_KEY=
GATE_API_SECRET=
KUCOIN_API_KEY=
KUCOIN_API_SECRET=
KUCOIN_PASSPHRASE=
BINGX_API_KEY=
BINGX_API_SECRET=
MEXC_API_KEY=
MEXC_API_SECRET=

MAX_POSITIONS=5
MAX_POSITION_USD=500
DAILY_LOSS_LIMIT_USD=200
```

## Pre-trade checks (in order)

1. `trading:enabled` Redis flag - emergency stop
2. Daily loss limit not breached
3. Max open positions cap
4. No existing position in same token
5. Sufficient free margin on target exchange
