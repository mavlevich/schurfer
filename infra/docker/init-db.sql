-- Enable TimescaleDB extension
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- Schema for time-series data (OHLCV, ticks, funding, OI)
CREATE SCHEMA IF NOT EXISTS timeseries;

-- Schema for application data (journal, orders, positions, configs)
CREATE SCHEMA IF NOT EXISTS app;

-- All application tables (strategies, trades, trade_decisions, etc.) are managed by
-- Alembic migrations in packages/journal/migrations/. Run `make migrate` after `make dev`.
