package main

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/mavlevich/schurfer/collector/internal/bybit"
	"github.com/mavlevich/schurfer/collector/internal/hotset"
	"github.com/mavlevich/schurfer/collector/internal/orderflow"
	"github.com/redis/go-redis/v9"
)

const (
	healthKey       = "market:orderflow:health"
	healthTTL       = 30 * time.Second
	pollInterval    = 5 * time.Second
	flushInterval   = 30 * time.Second
	pruneInterval   = time.Hour
	seenEventTTL    = 24 * time.Hour
	shutdownTimeout = 10 * time.Second
)

type config struct {
	RedisAddr       string
	Symbols         []string
	BucketSize      time.Duration
	Prebuffer       time.Duration
	CaptureAfter    time.Duration
	Controls        int
	MaxSymbols      int
	MaxActiveEvents int
	RecentTradeIDs  int
	QueueSize       int
	MaxPending      int
	StorageRoot     string
	MaxDiskBytes    int64
	Retention       time.Duration
}

type counters struct {
	eventsTotal        uint64
	invalidTotal       uint64
	duplicateTotal     uint64
	outOfOrderTotal    uint64
	queueDroppedTotal  uint64
	pendingDropped     uint64
	recordsPersisted   uint64
	persistErrors      uint64
	storageLimited     uint64
	activationTotal    uint64
	leftCensoredTotal  uint64
	capacityRejected   uint64
	controlsSelected   uint64
	lastEventAt        time.Time
	lastLag            time.Duration
	maxLag             time.Duration
	windowMaxLag       time.Duration
	lastHealthAt       time.Time
	lastHealthEvents   uint64
	lastPersistedBytes int64
}

type application struct {
	cfg        config
	startedAt  time.Time
	engine     *orderflow.Engine
	store      *orderflow.FileStore
	hotset     *hotset.RedisStore
	redis      *redis.Client
	trades     <-chan bybit.PublicTrade
	dropped    *atomic.Uint64
	seenEvents map[int64]time.Time
	pending    []orderflow.Record
	stats      counters
}

func main() {
	if err := run(); err != nil {
		slog.Error("fatal", "err", err)
		os.Exit(1)
	}
}

func run() error {
	configureLogging()
	cfg := loadConfig()
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()
	startedAt := time.Now()

	engine, err := orderflow.New(orderflow.Config{
		BucketSize:      cfg.BucketSize,
		Prebuffer:       cfg.Prebuffer,
		CaptureAfter:    cfg.CaptureAfter,
		Controls:        cfg.Controls,
		MaxSymbols:      cfg.MaxSymbols,
		MaxActiveEvents: cfg.MaxActiveEvents,
		RecentTradeIDs:  cfg.RecentTradeIDs,
	}, startedAt)
	if err != nil {
		return fmt.Errorf("order-flow engine: %w", err)
	}
	store, err := orderflow.NewFileStore(cfg.StorageRoot, cfg.MaxDiskBytes, cfg.Retention)
	if err != nil {
		return fmt.Errorf("order-flow store: %w", err)
	}
	rdb := redis.NewClient(&redis.Options{Addr: cfg.RedisAddr})
	defer func() {
		if err := rdb.Close(); err != nil {
			slog.Warn("orderflow.redis.close_failed", "err", err)
		}
	}()
	if err := rdb.Ping(ctx).Err(); err != nil {
		return fmt.Errorf("redis: %w", err)
	}
	activationStore, err := hotset.NewRedisStore(rdb, 1, time.Second)
	if err != nil {
		return fmt.Errorf("activation store: %w", err)
	}

	source := bybit.NewSource()
	symbols := cfg.Symbols
	if len(symbols) == 0 {
		symbols, err = source.FetchSymbols(ctx)
		if err != nil {
			return fmt.Errorf("fetch symbols: %w", err)
		}
	}
	if len(symbols) == 0 {
		return errors.New("no Bybit symbols to subscribe to")
	}
	trades := make(chan bybit.PublicTrade, cfg.QueueSize)
	var queueDropped atomic.Uint64
	go func() {
		_ = source.RunTrades(ctx, symbols, func(_ context.Context, trade bybit.PublicTrade) error {
			select {
			case trades <- trade:
			default:
				queueDropped.Add(1)
			}
			return nil
		})
		close(trades)
	}()

	slog.Info(
		"orderflow.starting",
		"symbols", len(symbols),
		"bucket_seconds", cfg.BucketSize.Seconds(),
		"prebuffer_minutes", cfg.Prebuffer.Minutes(),
		"capture_after_minutes", cfg.CaptureAfter.Minutes(),
		"controls", cfg.Controls,
		"queue_size", cfg.QueueSize,
		"max_pending", cfg.MaxPending,
		"max_disk_bytes", cfg.MaxDiskBytes,
	)
	app := &application{
		cfg:        cfg,
		startedAt:  startedAt,
		engine:     engine,
		store:      store,
		hotset:     activationStore,
		redis:      rdb,
		trades:     trades,
		dropped:    &queueDropped,
		seenEvents: make(map[int64]time.Time),
		stats: counters{
			lastHealthAt: startedAt,
		},
	}
	return app.loop(ctx)
}

