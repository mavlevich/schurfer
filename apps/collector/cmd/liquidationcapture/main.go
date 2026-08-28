// Command liquidationcapture records public liquidation observations for one
// venue. Compose runs one isolated process per venue; the shared binary only
// prevents two copies of lifecycle/writer logic from drifting.
package main

import (
	"context"
	"crypto/rand"
	"fmt"
	"log/slog"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/mavlevich/schurfer/collector/internal/binance"
	"github.com/mavlevich/schurfer/collector/internal/bybit"
	"github.com/mavlevich/schurfer/collector/internal/liquidationcapture"
	"github.com/mavlevich/schurfer/collector/internal/wsstream"
	"github.com/redis/go-redis/v9"
)

const (
	marketType      = "linear"
	flushInterval   = time.Second
	healthInterval  = 5 * time.Second
	shutdownTimeout = 20 * time.Second
)

type config struct {
	Exchange    string
	DatabaseURL string
	RedisAddr   string
}

func main() {
	if len(os.Args) > 1 && os.Args[1] == "healthcheck" {
		if err := runHealthcheck(); err != nil {
			fmt.Fprintf(os.Stderr, "healthcheck failed: %v\n", err)
			os.Exit(1)
		}
		os.Exit(0)
	}

	if err := run(); err != nil {
		slog.Error("liquidationcapture.fatal", "err", err)
		os.Exit(1)
	}
}

