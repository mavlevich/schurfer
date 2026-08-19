package health

import (
	"context"
	"encoding/json"
	"log/slog"
	"math"
	"strconv"
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
	Postgres         Status            `json:"postgres"`
	Redis            Status            `json:"redis"`
	NATS             Status            `json:"nats"`
	Collector        Status            `json:"collector"`
	Execution        Status            `json:"execution"`
	TelegramBot      Status            `json:"telegram_bot"`
	SignalReadiness  *SignalReadiness  `json:"signal_readiness"`
	SystemLoad       *SystemLoad       `json:"system_load"`
	ContainerRuntime *ContainerRuntime `json:"container_runtime"`
	DiskUsage        *DiskUsage        `json:"disk_usage"`
	MarketPipeline   *MarketPipeline   `json:"market_pipeline"`
	OrderflowPilot   *OrderflowPilot   `json:"orderflow_pilot"`
	FillIncidents    *FillIncidents    `json:"fill_incidents"`
}

type SignalReadiness struct {
	UpdatedAtMS int64            `json:"updated_at_ms"`
	PumpCount   int64            `json:"pump_count"`
	Evaluated   int64            `json:"evaluated"`
	Ready       int64            `json:"ready"`
	Deferred    int64            `json:"deferred"`
	Reasons     map[string]int64 `json:"reasons"`
}

type MarketPipeline struct {
	UpdatedAtMS        int64   `json:"updated_at_ms"`
	ObservedSymbols    int64   `json:"observed_symbols"`
	HotSymbols         int64   `json:"hot_symbols"`
	EventRatePerSecond float64 `json:"event_rate_per_sec"`
	LastLagMS          int64   `json:"last_lag_ms"`
	// MaxLagMS is the lifetime maximum since the collector started: a single
	// historical outlier stays in this field forever and must never be shown
	// as if it were a current condition. WindowMaxLagMS is the collector's
	// own rolling-window maximum and is what "is lag currently a problem?"
	// should be judged against.
	MaxLagMS            int64  `json:"max_lag_ms"`
	WindowMaxLagMS      int64  `json:"window_max_lag_ms"`
	NATSDroppedTotal    int64  `json:"nats_dropped_total"`
	PendingDroppedTotal int64  `json:"pending_dropped_total"`
	PersistErrorsTotal  int64  `json:"persist_errors_total"`
	BarsPersistedTotal  int64  `json:"bars_persisted_total"`
	PumpFeedStatus      string `json:"pump_feed_status"`
}

type OrderflowPilot struct {
	UpdatedAtMS         int64   `json:"updated_at_ms"`
	StartedAtMS         int64   `json:"started_at_ms"`
	Status              string  `json:"status"`
	ObservedSymbols     int64   `json:"observed_symbols"`
	EventRatePerSecond  float64 `json:"event_rate_per_sec"`
	ActiveCaptures      int64   `json:"active_captures"`
	ActivationTotal     int64   `json:"activation_total"`
	RecordsPersisted    int64   `json:"records_persisted_total"`
	StorageBytes        int64   `json:"storage_bytes"`
	StorageBytesPerDay  float64 `json:"storage_bytes_per_day"`
	LastLagMS           int64   `json:"last_lag_ms"`
	WindowMaxLagMS      int64   `json:"window_max_lag_ms"`
	QueueDroppedTotal   int64   `json:"queue_dropped_total"`
	PendingDroppedTotal int64   `json:"pending_dropped_total"`
	PersistErrorsTotal  int64   `json:"persist_errors_total"`
	StorageLimitedTotal int64   `json:"storage_limited_total"`
	LeftCensoredTotal   int64   `json:"left_censored_total"`
	CapacityRejected    int64   `json:"capacity_rejected_total"`
}

type Config struct {
	PostgresDSN        string
	RedisAddr          string
	NATSUrl            string
	RuntimeMetricsPath string
	DiskUsagePath      string
}