func (app *application) loop(ctx context.Context) error {
	activationTicker := time.NewTicker(pollInterval)
	defer activationTicker.Stop()
	flushTicker := time.NewTicker(flushInterval)
	defer flushTicker.Stop()
	healthTicker := time.NewTicker(pollInterval)
	defer healthTicker.Stop()
	pruneTicker := time.NewTicker(pruneInterval)
	defer pruneTicker.Stop()

	for {
		select {
		case <-ctx.Done():
			shutdownCtx, cancel := context.WithTimeout(context.Background(), shutdownTimeout)
			app.flush(shutdownCtx)
			app.storeHealth(shutdownCtx, time.Now())
			cancel()
			return nil
		case trade, ok := <-app.trades:
			if !ok {
				if ctx.Err() != nil {
					return nil
				}
				return errors.New("bybit public-trade stream stopped")
			}
			app.observe(trade)
		case now := <-activationTicker.C:
			app.syncActivations(ctx, now)
		case <-flushTicker.C:
			app.flush(ctx)
		case now := <-healthTicker.C:
			app.storeHealth(ctx, now)
		case now := <-pruneTicker.C:
			removed, err := app.store.Prune(now)
			if err != nil {
				slog.Warn("orderflow.prune_failed", "err", err)
			} else if removed > 0 {
				slog.Info("orderflow.pruned", "bytes", removed)
			}
		}
	}
}

func (app *application) observe(trade bybit.PublicTrade) {
	app.stats.eventsTotal++
	lag := max(trade.ReceivedAt.Sub(trade.EventAt), time.Duration(0))
	app.stats.lastEventAt = maxTime(app.stats.lastEventAt, trade.EventAt)
	app.stats.lastLag = lag
	app.stats.maxLag = max(app.stats.maxLag, lag)
	app.stats.windowMaxLag = max(app.stats.windowMaxLag, lag)

	records, err := app.engine.Observe(trade)
	switch {
	case errors.Is(err, orderflow.ErrDuplicateTrade):
		app.stats.duplicateTotal++
	case errors.Is(err, orderflow.ErrOutOfOrderTrade):
		app.stats.outOfOrderTotal++
	case err != nil:
		app.stats.invalidTotal++
	default:
		app.enqueue(records)
	}
}

func (app *application) syncActivations(ctx context.Context, now time.Time) {
	active, err := app.hotset.ActiveActivations(ctx, now)
	if err != nil {
		slog.Warn("orderflow.activations_failed", "err", err)
		return
	}
	activeSymbols := make([]string, 0, len(active))
	activeEventIDs := make(map[int64]struct{}, len(active))
	for _, current := range active {
		activeSymbols = append(activeSymbols, current.Symbol)
		activeEventIDs[current.PumpEventID] = struct{}{}
	}
	pruneSeenEvents(app.seenEvents, activeEventIDs, now, seenEventTTL)
	for _, current := range active {
		if _, seen := app.seenEvents[current.PumpEventID]; seen {
			app.seenEvents[current.PumpEventID] = now
			continue
		}
		app.seenEvents[current.PumpEventID] = now
		records, controls, activateErr := app.engine.Activate(orderflow.Activation{
			PumpEventID:      current.PumpEventID,
			Base:             current.Base,
			Symbol:           current.Symbol,
			FirstObservedAt:  now,
			ExcludedControls: activeSymbols,
		})
		switch {
		case errors.Is(activateErr, orderflow.ErrPrebufferNotReady):
			app.stats.leftCensoredTotal++
			slog.Info(
				"orderflow.activation_excluded",
				"pump_event_id", current.PumpEventID,
				"symbol", current.Symbol,
				"reason", "left_censored",
			)
		case errors.Is(activateErr, orderflow.ErrCaptureCapacity):
			app.stats.capacityRejected++
		case activateErr != nil:
			app.stats.invalidTotal++
			slog.Warn("orderflow.activation_failed", "err", activateErr)
		default:
			app.stats.activationTotal++
			app.stats.controlsSelected += uint64(len(controls))
			app.enqueue(records)
			app.flush(ctx)
			slog.Info(
				"orderflow.activated",
				"pump_event_id", current.PumpEventID,
				"symbol", current.Symbol,
				"controls", controls,
				"prebuffer_records", len(records),
			)
		}
	}
	app.engine.Expire(now)
}

