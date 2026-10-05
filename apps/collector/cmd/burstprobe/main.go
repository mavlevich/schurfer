// Command burstprobe runs the HYP-030 bounded path measurement
// (docs/research/hyp030-path-measurement-v1.md) for a fixed duration and stops.
//
// It streams Bybit linear trades of the run-frozen universe through the shared
// runtime (streamrt), evaluates the burst rule on its own data contract (burstengine),
// and for every signal fetches the entry and exit books and the settled funding
// (burstprobe). Market values go to sealed files only. The Redis health hash and the
// daily summaries carry counters and durations only. It never sends an order and never
// touches the database.
package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"runtime"
	"strings"
	"syscall"
	"time"

	"github.com/redis/go-redis/v9"

	"github.com/mavlevich/schurfer/collector/internal/burstengine"
	"github.com/mavlevich/schurfer/collector/internal/burstprobe"
	"github.com/mavlevich/schurfer/collector/internal/bybit"
	"github.com/mavlevich/schurfer/collector/internal/streamrt"
)

const healthKey = "burstprobe:health"

func main() {
	if err := run(); err != nil {
		slog.Error("burstprobe.failed", "err", err)
		os.Exit(1)
	}
}

type options struct {
	duration     time.Duration
	outDir       string
	redisAddr    string
	restBase     string
	maxOpen      int
	hold         time.Duration
	quoteTimeout time.Duration
}

func parseOptions(args []string) (options, error) {
	fs := flag.NewFlagSet("burstprobe", flag.ContinueOnError)
	var o options
	fs.DurationVar(&o.duration, "duration", 28*24*time.Hour, "run length; the process stops after it")
	fs.StringVar(&o.outDir, "out-dir", "/data/burstprobe", "sealed files, universe and summaries")
	fs.StringVar(&o.redisAddr, "redis-addr", "redis:6379", "Redis for the counters-only health hash (empty: none)")
	fs.StringVar(&o.restBase, "rest-base", "https://api.bybit.com", "Bybit public REST base")
	fs.IntVar(&o.maxOpen, "max-open", 3, "the design's portfolio limit")
	fs.DurationVar(&o.hold, "hold", time.Hour, "exit after the bar's end")
	fs.DurationVar(&o.quoteTimeout, "quote-timeout", 3*time.Second, "per REST request")
	if err := fs.Parse(args); err != nil {
		return o, err
	}
	if o.duration <= 0 || o.duration > 28*24*time.Hour || o.maxOpen <= 0 || o.hold <= 0 {
		return o, errors.New("duration must be in (0, 28 days], max-open and hold positive")
	}
	return o, nil
}

