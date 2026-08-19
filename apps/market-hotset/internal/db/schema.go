package db

import (
	"context"
	"fmt"

	"github.com/jackc/pgx/v5/pgxpool"
)

func EnsureSchema(ctx context.Context, pool *pgxpool.Pool) error {
	query := `
CREATE SCHEMA IF NOT EXISTS app;

CREATE TABLE IF NOT EXISTS app.live_long_short_ratio (
	ts TIMESTAMPTZ NOT NULL,
	base TEXT NOT NULL,
	exchange TEXT NOT NULL,
	ratio NUMERIC NOT NULL,
	long_account NUMERIC,
	short_account NUMERIC,
	PRIMARY KEY (exchange, base, ts)
);
`
	_, err := pool.Exec(ctx, query)
	if err != nil {
		return fmt.Errorf("create table: %w", err)
	}

	// Create hypertable if it doesn't exist (TimescaleDB)
	hypertableQuery := `
DO $$
BEGIN
	IF NOT EXISTS (
		SELECT 1
		FROM timescaledb_information.hypertables
		WHERE hypertable_schema = 'app' AND hypertable_name = 'live_long_short_ratio'
	) THEN
		PERFORM create_hypertable('app.live_long_short_ratio', 'ts');
	END IF;
END $$;
`
	_, err = pool.Exec(ctx, hypertableQuery)
	if err != nil {
		return fmt.Errorf("create hypertable: %w", err)
	}

	return nil
}