func (app *application) enqueue(records []orderflow.Record) {
	if len(records) == 0 {
		return
	}
	app.pending = append(app.pending, records...)
	if extra := len(app.pending) - app.cfg.MaxPending; extra > 0 {
		copy(app.pending, app.pending[extra:])
		app.pending = app.pending[:app.cfg.MaxPending]
		app.stats.pendingDropped += uint64(extra)
	}
}

func (app *application) flush(_ context.Context) {
	if len(app.pending) == 0 {
		return
	}
	written, err := app.store.Append(app.pending)
	app.stats.lastPersistedBytes = written
	if errors.Is(err, orderflow.ErrStorageBudget) {
		app.stats.storageLimited++
		app.stats.pendingDropped += uint64(len(app.pending))
		app.pending = app.pending[:0]
		slog.Warn("orderflow.storage_budget_reached", "size_bytes", app.store.SizeBytes())
		return
	}
	if err != nil {
		app.stats.persistErrors++
		slog.Warn("orderflow.persist_failed", "records", len(app.pending), "err", err)
		return
	}
	app.stats.recordsPersisted += uint64(len(app.pending))
	app.pending = app.pending[:0]
}

func (app *application) storeHealth(ctx context.Context, now time.Time) {
	app.stats.queueDroppedTotal = app.dropped.Load()
	elapsed := now.Sub(app.stats.lastHealthAt).Seconds()
	rate := 0.0
	if elapsed > 0 {
		rate = float64(app.stats.eventsTotal-app.stats.lastHealthEvents) / elapsed
	}
	status := "warming"
	if !app.stats.lastEventAt.IsZero() {
		status = "ok"
	}
	if !app.stats.lastEventAt.IsZero() && now.Sub(app.stats.lastEventAt) > healthTTL {
		status = "stale"
	}
	if app.stats.queueDroppedTotal > 0 || app.stats.pendingDropped > 0 {
		status = "degraded"
	}
	if app.stats.storageLimited > 0 {
		status = "storage_limited"
	}
	uptime := max(now.Sub(app.startedAt), time.Duration(0))
	storageBytesPerDay := 0.0
	if uptime > 0 {
		storageBytesPerDay = float64(app.store.SizeBytes()) * float64(24*time.Hour) /
			float64(uptime)
	}
	lastEventAtMS := int64(0)
	if !app.stats.lastEventAt.IsZero() {
		lastEventAtMS = app.stats.lastEventAt.UnixMilli()
	}
	fields := map[string]any{
		"schema_version":          1,
		"capture_contract":        orderflow.ContractVersion,
		"status":                  status,
		"started_at_ms":           app.startedAt.UnixMilli(),
		"updated_at_ms":           now.UnixMilli(),
		"uptime_seconds":          int64(uptime.Seconds()),
		"last_event_at_ms":        lastEventAtMS,
		"events_total":            app.stats.eventsTotal,
		"event_rate_per_sec":      fmt.Sprintf("%.2f", rate),
		"observed_symbols":        app.engine.ObservedSymbols(),
		"completed_buckets_total": app.engine.CompletedBuckets(),
		"buffered_buckets":        app.engine.BufferedBuckets(),
		"active_captures":         app.engine.ActiveCaptures(),
		"seen_events":             len(app.seenEvents),
		"activation_total":        app.stats.activationTotal,
		"left_censored_total":     app.stats.leftCensoredTotal,
		"capacity_rejected_total": app.stats.capacityRejected,
		"controls_selected_total": app.stats.controlsSelected,
		"records_persisted_total": app.stats.recordsPersisted,
		"queue_dropped_total":     app.stats.queueDroppedTotal,
		"pending_dropped_total":   app.stats.pendingDropped,
		"invalid_total":           app.stats.invalidTotal,
		"duplicate_total":         app.stats.duplicateTotal,
		"out_of_order_total":      app.stats.outOfOrderTotal,
		"persist_errors_total":    app.stats.persistErrors,
		"storage_limited_total":   app.stats.storageLimited,
		"storage_bytes":           app.store.SizeBytes(),
		"storage_bytes_per_day":   fmt.Sprintf("%.0f", storageBytesPerDay),
		"last_write_bytes":        app.stats.lastPersistedBytes,
		"last_lag_ms":             app.stats.lastLag.Milliseconds(),
		"max_lag_ms":              app.stats.maxLag.Milliseconds(),
		"window_max_lag_ms":       app.stats.windowMaxLag.Milliseconds(),
	}
	pipe := app.redis.Pipeline()
	pipe.HSet(ctx, healthKey, fields)
	pipe.Expire(ctx, healthKey, healthTTL)
	if _, err := pipe.Exec(ctx); err != nil {
		slog.Warn("orderflow.health_failed", "err", err)
	}
	app.stats.lastHealthAt = now
	app.stats.lastHealthEvents = app.stats.eventsTotal
	app.stats.windowMaxLag = 0
}

