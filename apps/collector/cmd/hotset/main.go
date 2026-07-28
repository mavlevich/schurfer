package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"math"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/mavlevich/schurfer/collector/internal/bybit"
	"github.com/mavlevich/schurfer/collector/internal/hotset"
	"github.com/nats-io/nats.go"
	"github.com/redis/go-redis/v9"
)

const (
	natsSubject     = "market.bybit.ticker.*"
	eventBuffer     = 8192
	maxPendingBars  = 10000
	measurementPoll = 5 * time.Second
	healthInterval  = 5 * time.Second
	maxFutureSkew   = 5 * time.Second
	shutdownTimeout = 5 * time.Second
	persistRetryMin = time.Second
	persistRetryMax = 30 * time.Second
)

type config struct {
	NATSURL      string
	RedisAddr    string
	BucketSize   time.Duration
	Prebuffer    time.Duration
	HotTTL       time.Duration
	MaxSymbols   int
	StreamMaxLen int64
	StreamTTL    time.Duration
}

type counters struct {
	eventsTotal         uint64
	invalidTotal        uint64
	outOfOrderTotal     uint64
	barsPersistedTotal  uint64
	persistErrorsTotal  uint64
	natsDroppedTotal    uint64
	pendingDroppedTotal uint64
	lastEventAt         time.Time
	lastLag             time.Duration
	maxLag              time.Duration
	windowMaxLag        time.Duration
}

type application struct {
	cfg                   config
	engine                *hotset.Engine
	store                 *hotset.RedisStore
	subscription          *nats.Subscription
	messages              <-chan *nats.Msg
	stats                 counters
	pending               []hotset.Bar
	pumpFeedStatus        string
	measurementCandidates int
	unmappedCandidates    int
	lastHealthAt          time.Time
	lastHealthEvents      uint64
	persistRetry          persistRetryState
}

type persistRetryState struct {
	nextAttempt time.Time
	backoff     time.Duration
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

	engine, err := hotset.New(hotset.Config{
		BucketSize: cfg.BucketSize,
		Prebuffer:  cfg.Prebuffer,
		HotTTL:     cfg.HotTTL,
		MaxSymbols: cfg.MaxSymbols,
	})
	if err != nil {
		return fmt.Errorf("hot-set engine: %w", err)
	}

	rdb := redis.NewClient(&redis.Options{Addr: cfg.RedisAddr})
	defer func() {
		if err := rdb.Close(); err != nil {
			slog.Warn("hotset.redis.close_failed", "err", err)
		}
	}()
	if err := rdb.Ping(ctx).Err(); err != nil {
		return fmt.Errorf("redis: %w", err)
	}
	store, err := hotset.NewRedisStore(rdb, cfg.StreamMaxLen, cfg.StreamTTL)
	if err != nil {
		return fmt.Errorf("hot-set store: %w", err)
	}

	nc, err := nats.Connect(
		cfg.NATSURL,
		nats.MaxReconnects(-1),
		nats.ReconnectWait(2*time.Second),
		nats.ErrorHandler(func(_ *nats.Conn, _ *nats.Subscription, err error) {
			slog.Warn("hotset.nats.async_error", "err", err)
		}),
	)
	if err != nil {
		return fmt.Errorf("nats: %w", err)
	}
	defer func() {
		if err := nc.Drain(); err != nil {
			slog.Warn("hotset.nats.drain_failed", "err", err)
		}
	}()

	messages := make(chan *nats.Msg, eventBuffer)
	subscription, err := nc.ChanSubscribe(natsSubject, messages)
	if err != nil {
		return fmt.Errorf("subscribe %s: %w", natsSubject, err)
	}
	if err := nc.FlushTimeout(5 * time.Second); err != nil {
		return fmt.Errorf("flush subscription: %w", err)
	}

	slog.Info(
		"hotset.starting",
		"subject", natsSubject,
		"bucket_seconds", cfg.BucketSize.Seconds(),
		"prebuffer_minutes", cfg.Prebuffer.Minutes(),
		"hot_ttl_minutes", cfg.HotTTL.Minutes(),
		"max_symbols", cfg.MaxSymbols,
		"stream_max_len", cfg.StreamMaxLen,
	)

	app := &application{
		cfg:            cfg,
		engine:         engine,
		store:          store,
		subscription:   subscription,
		messages:       messages,
		pumpFeedStatus: "unknown",
		lastHealthAt:   time.Now(),
	}
	return app.loop(ctx)
}