// Checker holds shared clients and pings them on each check.
// Call Close when the application shuts down.
type Checker struct {
	pool           *pgxpool.Pool
	db             queryRower
	rdb            *redis.Client
	nc             *nats.Conn
	systemProbe    func() *SystemLoad
	runtimeProbe   func() *ContainerRuntime
	diskUsageProbe func() *DiskUsage
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

	systemProbe := newSystemProbe("/proc", "/")
	return &Checker{
		pool:        pool,
		db:          &poolAdapter{inner: pool},
		rdb:         rdb,
		nc:          nc,
		systemProbe: systemProbe,
		runtimeProbe: func() *ContainerRuntime {
			return readContainerRuntime(cfg.RuntimeMetricsPath)
		},
		diskUsageProbe: func() *DiskUsage {
			return readDiskUsage(cfg.DiskUsagePath)
		},
	}, nil
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
	var systemLoad *SystemLoad
	if c.systemProbe != nil {
		systemLoad = c.systemProbe()
	}
	var containerRuntime *ContainerRuntime
	if c.runtimeProbe != nil {
		containerRuntime = c.runtimeProbe()
	}
	var diskUsage *DiskUsage
	if c.diskUsageProbe != nil {
		diskUsage = c.diskUsageProbe()
	}
	// Collector and Execution have no dedicated heartbeat key of their own;
	// each is instead inferred from telemetry only that service writes, on a
	// short Redis TTL the service itself refreshes (30s for market:hotset:health,
	// 180s for execution:signal_readiness). Presence therefore already means
	// "wrote fresh telemetry within its own TTL window", so there is no
	// separate staleness case to handle here: absence (nil) already covers it.
	marketPipeline := c.checkMarketPipeline(ctx)
	signalReadiness := c.checkSignalReadiness(ctx)
	return Report{
		Postgres:         c.checkPostgres(ctx),
		Redis:            c.checkRedis(ctx),
		NATS:             c.checkNATS(),
		Collector:        statusFromPresence(marketPipeline != nil),
		Execution:        statusFromPresence(signalReadiness != nil),
		TelegramBot:      c.checkTelegramBot(ctx),
		SignalReadiness:  signalReadiness,
		SystemLoad:       systemLoad,
		ContainerRuntime: containerRuntime,
		DiskUsage:        diskUsage,
		MarketPipeline:   marketPipeline,
		OrderflowPilot:   c.checkOrderflowPilot(ctx),
		FillIncidents:    c.checkFillIncidents(ctx),
	}
}

func statusFromPresence(present bool) Status {
	if present {
		return StatusUp
	}
	return StatusDown
}

func (c *Checker) checkOrderflowPilot(ctx context.Context) *OrderflowPilot {
	ctx, cancel := context.WithTimeout(ctx, 2*time.Second)
	defer cancel()

	values, err := c.rdb.HGetAll(ctx, "market:orderflow:health").Result()
	if err != nil || len(values) == 0 {
		return nil
	}
	updatedAt, ok := parseInt64(values, "updated_at_ms")
	if !ok {
		return nil
	}
	startedAt, ok := parseInt64(values, "started_at_ms")
	if !ok {
		return nil
	}
	observedSymbols, ok := parseInt64(values, "observed_symbols")
	if !ok {
		return nil
	}
	eventRate, ok := parseFloat64(values, "event_rate_per_sec")
	if !ok {
		return nil
	}
	storageBytesPerDay, ok := parseFloat64(values, "storage_bytes_per_day")
	if !ok {
		return nil
	}
	status := values["status"]
	if status == "" {
		return nil
	}

	return &OrderflowPilot{
		UpdatedAtMS:         updatedAt,
		StartedAtMS:         startedAt,
		Status:              status,
		ObservedSymbols:     observedSymbols,
		EventRatePerSecond:  eventRate,
		ActiveCaptures:      optionalInt64(values, "active_captures"),
		ActivationTotal:     optionalInt64(values, "activation_total"),
		RecordsPersisted:    optionalInt64(values, "records_persisted_total"),
		StorageBytes:        optionalInt64(values, "storage_bytes"),
		StorageBytesPerDay:  storageBytesPerDay,
		LastLagMS:           optionalInt64(values, "last_lag_ms"),
		WindowMaxLagMS:      optionalInt64(values, "window_max_lag_ms"),
		QueueDroppedTotal:   optionalInt64(values, "queue_dropped_total"),
		PendingDroppedTotal: optionalInt64(values, "pending_dropped_total"),
		PersistErrorsTotal:  optionalInt64(values, "persist_errors_total"),
		StorageLimitedTotal: optionalInt64(values, "storage_limited_total"),
		LeftCensoredTotal:   optionalInt64(values, "left_censored_total"),
		CapacityRejected:    optionalInt64(values, "capacity_rejected_total"),
	}
}