func run() error {
	o, err := parseOptions(os.Args[1:])
	if err != nil {
		return err
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	ctx, cancel := context.WithTimeout(ctx, o.duration)
	defer cancel()

	universe, err := bybit.NewAdapter(bybit.NewSource()).FetchUniverse(ctx)
	if err != nil {
		return fmt.Errorf("universe: %w", err)
	}
	if err := universe.Validate(); err != nil {
		return err
	}
	if err := writeUniverse(o.outDir, universe.IncludedSymbols, universe.ExclusionCounts); err != nil {
		return err
	}
	sealed, err := burstprobe.NewSealed(filepath.Join(o.outDir, "sealed"), burstengine.ContractVersion)
	if err != nil {
		return err
	}
	var rdb *redis.Client
	if o.redisAddr != "" {
		rdb = redis.NewClient(&redis.Options{Addr: o.redisAddr})
		defer func() { _ = rdb.Close() }()
		if err := rdb.Ping(ctx).Err(); err != nil {
			return fmt.Errorf("redis: %w", err)
		}
	}

	rt := streamrt.New(streamrt.BybitCodec{}, streamrt.Config{})
	engine := burstengine.New(burstengine.HYP030())
	probe := burstprobe.New(
		burstprobe.REST{Base: o.restBase, Client: &http.Client{Timeout: o.quoteTimeout}},
		sealed,
		burstprobe.Config{MaxOpen: o.maxOpen, Hold: o.hold, QuoteTimeout: o.quoteTimeout},
	)
	go func() { _ = rt.Run(ctx, universe.IncludedSymbols) }()
	slog.Info("burstprobe.started", "instruments", len(universe.IncludedSymbols),
		"contract", burstengine.ContractVersion, "duration", o.duration)

	tick := time.NewTicker(100 * time.Millisecond)
	defer tick.Stop()
	report := time.NewTicker(10 * time.Second)
	defer report.Stop()
	summaryDay := time.Now().UTC().Format("2006-01-02")
	events := rt.Events()
loop:
	for {
		select {
		case event, ok := <-events:
			if !ok {
				break loop
			}
			engine.OnEvent(event)
		case now := <-tick.C:
			for _, s := range engine.Tick(now) {
				probe.Handle(ctx, s)
			}
		case now := <-report.C:
			counters := health(rt, engine, probe)
			publish(ctx, rdb, counters, now)
			if day := now.UTC().Format("2006-01-02"); day != summaryDay {
				writeSummary(o.outDir, summaryDay, counters)
				summaryDay = day
			}
		case <-ctx.Done():
			break loop
		}
	}
	probe.Wait()
	counters := health(rt, engine, probe)
	writeSummary(o.outDir, summaryDay, counters)
	publish(context.Background(), rdb, counters, time.Now())
	slog.Info("burstprobe.stopped", "signals", counters["signals"])
	return sealed.Close()
}

// health merges the runtime, engine and probe counters; no instrument, no market value.
func health(rt *streamrt.Runtime, engine *burstengine.Engine, probe *burstprobe.Probe) map[string]int64 {
	out := probe.Health()
	out["frames"] = rt.Stats.Frames.Load()
	out["bytes"] = rt.Stats.Bytes.Load()
	out["trades_received"] = rt.Stats.Trades.Load()
	out["acks"] = rt.Stats.Acks.Load()
	out["dropped"] = rt.Stats.Dropped.Load()
	out["sessions"] = rt.Stats.Sessions.Load()
	out["disconnects"] = rt.Stats.Disconnects.Load()
	out["parse_errors"] = rt.Stats.ParseErrors.Load()
	s := engine.Stats
	out["engine_trades"] = s.Trades
	out["missing_id"] = s.MissingID
	out["duplicates"] = s.Duplicates
	out["late_trades"] = s.LateTrades
	out["bars"] = s.Bars
	out["empty_bars"] = s.EmptyBars
	out["incomplete_bars"] = s.IncompleteBars
	out["suppressed_by_gap"] = s.SuppressedByGap
	out["engine_signals"] = s.Signals
	out["peak_rss_bytes"] = peakRSS()
	return out
}

func peakRSS() int64 {
	var usage syscall.Rusage
	if syscall.Getrusage(syscall.RUSAGE_SELF, &usage) != nil {
		return 0
	}
	if runtime.GOOS == "darwin" {
		return usage.Maxrss // bytes on macOS
	}
	return usage.Maxrss * 1024 // KiB on Linux
}

func publish(ctx context.Context, rdb *redis.Client, counters map[string]int64, now time.Time) {
	if rdb == nil {
		slog.Info("burstprobe.health", "counters", counters)
		return
	}
	values := make(map[string]any, len(counters)+1)
	for k, v := range counters {
		values[k] = v
	}
	values["updated_at"] = now.UTC().Format(time.RFC3339)
	pipe := rdb.TxPipeline()
	pipe.HSet(ctx, healthKey, values)
	pipe.Expire(ctx, healthKey, 10*time.Minute)
	if _, err := pipe.Exec(ctx); err != nil {
		slog.Warn("burstprobe.health_publish_failed", "err", err)
	}
}

func writeSummary(dir, day string, counters map[string]int64) {
	body, err := json.MarshalIndent(map[string]any{"day": day, "counters": counters}, "", " ")
	if err == nil {
		err = os.MkdirAll(filepath.Join(dir, "summary"), 0o750)
	}
	if err == nil {
		err = os.WriteFile(filepath.Join(dir, "summary", "summary-"+day+".json"), append(body, '\n'), 0o600)
	}
	if err != nil {
		slog.Warn("burstprobe.summary_failed", "err", err)
	}
}

// writeUniverse freezes the run's universe with its sha256 and exclusion counts.
func writeUniverse(dir string, symbols []string, excluded map[string]int) error {
	if err := os.MkdirAll(dir, 0o750); err != nil {
		return err
	}
	sum := sha256.Sum256([]byte(strings.Join(symbols, "\n")))
	body, err := json.MarshalIndent(map[string]any{
		"contract": burstengine.ContractVersion, "captured_at": time.Now().UTC(),
		"symbols": symbols, "symbols_sha256": hex.EncodeToString(sum[:]), "excluded": excluded,
	}, "", " ")
	if err != nil {
		return err
	}
	name := filepath.Join(dir, "universe-"+time.Now().UTC().Format("20060102T150405Z")+".json")
	return os.WriteFile(name, append(body, '\n'), 0o600)
}