func (app *application) loop(ctx context.Context) error {
	pumpTicker := time.NewTicker(measurementPoll)
	defer pumpTicker.Stop()
	healthTicker := time.NewTicker(healthInterval)
	defer healthTicker.Stop()

	now := time.Now()
	app.syncActivations(ctx, now)
	app.flushPending(ctx, now, false)

	for {
		select {
		case <-ctx.Done():
			shutdownCtx, shutdownCancel := context.WithTimeout(context.Background(), shutdownTimeout)
			app.flushPending(shutdownCtx, time.Now(), true)
			shutdownCancel()
			return nil
		case msg, ok := <-app.messages:
			if !ok {
				return errors.New("NATS ticker channel closed")
			}
			app.handleMessage(ctx, msg, time.Now())
		case now := <-pumpTicker.C:
			app.syncActivations(ctx, now)
			app.flushPending(ctx, now, false)
		case now := <-healthTicker.C:
			app.storeHealth(ctx, now)
		}
	}
}

func (app *application) syncActivations(ctx context.Context, now time.Time) {
	snapshot, loadErr := app.store.Activations(ctx)
	app.pumpFeedStatus = snapshot.Status
	app.measurementCandidates = snapshot.Candidates
	app.unmappedCandidates = snapshot.Unmapped
	if loadErr != nil {
		slog.Warn("hotset.measurement_feed_failed", "err", loadErr)
	} else if snapshot.Status == "ok" {
		if refreshErr := app.store.RefreshActivations(
			ctx,
			snapshot.Activations,
			now.Add(app.cfg.HotTTL),
		); refreshErr != nil {
			slog.Warn("hotset.activation_registry_refresh_failed", "err", refreshErr)
		}
	}
	active, registryErr := app.store.ActiveActivations(ctx, now)
	if registryErr != nil {
		slog.Warn("hotset.activation_registry_read_failed", "err", registryErr)
		return
	}
	for _, activation := range active {
		bars, activated := app.engine.Activate(activation, now)
		if !activated {
			slog.Debug(
				"hotset.activation_rejected",
				"symbol", activation.Symbol,
				"pump_event_id", activation.PumpEventID,
				"reason", "capacity_or_invalid",
			)
			continue
		}
		app.pending, app.stats.pendingDroppedTotal = enqueue(
			app.pending,
			bars,
			app.stats.pendingDroppedTotal,
		)
	}
}

func (app *application) handleMessage(ctx context.Context, msg *nats.Msg, now time.Time) {
	app.stats.eventsTotal++
	tick, parseErr := parseTicker(msg.Data, now)
	if parseErr != nil {
		app.stats.invalidTotal++
		return
	}
	lag := max(tick.ReceivedAt.Sub(tick.EventAt), time.Duration(0))
	if tick.EventAt.After(app.stats.lastEventAt) {
		app.stats.lastEventAt = tick.EventAt
	}
	app.stats.lastLag = lag
	app.stats.maxLag = max(app.stats.maxLag, lag)
	app.stats.windowMaxLag = max(app.stats.windowMaxLag, lag)

	bars, observeErr := app.engine.Observe(tick)
	switch {
	case errors.Is(observeErr, hotset.ErrOutOfOrderTick):
		app.stats.outOfOrderTotal++
	case observeErr != nil:
		app.stats.invalidTotal++
	default:
		app.pending, app.stats.pendingDroppedTotal = enqueue(
			app.pending,
			bars,
			app.stats.pendingDroppedTotal,
		)
		app.flushPending(ctx, now, false)
	}
}

