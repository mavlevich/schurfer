package health

import (
	"context"
	"log/slog"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/nats-io/nats.go"
	"github.com/redis/go-redis/v9"
)

type Status string

const (
	StatusUp      Status = "up"
	StatusDown    Status = "down"
	StatusUnknown Status = "unknown"
)

type Report struct {
	Postgres    Status `json:"postgres"`
	Redis       Status `json:"redis"`
	NATS        Status `json:"nats"`
	Collector   Status `json:"collector"`
	Execution   Status `json:"execution"`
	TelegramBot Status `json:"telegram_bot"`
}

type Config struct {
	PostgresDSN string
	RedisAddr   string
	NATSUrl     string
}

// Checker holds shared clients and pings them on each check.
// Call Close when the application shuts down.
type Checker struct {
	pool *pgxpool.Pool
	rdb  *redis.Client
	nc   *nats.Conn
}

func NewChecker(ctx context.Context, cfg Config) (*Checker, error) {
	pool, err := pgxpool.New(ctx, cfg.PostgresDSN)
	if err != nil {
		return nil, err
	}

	rdb := redis.NewClient(&redis.Options{Addr: cfg.RedisAddr})

	// RetryOnFailedConnect: if NATS is down at startup, Connect returns a
	// valid conn that keeps retrying in the background, so nc is never nil.
	// Without this option, a failed initial connect is permanent (no retry).
	nc, err := nats.Connect(cfg.NATSUrl,
		nats.MaxReconnects(-1), // reconnect indefinitely after initial connect
		nats.ReconnectWait(4*time.Second),
		nats.RetryOnFailedConnect(true),
	)
	if err != nil {
		// Only happens on invalid URL or client-side config error.
		slog.Warn("NATS: client creation failed", "err", err)
		nc = nil
	}

	return &Checker{pool: pool, rdb: rdb, nc: nc}, nil
}

func (c *Checker) Pool() *pgxpool.Pool { return c.pool }

func (c *Checker) Close() {
	c.pool.Close()
	_ = c.rdb.Close()
	if c.nc != nil {
		c.nc.Close()
	}
}

func (c *Checker) Check(ctx context.Context) Report {
	return Report{
		Postgres:    c.checkPostgres(ctx),
		Redis:       c.checkRedis(ctx),
		NATS:        c.checkNATS(),
		Collector:   StatusUnknown, // populated via NATS heartbeats later
		Execution:   StatusUnknown,
		TelegramBot: c.checkTelegramBot(ctx),
	}
}

func (c *Checker) checkTelegramBot(ctx context.Context) Status {
	ctx, cancel := context.WithTimeout(ctx, 2*time.Second)
	defer cancel()
	exists, err := c.rdb.Exists(ctx, "notifier:heartbeat").Result()
	if err != nil || exists == 0 {
		return StatusDown
	}
	return StatusUp
}

func (c *Checker) checkPostgres(ctx context.Context) Status {
	ctx, cancel := context.WithTimeout(ctx, 4*time.Second)
	defer cancel()
	if err := c.pool.Ping(ctx); err != nil {
		return StatusDown
	}
	return StatusUp
}

func (c *Checker) checkRedis(ctx context.Context) Status {
	ctx, cancel := context.WithTimeout(ctx, 4*time.Second)
	defer cancel()
	if err := c.rdb.Ping(ctx).Err(); err != nil {
		return StatusDown
	}
	return StatusUp
}

func (c *Checker) checkNATS() Status {
	if c.nc == nil || !c.nc.IsConnected() {
		return StatusDown
	}
	return StatusUp
}
