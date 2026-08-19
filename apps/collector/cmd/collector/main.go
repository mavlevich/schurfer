package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/mavlevich/schurfer/collector/internal/bybit"

	"github.com/mavlevich/schurfer/collector/internal/binance"
	"golang.org/x/sync/errgroup"

	"github.com/nats-io/nats.go"
)

func main() {
	if err := run(); err != nil {
		slog.Error("fatal", "err", err)
		os.Exit(1)
	}
}

func run() error {
	level := slog.LevelInfo
	if strings.EqualFold(os.Getenv("LOG_LEVEL"), "debug") {
		level = slog.LevelDebug
	}
	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: level})))

	cfg := loadConfig()

	nc, err := nats.Connect(cfg.NATSUrl,
		nats.MaxReconnects(-1),
		nats.ReconnectWait(2*time.Second),
	)
	if err != nil {
		return fmt.Errorf("nats: %w", err)
	}
	defer func() {
		if err := nc.Drain(); err != nil {
			slog.Warn("nats.drain", "err", err)
		}
	}()
	slog.Info("nats.connected", "url", cfg.NATSUrl)

	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()

	src := bybit.NewSource()
	binanceSrc := binance.NewSource()

	binanceCatalog, err := binanceSrc.FetchSymbolCatalog(ctx)
	if err != nil {
		return fmt.Errorf("fetch binance catalog: %w", err)
	}

	symbols := cfg.Symbols
	if len(symbols) == 0 {
		symbols, err = src.FetchSymbols(ctx)
		if err != nil {
			return fmt.Errorf("fetch symbols: %w", err)
		}
	}
	if len(symbols) == 0 {
		return fmt.Errorf("no symbols to subscribe to")
	}
	slog.Info("collector.starting", "symbols", len(symbols))

	publish := func(ctx context.Context, event bybit.TickerEvent) error {
		_ = ctx
		data, err := json.Marshal(event)
		if err != nil {
			return err
		}
		if err := nc.Publish("market.bybit.ticker."+event.Symbol, data); err != nil {
			return err
		}
		slog.Debug("published", "symbol", event.Symbol, "price", event.LastPrice)
		return nil
	}

	g, gctx := errgroup.WithContext(ctx)

	g.Go(func() error {
		return src.Run(gctx, symbols, publish)
	})

	g.Go(func() error {
		// Calculate rate limit to fetch each symbol once every 5 minutes
		rpm := len(binanceCatalog.CryptoPerpetualSymbols) / 5
		if rpm < 1 {
			rpm = 1
		}

		cfgLSR := binance.LSRSchedulerConfig{
			Workers:            2,
			RateLimitPerMinute: rpm,
		}
		slog.Info("collector.lsr.starting", "symbols", len(binanceCatalog.CryptoPerpetualSymbols), "rpm", rpm)
		return binanceSrc.PollLongShortRatio(gctx, binanceCatalog.CryptoPerpetualSymbols, cfgLSR, func(ctx context.Context, reading binance.LongShortRatioReading) error {
			_ = ctx
			data, err := json.Marshal(reading)
			if err != nil {
				return err
			}
			if err := nc.Publish("binance.lsr."+reading.Symbol, data); err != nil {
				return err
			}
			slog.Debug("published_lsr", "symbol", reading.Symbol, "ratio", reading.Ratio)
			return nil
		})
	})

	return g.Wait()
}

type config struct {
	NATSUrl string
	Symbols []string
}

func loadConfig() config {
	cfg := config{
		NATSUrl: getEnv("NATS_URL", "nats://localhost:4222"),
	}
	if raw := os.Getenv("BYBIT_SYMBOLS"); raw != "" {
		for _, s := range strings.Split(raw, ",") {
			if sym := strings.TrimSpace(s); sym != "" {
				cfg.Symbols = append(cfg.Symbols, sym)
			}
		}
	}
	return cfg
}

func getEnv(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}