func (app *application) flushPending(ctx context.Context, now time.Time, force bool) {
	if len(app.pending) == 0 || (!force && !app.persistRetry.ready(now)) {
		return
	}
	if storeErr := app.store.StoreBars(ctx, app.pending); storeErr != nil {
		app.stats.persistErrorsTotal++
		retryAfter := app.persistRetry.failed(now)
		slog.Warn(
			"hotset.persist_failed",
			"bars", len(app.pending),
			"retry_after_ms", retryAfter.Milliseconds(),
			"err", storeErr,
		)
		return
	}
	app.stats.barsPersistedTotal += uint64(len(app.pending))
	app.pending = app.pending[:0]
	app.persistRetry.succeeded()
}

func (app *application) storeHealth(ctx context.Context, now time.Time) {
	app.flushPending(ctx, now, false)
	dropped, dropErr := app.subscription.Dropped()
	if dropErr == nil && dropped > 0 {
		app.stats.natsDroppedTotal = uint64(dropped)
	}
	elapsed := now.Sub(app.lastHealthAt).Seconds()
	rate := 0.0
	if elapsed > 0 {
		rate = float64(app.stats.eventsTotal-app.lastHealthEvents) / elapsed
	}
	health := hotset.Health{
		UpdatedAt:             now,
		LastEventAt:           app.stats.lastEventAt,
		EventsTotal:           app.stats.eventsTotal,
		InvalidTotal:          app.stats.invalidTotal,
		OutOfOrderTotal:       app.stats.outOfOrderTotal,
		BarsPersistedTotal:    app.stats.barsPersistedTotal,
		PersistErrorsTotal:    app.stats.persistErrorsTotal,
		NATSDroppedTotal:      app.stats.natsDroppedTotal,
		PendingDroppedTotal:   app.stats.pendingDroppedTotal,
		ObservedSymbols:       app.engine.ObservedSymbols(),
		HotSymbols:            app.engine.HotCount(now),
		EventRate:             rate,
		LastLag:               app.stats.lastLag,
		MaxLag:                app.stats.maxLag,
		WindowMaxLag:          app.stats.windowMaxLag,
		PumpFeedStatus:        app.pumpFeedStatus,
		MeasurementCandidates: app.measurementCandidates,
		UnmappedCandidates:    app.unmappedCandidates,
	}
	if storeErr := app.store.StoreHealth(ctx, health); storeErr != nil {
		slog.Warn("hotset.health_failed", "err", storeErr)
	}
	app.lastHealthAt = now
	app.lastHealthEvents = app.stats.eventsTotal
	app.stats.windowMaxLag = 0
}

func (state *persistRetryState) ready(now time.Time) bool {
	return state.nextAttempt.IsZero() || !now.Before(state.nextAttempt)
}

func (state *persistRetryState) failed(now time.Time) time.Duration {
	if state.backoff == 0 {
		state.backoff = persistRetryMin
	} else {
		state.backoff = min(state.backoff*2, persistRetryMax)
	}
	state.nextAttempt = now.Add(state.backoff)
	return state.backoff
}

func (state *persistRetryState) succeeded() {
	state.nextAttempt = time.Time{}
	state.backoff = 0
}

