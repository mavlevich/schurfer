# collectors

Status: inactive legacy scaffold.

The active Go collector module is [`apps/collector`](../collector/). Do not add new
exchange adapters here. Removal of this directory is deferred to a bounded repository
hygiene PR after build and import references are checked.

Go services that maintain WebSocket subscriptions to exchanges
and publish normalized events to NATS.

One sub-package per exchange (binance/, bybit/, okx/, hyperliquid/).

Historical note: this planned directory structure was never activated.