func (c *Checker) checkMarketPipeline(ctx context.Context) *MarketPipeline {
	ctx, cancel := context.WithTimeout(ctx, 2*time.Second)
	defer cancel()

	values, err := c.rdb.HGetAll(ctx, "market:hotset:health").Result()
	if err != nil || len(values) == 0 {
		return nil
	}
	updatedAt, ok := parseInt64(values, "updated_at_ms")
	if !ok {
		return nil
	}
	observedSymbols, ok := parseInt64(values, "observed_symbols")
	if !ok {
		return nil
	}
	eventRate, ok := parseFloat64(values, "event_rate_per_sec")
	if !ok {
		return nil
	}

	return &MarketPipeline{
		UpdatedAtMS:         updatedAt,
		ObservedSymbols:     observedSymbols,
		HotSymbols:          optionalInt64(values, "hot_symbols"),
		EventRatePerSecond:  eventRate,
		LastLagMS:           optionalInt64(values, "last_lag_ms"),
		MaxLagMS:            optionalInt64(values, "max_lag_ms"),
		WindowMaxLagMS:      optionalInt64(values, "window_max_lag_ms"),
		NATSDroppedTotal:    optionalInt64(values, "nats_dropped_total"),
		PendingDroppedTotal: optionalInt64(values, "pending_dropped_total"),
		PersistErrorsTotal:  optionalInt64(values, "persist_errors_total"),
		BarsPersistedTotal:  optionalInt64(values, "bars_persisted_total"),
		PumpFeedStatus:      values["pump_feed_status"],
	}
}

func (c *Checker) checkSignalReadiness(ctx context.Context) *SignalReadiness {
	ctx, cancel := context.WithTimeout(ctx, 2*time.Second)
	defer cancel()

	values, err := c.rdb.HGetAll(ctx, "execution:signal_readiness").Result()
	if err != nil || len(values) == 0 {
		return nil
	}

	updatedAt, ok := parseInt64(values, "updated_at_ms")
	if !ok {
		return nil
	}
	pumpCount, ok := parseInt64(values, "pump_count")
	if !ok {
		return nil
	}
	evaluated, ok := parseInt64(values, "evaluated")
	if !ok {
		return nil
	}
	ready, ok := parseInt64(values, "ready")
	if !ok {
		return nil
	}
	deferred, ok := parseInt64(values, "deferred")
	if !ok {
		return nil
	}

	reasons := make(map[string]int64)
	if err := json.Unmarshal([]byte(values["reasons"]), &reasons); err != nil {
		return nil
	}

	return &SignalReadiness{
		UpdatedAtMS: updatedAt,
		PumpCount:   pumpCount,
		Evaluated:   evaluated,
		Ready:       ready,
		Deferred:    deferred,
		Reasons:     reasons,
	}
}

func parseInt64(values map[string]string, key string) (int64, bool) {
	value, ok := values[key]
	if !ok {
		return 0, false
	}
	parsed, err := strconv.ParseInt(value, 10, 64)
	return parsed, err == nil
}

func optionalInt64(values map[string]string, key string) int64 {
	value, _ := parseInt64(values, key)
	return value
}

func parseFloat64(values map[string]string, key string) (float64, bool) {
	value, ok := values[key]
	if !ok {
		return 0, false
	}
	parsed, err := strconv.ParseFloat(value, 64)
	return parsed, err == nil && !math.IsNaN(parsed) && !math.IsInf(parsed, 0)
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