func run() error {
	configureLogging()
	cfg, err := loadConfig()
	if err != nil {
		return err
	}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	source, symbols, err := buildSource(ctx, cfg.Exchange)
	if err != nil {
		return err
	}
	expected := source.ExpectedConnections(len(symbols))
	tracker, err := liquidationcapture.NewCoverageTracker(expected)
	if err != nil {
		return err
	}
	processSessionID, err := wsstream.NewSessionID(rand.Reader)
	if err != nil {
		return fmt.Errorf("process session id: %w", err)
	}

	writer, err := liquidationcapture.NewWriter(ctx, cfg.DatabaseURL)
	if err != nil {
		return err
	}
	defer writer.Close()
	rdb := redis.NewClient(&redis.Options{Addr: cfg.RedisAddr})
	defer func() { _ = rdb.Close() }()
	if err := rdb.Ping(ctx).Err(); err != nil {
		return fmt.Errorf("ping redis: %w", err)
	}
	healthStore, err := liquidationcapture.NewRedisStore(rdb)
	if err != nil {
		return err
	}

	startedAt := time.Now()
	universeVersion := liquidationcapture.UniverseVersion(symbols)
	slog.Info("liquidationcapture.start",
		"exchange", cfg.Exchange, "coverage_kind", source.CoverageKind(),
		"symbols", len(symbols), "expected_connections", expected,
		"process_session_id", processSessionID, "universe_version", universeVersion,
	)
	sourceDone := make(chan error, 1)
	go func() {
		sourceDone <- source.RunLiquidations(ctx, symbols, universeVersion,
			func(_ context.Context, event liquidationcapture.Event) error {
				if !writer.Enqueue(event) {
					tracker.MarkDataLoss()
					slog.Error("liquidationcapture.writer_queue_full", "exchange", cfg.Exchange)
				}
				return nil
			},
			tracker.ObserveLifecycle,
		)
	}()

	flushTicker := time.NewTicker(flushInterval)
	healthTicker := time.NewTicker(healthInterval)
	defer flushTicker.Stop()
	defer healthTicker.Stop()

	nextHeartbeatBucket := startedAt.UTC().Truncate(time.Minute)

	evaluatorState := &liquidationcapture.EvaluatorState{
		StartedAt: startedAt,
		LastSnapshot: liquidationcapture.Snapshot{
			Source: source.Stats(),
			Writer: writer.Stats(),
		},
		LastSnapshotTime:    startedAt,
		LastHeartbeatBucket: nextHeartbeatBucket,
	}

	for {
		select {
		case <-ctx.Done():
			shutdownCtx, cancel := context.WithTimeout(context.Background(), shutdownTimeout)
			defer cancel()
			select {
			case err := <-sourceDone:
				if err != nil && err != context.Canceled {
					slog.Warn("liquidationcapture.source_stopped", "err", err)
				}
			case <-shutdownCtx.Done():
				return fmt.Errorf("source shutdown: %w", shutdownCtx.Err())
			}
			if err := writer.Flush(shutdownCtx); err != nil {
				return err
			}
			return nil

		case err := <-sourceDone:
			if ctx.Err() != nil {
				shutdownCtx, cancel := context.WithTimeout(context.Background(), shutdownTimeout)
				defer cancel()
				if flushErr := writer.Flush(shutdownCtx); flushErr != nil {
					return flushErr
				}
				return nil
			}
			if err == nil {
				return fmt.Errorf("liquidation source stopped unexpectedly")
			}
			return fmt.Errorf("liquidation source: %w", err)

		case <-flushTicker.C:
			if err := writer.Flush(ctx); err != nil {
				slog.Error("liquidationcapture.flush_failed", "err", err)
			}
			nextHeartbeatBucket = writeDueHeartbeats(
				ctx, time.Now().UTC(), nextHeartbeatBucket, cfg.Exchange,
				universeVersion, processSessionID, source, tracker, writer, evaluatorState,
			)

		case <-healthTicker.C:
			connected, expectedConnections, loss := tracker.Connected()
			writerStats := writer.Stats()
			sourceStats := source.Stats()

			// Compute evaluated health
			evaluated := liquidationcapture.EvaluateHealth(
				evaluatorState,
				time.Now(),
				sourceStats,
				writerStats,
				connected,
				expectedConnections,
				loss,
				liquidationcapture.MaxPendingEvents,
			)

			// Update state snapshot
			evaluatorState.LastSnapshot = liquidationcapture.Snapshot{
				Source: sourceStats,
				Writer: writerStats,
			}
			evaluatorState.LastSnapshotTime = time.Now()

			health := liquidationcapture.Health{
				Exchange: cfg.Exchange, CoverageKind: source.CoverageKind(),
				ProcessSessionID: processSessionID, UniverseVersion: universeVersion,
				StartedAt: startedAt, UpdatedAt: time.Now(),
				LastEventAt: sourceStats.LastEventAt, LastPersistAt: writerStats.LastPersistAt,
				SubscribedSymbols: len(symbols), ConnectedConnections: connected,
				ExpectedConnections: expectedConnections, DataLossDetected: loss,
				Source: sourceStats, Writer: writerStats, Evaluated: evaluated,
			}
			if err := healthStore.StoreHealth(ctx, health); err != nil {
				slog.Error("liquidationcapture.health_failed", "err", err)
			}
			if evaluated.ShouldExit {
				incident := liquidationcapture.Incident{
					Exchange: cfg.Exchange, ProcessSessionID: processSessionID,
					OccurredAt: time.Now(), ReasonCodes: evaluated.ReasonCodes,
				}
				if err := healthStore.StoreIncident(ctx, incident); err != nil {
					slog.Error("liquidationcapture.incident_store_failed", "err", err)
				}
				return fmt.Errorf("fatal health evaluation: %s", evaluated.ReasonCodes)
			}
		}
	}
}

const heartbeatLatenessTolerance = 10 * time.Second

func writeDueHeartbeats(
	ctx context.Context,
	now time.Time,
	nextBucket time.Time,
	exchange string,
	universeVersion string,
	processSessionID string,
	source liquidationcapture.Source,
	tracker *liquidationcapture.CoverageTracker,
	writer *liquidationcapture.Writer,
	state *liquidationcapture.EvaluatorState,
) time.Time {
	for _, due := range heartbeatBucketsDue(nextBucket, now) {
		// If the scheduler is this late, it cannot honestly reconstruct the
		// exact connection/loss state of the old minute. Persist it as
		// incomplete instead of backfilling a false healthy interval.
		if due.Late {
			tracker.MarkDataLoss()
		}
		heartbeat := tracker.SnapshotAndReset(
			due.Bucket, exchange, marketType, source.CoverageKind(),
			processSessionID, universeVersion, source.Stats(), writer.Stats(),
		)
		if err := writer.WriteHeartbeat(ctx, heartbeat); err != nil {
			tracker.MarkDataLoss()
			slog.Error("liquidationcapture.heartbeat_failed", "bucket_start", due.Bucket, "err", err)
			state.ObserveHeartbeat(due.Bucket, false)
		} else {
			state.ObserveHeartbeat(due.Bucket, heartbeat.Complete)
		}
		nextBucket = due.Bucket.Add(time.Minute)
	}
	return nextBucket
}