func loadConfig() config {
	return config{
		RedisAddr:       envString("REDIS_ADDR", "localhost:6379"),
		Symbols:         envList("BYBIT_SYMBOLS"),
		BucketSize:      envSeconds("ORDERFLOW_BUCKET_SECONDS", time.Second),
		Prebuffer:       envSeconds("ORDERFLOW_PREBUFFER_SECONDS", 30*time.Minute),
		CaptureAfter:    envSeconds("ORDERFLOW_CAPTURE_AFTER_SECONDS", time.Hour),
		Controls:        envPositiveInt("ORDERFLOW_CONTROLS", 3),
		MaxSymbols:      envPositiveInt("ORDERFLOW_MAX_SYMBOLS", 1000),
		MaxActiveEvents: envPositiveInt("ORDERFLOW_MAX_ACTIVE_EVENTS", 32),
		RecentTradeIDs:  envPositiveInt("ORDERFLOW_RECENT_TRADE_IDS", 512),
		QueueSize:       envPositiveInt("ORDERFLOW_QUEUE_SIZE", 32768),
		MaxPending:      envPositiveInt("ORDERFLOW_MAX_PENDING_RECORDS", 8192),
		StorageRoot:     envString("ORDERFLOW_STORAGE_ROOT", "/data/orderflow"),
		MaxDiskBytes:    envPositiveInt64("ORDERFLOW_MAX_DISK_BYTES", 5<<30),
		Retention:       envSeconds("ORDERFLOW_RETENTION_SECONDS", 14*24*time.Hour),
	}
}

func configureLogging() {
	level := slog.LevelInfo
	if strings.EqualFold(os.Getenv("LOG_LEVEL"), "debug") {
		level = slog.LevelDebug
	}
	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: level})))
}

func envString(key, fallback string) string {
	if value := strings.TrimSpace(os.Getenv(key)); value != "" {
		return value
	}
	return fallback
}

func envList(key string) []string {
	var values []string
	for _, value := range strings.Split(os.Getenv(key), ",") {
		if value = strings.TrimSpace(value); value != "" {
			values = append(values, value)
		}
	}
	return values
}

func envPositiveInt(key string, fallback int) int {
	value := strings.TrimSpace(os.Getenv(key))
	if value == "" {
		return fallback
	}
	parsed, err := strconv.Atoi(value)
	if err != nil || parsed <= 0 {
		slog.Warn("orderflow.config.invalid", "key", key, "value", value, "fallback", fallback)
		return fallback
	}
	return parsed
}

func envPositiveInt64(key string, fallback int64) int64 {
	value := strings.TrimSpace(os.Getenv(key))
	if value == "" {
		return fallback
	}
	parsed, err := strconv.ParseInt(value, 10, 64)
	if err != nil || parsed <= 0 {
		slog.Warn("orderflow.config.invalid", "key", key, "value", value, "fallback", fallback)
		return fallback
	}
	return parsed
}

func envSeconds(key string, fallback time.Duration) time.Duration {
	return time.Duration(envPositiveInt(key, int(fallback.Seconds()))) * time.Second
}

func maxTime(left, right time.Time) time.Time {
	if right.After(left) {
		return right
	}
	return left
}

func pruneSeenEvents(
	seen map[int64]time.Time,
	active map[int64]struct{},
	now time.Time,
	retention time.Duration,
) {
	cutoff := now.Add(-retention)
	for eventID, lastActiveAt := range seen {
		if _, stillActive := active[eventID]; stillActive {
			continue
		}
		if lastActiveAt.Before(cutoff) {
			delete(seen, eventID)
		}
	}
}
