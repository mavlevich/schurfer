# On-chain intelligence and temporal wallet graph

Status: parked exploratory track

This document records the architecture and evidence gates for a future direct
blockchain scanner. It is intentionally separate from the current pump-reversion
critical path. Building infrastructure is not evidence that wallet activity can be
traded profitably.

## Objective

Build a source-neutral, point-in-time system that can answer:

- which wallets or entity clusters accumulated a token before price moved;
- whether the activity was independent or coordinated through common funding;
- whether the flow reached a liquid, executable market before the public narrative;
- whether later deposits, sales, liquidity removal, or bridge movements warned of an
  exit;
- whether the signal retained positive net expectancy at our actual observation and
  execution latency.

The system may use external RPC endpoints or managed webhooks as transport. The event
contract, decoders, provenance, entity resolution, wallet scoring, graph projection,
signals, and outcome evaluation remain under our control.

## What this is not

- It is not automatic copy trading.
- It is not a promise to reconstruct every blockchain from genesis.
- It is not a full mempool or validator-latency strategy.
- It does not treat a profitable wallet screenshot or Telegram post as point-in-time
  evidence.
- It does not assume that several wallets are independent before funding and entity
  links are checked.

Wallet activity is one feature in a decision model. It is never an order by itself.

## Initial scope

Start with Solana because early DEX and meme-token activity is prominent there and
the pilot can be bounded to watched wallets, programs, pools, and mints. Add one EVM
chain only after the Solana contracts, retry behavior, and resource budget are stable.

The first watchlist should contain 50 to 200 wallets with documented provenance. It
must include negative and ordinary controls, not only wallets selected after a famous
winner. The current 4 GB server is suitable for a watched-wallet pilot and compact
aggregates, not a chain-wide transaction firehose.

## Canonical event envelope

Every normalized event needs:

- `chain`, network, transaction signature or hash, block or slot, and instruction or
  log index;
- `event_type`, schema version, decoder version, source adapter, and raw payload hash;
- `occurred_at`, `published_at` when applicable, `first_observed_at`, `ingested_at`,
  and `finalized_at`;
- finality state, replacement or tombstone reference, and reorg or rollback reason;
- wallet, entity, token or mint, pool, protocol, amounts, decimals, direction, and
  point-in-time USD value;
- source latency, parser status, identity conflicts, and explicit missing fields.

The durable identity key must be derived from chain-native transaction coordinates,
not ticker or token name. Delivery is at least once, so consumers must be idempotent.
Reconnect backfill and finality updates are part of correctness, not later cleanup.

## Chain adapters

### Solana pilot

Observe selected accounts and programs through RPC/WebSocket subscriptions, then
recover complete transactions for durable decoding. Normalize:

- signers and fee payer;
- outer and inner instructions;
- SPL token and SOL balance deltas;
- token transfers, swaps, routes, pools, and mints;
- liquidity additions and removals;
- deployer and authority activity;
- failed transactions and finality transitions.

Do not infer a swap only from a token balance increase. Aggregators, routers, wrapped
assets, rent changes, and multi-hop routes require transaction-level decoding.

### Later EVM adapter

Use logs, receipts, traces where available, and block subscriptions to normalize:

- ERC-20 transfers;
- pool swaps and liquidity changes;
- contract deployment and ownership changes;
- bridge, CEX, and protocol flows;
- removed logs and replacement blocks after reorgs.

An external node provider reduces operations work but does not remove the need for
deduplication, gap detection, historical recovery, rate-limit handling, and reorg
semantics.

## Temporal graph

Represent the normalized data as a directed temporal multigraph.

Candidate node types:

- wallet or account;
- inferred entity;
- token or mint;
- pool;
- protocol or router;
- deployer;
- bridge;
- known CEX deposit or withdrawal cluster.

Candidate edge types:

- `TRANSFERRED`;
- `SWAPPED`;
- `FUNDED`;
- `DEPOSITED_TO_CEX`;
- `WITHDREW_FROM_CEX`;
- `ADDED_LIQUIDITY`;
- `REMOVED_LIQUIDITY`;
- `DEPLOYED`;
- `CO_BOUGHT_WITH`.

Every inferred entity or relationship needs evidence, confidence, provenance, and a
validity interval. Labels must be reconstructible as they were known at decision
time. A label learned next week cannot be injected into yesterday's replay.

Start with PostgreSQL normalized tables and offline graph projections in Python.
Evaluate NetworkX, graph-tool, or a columnar projection for research. Introduce Neo4j,
Memgraph, or another graph database only after a concrete query, latency, or scale
requirement cannot be met by the simpler model.

## Point-in-time wallet score

A wallet score can use only history available before the candidate event:

- realized P&L and return distribution;
- sample size and independent-token count;
- hit rate at fixed registered horizons;
- drawdown and loss-tail behavior;
- entry lead time relative to price and public announcements;
- holding-time distribution and exit discipline;
- concentration by token, deployer, protocol, and counterparty;
- wallet age and activity continuity;
- common funder, sybil, insider, and self-trading risk;
- source, decoder, and entity-label confidence.

Unrealized holdings are not realized profit. Thin-liquidity mark-to-market values must
be discounted by executable exit capacity. A wallet that bought one extreme winner is
not automatically smart money.

## Candidate signals

The first research family may include:

1. coordinated accumulation by previously scored independent wallets before price
   acceleration;
2. early DEX buy flow with rising unique buyers and stable or growing executable
   liquidity;
3. smart exits, CEX deposits, or cluster distribution after a public pump;
4. liquidity removal or authority changes that increase failed-exit risk;
5. recurring deployer, funder, pool, and wallet clusters across launches.

Every signal snapshot must also record:

- price already moved since first observable activity;
- liquidity, spread, executable quote, and estimated impact;
- unique wallet count before and after entity clustering;
- concentration in the largest wallet and entity;
- observation latency and chain finality;
- whether a CEX market or perpetual already exists;
- missing decoders, labels, prices, or liquidity data.

## Outcomes and evidence gate

Resolve point-in-time outcomes at 1 minute, 5 minutes, 15 minutes, 1 hour, 4 hours,
and 24 hours. For each candidate, retain:

- executable net return after fees and estimated impact;
- MFE, MAE, time to peak, and liquidity drawdown;
- rug, sell-failure, or route-unavailable status;
- signal-to-observation and observation-to-decision latency;
- edge decay under delayed entry;
- cluster and token concentration.

Run shadow alerts before paper execution. Promotion requires an untouched forward
cohort, enough independent wallets, entities, and tokens, stable results across
calendar weeks, and positive expected net return after adverse selection and failed
exit costs. Telegram-channel messages can be a latency benchmark or enrichment
source, but never the primary timestamp or trading trigger.

## Delivery stages

1. Define and test the finality-aware event envelope, idempotency key, gap detector,
   reconnect recovery, and tombstone semantics.
2. Build a Solana watched-wallet backfill and live collector.
3. Add SPL transfer and bounded DEX swap decoding with golden transaction fixtures.
4. Persist normalized events and build the first temporal graph projection.
5. Add evidence-based entity resolution and point-in-time wallet scoring.
6. Register one small signal family and forward-outcome resolver.
7. Publish shadow alerts with latency, liquidity, and concentration diagnostics.
8. Evaluate wider collection, another chain, or paper execution only after the gate.

This track must not displace the two-lane research budget in `ROADMAP.md`. A new
collector consumes an evidence-producing pull request and needs an explicit lane slot.

## Resource gates

- Use NATS for transport and PostgreSQL for the bounded pilot.
- Store compact normalized events and aggregates, not an unbounded raw firehose.
- Move broad history to object storage or ClickHouse only after retained bytes per
  hour, query demand, and replay value are measured.
- Add a graph database only after the temporal graph queries justify it.
- Operate our own archival or validator nodes only when measured RPC cost, missing
  history, latency, or reliability makes them cheaper than managed transport.
- Upgrade or split the host before broad raw capture if memory, consumer lag, dropped
  events, database batch latency, or retained storage crosses a registered limit.

## Public and private boundary

Potential public open-source components:

- chain-neutral event schemas and finality contracts;
- Solana and EVM decoders with public fixtures;
- adapter conformance tests and gap diagnostics;
- temporal graph projection tools;
- deterministic export and replay manifests;
- a delayed read-only explorer over permitted public data.

Private Schurfer components:

- curated wallet and entity lists;
- non-public or licensed labels and raw datasets;
- point-in-time scores and feature weights;
- signal thresholds, decisions, positions, and execution logic;
- account data, credentials, and production observations.

If the generic contracts survive production and one reproducible event study, assess
whether they extend the public market-events project described by
[ADR-0009](../adr/0009-separate-public-market-events-project.md) or belong in a
separate sibling repository. Do not create either repository before that evidence.

## Useful primary references

- [Solana `getTransaction`](https://solana.com/docs/rpc/http/gettransaction)
- [Ethereum JSON-RPC and `eth_getLogs`](https://ethereum.org/developers/docs/apis/json-rpc/)
- [Geth publish/subscribe and removed-log behavior](https://geth.ethereum.org/docs/interacting-with-geth/rpc/pubsub)
- [Helius webhooks](https://www.helius.dev/docs/webhooks)
- [Alchemy address activity webhooks](https://www.alchemy.com/docs/reference/address-activity-webhook)