func parseTicker(data []byte, receivedAt time.Time) (hotset.Tick, error) {
	var event bybit.TickerEvent
	if err := json.Unmarshal(data, &event); err != nil {
		return hotset.Tick{}, fmt.Errorf("decode ticker: %w", err)
	}
	if event.SchemaVersion != 0 && event.SchemaVersion != 1 {
		return hotset.Tick{}, fmt.Errorf("unsupported ticker schema version %d", event.SchemaVersion)
	}
	if event.Source != "bybit" {
		return hotset.Tick{}, fmt.Errorf("unexpected ticker source %q", event.Source)
	}
	last, err := requiredFloat(event.LastPrice)
	if err != nil {
		return hotset.Tick{}, fmt.Errorf("last price: %w", err)
	}
	eventAt := time.UnixMilli(event.TS)
	if event.TS <= 0 {
		return hotset.Tick{}, errors.New("ticker timestamp is required")
	}
	if eventAt.After(receivedAt.Add(maxFutureSkew)) {
		return hotset.Tick{}, errors.New("ticker timestamp is too far in the future")
	}
	return hotset.Tick{
		Symbol:      event.Symbol,
		EventAt:     eventAt,
		ReceivedAt:  receivedAt,
		LastPrice:   last,
		Bid:         optionalFloat(event.Bid),
		Ask:         optionalFloat(event.Ask),
		Volume24h:   optionalFloat(event.Volume24h),
		Turnover24h: optionalFloat(event.Turnover24h),
	}, nil
}

func requiredFloat(value *string) (float64, error) {
	if value == nil {
		return 0, errors.New("value is missing")
	}
	parsed, err := strconv.ParseFloat(*value, 64)
	if err != nil || math.IsNaN(parsed) || math.IsInf(parsed, 0) || parsed <= 0 {
		return 0, errors.New("value must be finite and positive")
	}
	return parsed, nil
}

func optionalFloat(value *string) *float64 {
	if value == nil {
		return nil
	}
	parsed, err := strconv.ParseFloat(*value, 64)
	if err != nil || math.IsNaN(parsed) || math.IsInf(parsed, 0) || parsed < 0 {
		return nil
	}
	return &parsed
}

func enqueue(pending []hotset.Bar, bars []hotset.Bar, dropped uint64) ([]hotset.Bar, uint64) {
	if len(bars) == 0 {
		return pending, dropped
	}
	pending = append(pending, bars...)
	if extra := len(pending) - maxPendingBars; extra > 0 {
		copy(pending, pending[extra:])
		pending = pending[:maxPendingBars]
		dropped += uint64(extra)
	}
	return pending, dropped
}

func configureLogging() {
	level := slog.LevelInfo
	if strings.EqualFold(os.Getenv("LOG_LEVEL"), "debug") {
		level = slog.LevelDebug
	}
	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: level})))
}

func loadConfig() config {
	return config{
		NATSURL:      envString("NATS_URL", "nats://localhost:4222"),
		RedisAddr:    envString("REDIS_ADDR", "localhost:6379"),
		BucketSize:   envDurationSeconds("HOTSET_BUCKET_SECONDS", 5*time.Second),
		Prebuffer:    envDurationSeconds("HOTSET_PREBUFFER_SECONDS", 10*time.Minute),
		HotTTL:       envDurationSeconds("HOTSET_TTL_SECONDS", 4*time.Hour),
		MaxSymbols:   envPositiveInt("HOTSET_MAX_SYMBOLS", 12),
		StreamMaxLen: int64(envPositiveInt("HOTSET_STREAM_MAXLEN", 3600)),
		StreamTTL:    envDurationSeconds("HOTSET_STREAM_TTL_SECONDS", 24*time.Hour),
	}
}

func envString(key, fallback string) string {
	if value := strings.TrimSpace(os.Getenv(key)); value != "" {
		return value
	}
	return fallback
}

func envPositiveInt(key string, fallback int) int {
	value := strings.TrimSpace(os.Getenv(key))
	if value == "" {
		return fallback
	}
	parsed, err := strconv.Atoi(value)
	if err != nil || parsed <= 0 {
		slog.Warn("hotset.config.invalid", "key", key, "value", value, "fallback", fallback)
		return fallback
	}
	return parsed
}

func envDurationSeconds(key string, fallback time.Duration) time.Duration {
	return time.Duration(envPositiveInt(key, int(fallback.Seconds()))) * time.Second
}