type dueHeartbeat struct {
	Bucket time.Time
	Late   bool
}

func heartbeatBucketsDue(nextBucket time.Time, now time.Time) []dueHeartbeat {
	completedBefore := now.UTC().Truncate(time.Minute)
	var due []dueHeartbeat
	for bucket := nextBucket.UTC().Truncate(time.Minute); bucket.Before(completedBefore); bucket = bucket.Add(time.Minute) {
		due = append(due, dueHeartbeat{
			Bucket: bucket,
			Late:   now.Sub(bucket.Add(time.Minute)) > heartbeatLatenessTolerance,
		})
	}
	return due
}

func buildSource(ctx context.Context, exchange string) (liquidationcapture.Source, []string, error) {
	switch exchange {
	case "bybit":
		source := bybit.NewSource()
		catalog, err := source.FetchSymbolCatalog(ctx)
		if err != nil {
			return nil, nil, err
		}
		return source, catalog.CryptoPerpetualSymbols, nil
	case "binance":
		source := binance.NewSource()
		catalog, err := source.FetchSymbolCatalog(ctx)
		if err != nil {
			return nil, nil, err
		}
		return source, catalog.CryptoPerpetualSymbols, nil
	default:
		return nil, nil, fmt.Errorf("unsupported LIQUIDATION_CAPTURE_EXCHANGE %q", exchange)
	}
}

func loadConfig() (config, error) {
	cfg := config{
		Exchange:    strings.ToLower(strings.TrimSpace(os.Getenv("LIQUIDATION_CAPTURE_EXCHANGE"))),
		DatabaseURL: strings.TrimSpace(os.Getenv("DATABASE_URL")),
		RedisAddr:   strings.TrimSpace(os.Getenv("REDIS_ADDR")),
	}
	if cfg.Exchange != "bybit" && cfg.Exchange != "binance" {
		return config{}, fmt.Errorf("LIQUIDATION_CAPTURE_EXCHANGE must be bybit or binance")
	}
	if cfg.DatabaseURL == "" || cfg.RedisAddr == "" {
		return config{}, fmt.Errorf("DATABASE_URL and REDIS_ADDR are required")
	}
	return cfg, nil
}

func configureLogging() {
	level := slog.LevelInfo
	if strings.EqualFold(os.Getenv("LOG_LEVEL"), "debug") {
		level = slog.LevelDebug
	}
	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: level})))
}

func runHealthcheck() error {
	cfg, err := loadConfig()
	if err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	rdb := redis.NewClient(&redis.Options{Addr: cfg.RedisAddr})
	defer rdb.Close()

	key := liquidationcapture.HealthKey(cfg.Exchange)
	res, err := rdb.HGetAll(ctx, key).Result()
	if err != nil {
		return err
	}
	if len(res) == 0 {
		return fmt.Errorf("health key missing for %s", cfg.Exchange)
	}
	switch res["status"] {
	case liquidationcapture.StatusFailed:
		return fmt.Errorf("status is failed: %s", res["reason_codes"])
	case liquidationcapture.StatusStarting, liquidationcapture.StatusOk, liquidationcapture.StatusDegraded:
		// These states are live. Degraded data quality is surfaced separately and
		// may recover without a process restart.
	default:
		return fmt.Errorf("health status is unknown: %q", res["status"])
	}

	updatedStr, ok := res["updated_at_ms"]
	if !ok {
		return fmt.Errorf("health updated_at_ms is missing")
	}
	updatedMs, err := strconv.ParseInt(updatedStr, 10, 64)
	if err != nil || updatedMs <= 0 {
		return fmt.Errorf("health updated_at_ms is invalid")
	}
	age := time.Since(time.UnixMilli(updatedMs))
	if age > 30*time.Second {
		return fmt.Errorf("health key is stale")
	}
	if age < -5*time.Second {
		return fmt.Errorf("health updated_at_ms is in the future")
	}
	return nil
}
