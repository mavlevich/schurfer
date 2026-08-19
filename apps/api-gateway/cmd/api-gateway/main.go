package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/go-chi/chi/v5/middleware"
	"github.com/mavlevich/schurfer/api-gateway/internal/auth"
	"github.com/mavlevich/schurfer/api-gateway/internal/decisions"
	"github.com/mavlevich/schurfer/api-gateway/internal/execution"
	"github.com/mavlevich/schurfer/api-gateway/internal/health"
	"github.com/mavlevich/schurfer/api-gateway/internal/pumps"
	"github.com/mavlevich/schurfer/api-gateway/internal/research"
	"github.com/mavlevich/schurfer/api-gateway/internal/trades"
	"github.com/mavlevich/schurfer/api-gateway/internal/ws"
	"github.com/redis/go-redis/v9"
)

const (
	measurementPumpsKey = "pumps:measurement"
	publicPumpsKey      = "pumps:latest"
)

func main() {
	if err := run(); err != nil {
		slog.Error("fatal", "err", err)
		os.Exit(1)
	}
}

func run() error {
	logger := slog.New(slog.NewJSONHandler(os.Stdout, nil))
	slog.SetDefault(logger)

	cfg := loadConfig()

	checker, err := health.NewChecker(context.Background(), health.Config{
		PostgresDSN:        cfg.PostgresDSN,
		RedisAddr:          cfg.RedisAddr,
		NATSUrl:            cfg.NATSUrl,
		RuntimeMetricsPath: cfg.RuntimeMetricsPath,
		DiskUsagePath:      cfg.DiskUsagePath,
	})
	if err != nil {
		return fmt.Errorf("health checker: %w", err)
	}
	defer checker.Close()

	authHandler := auth.NewHandler(auth.Config{
		PasswordHash: cfg.PasswordHash,
		JWTSecret:    cfg.JWTSecret,
		TokenTTL:     7 * 24 * time.Hour,
		Secure:       cfg.Env == "production",
	})

	rdb := redis.NewClient(&redis.Options{Addr: cfg.RedisAddr})
	defer func() { _ = rdb.Close() }()

	healthHandler := health.NewHandler(checker)
	wsHandler := ws.NewHandler(checker, 5*time.Second)
	pumpsHandler := pumps.NewHandler(rdb, checker.Pool())
	accountHandler := execution.NewHandler(cfg.ExecutionURL)
	tradesHandler := trades.NewHandler(checker.Pool())
	decisionsHandler := decisions.NewHandler(checker.Pool())
	researchHandler := research.NewHandler(checker.Pool(), rdb)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	go runSignalsTicker(ctx, rdb, pumpsHandler)

	r := chi.NewRouter()
	r.Use(middleware.RequestID)
	r.Use(middleware.Recoverer)

	// Public — no auth required.
	r.Post("/auth/login", authHandler.Login)
	r.Get("/healthz", healthHandler.Liveness) // liveness probe: always 200

	// Protected — JWT cookie required.
	r.Group(func(r chi.Router) {
		r.Use(auth.Middleware(cfg.JWTSecret))

		r.Post("/auth/logout", authHandler.Logout)
		r.Get("/api/health", healthHandler.Health)
		r.Get("/api/pumps", pumpsHandler.List)
		r.Get("/api/pumps/history", pumpsHandler.History)
		r.Get("/api/pumps/momentum-watch", pumpsHandler.MomentumWatch)
		r.Get("/api/pumps/{base}", pumpsHandler.Token)
		r.Get("/api/pumps/{base}/ohlcv", pumpsHandler.OHLCV)
		r.Get("/api/pumps/{base}/history", pumpsHandler.TokenHistory)
		r.Get("/api/pumps/{base}/oi", pumpsHandler.OI)
		r.Get("/api/pumps/{base}/funding", pumpsHandler.Funding)
		r.Get("/api/pumps/{base}/stats", pumpsHandler.Stats)
		r.Get("/api/pumps/{base}/signals", pumpsHandler.Signals)

		r.Get("/api/trades", tradesHandler.List)
		r.Get("/api/trades/stats", tradesHandler.Stats)
		r.Get("/api/decisions", decisionsHandler.List)
		r.Get("/api/research/readiness", researchHandler.Readiness)

		r.Get("/api/account/balance", accountHandler.ServeHTTP)
		r.Get("/api/account/positions", accountHandler.ServeHTTP)
		r.Get("/api/account/risk", accountHandler.ServeHTTP)
		r.Post("/api/account/order", accountHandler.ServeHTTP)
		r.Post("/api/account/positions/close", accountHandler.ServeHTTP)
		r.Post("/api/account/stop", accountHandler.ServeHTTP)
		r.Post("/api/account/resume", accountHandler.ServeHTTP)

		r.Get("/ws/status", wsHandler.Status)
	})

	// Use explicit http.Server to set read/write timeouts.
	// WebSocket connections are hijacked before these timeouts apply.
	srv := &http.Server{
		Addr:         ":" + cfg.Port,
		Handler:      r,
		ReadTimeout:  15 * time.Second,
		WriteTimeout: 30 * time.Second,
		IdleTimeout:  120 * time.Second,
	}

	slog.Info("starting api-gateway", "addr", srv.Addr, "env", cfg.Env)

	if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		return fmt.Errorf("server: %w", err)
	}
	return nil
}

func loadSignalBases(ctx context.Context, rdb *redis.Client) ([]string, error) {
	raw, err := rdb.Get(ctx, measurementPumpsKey).Bytes()
	if errors.Is(err, redis.Nil) {
		// Rolling-deploy compatibility until analytics publishes the private feed.
		raw, err = rdb.Get(ctx, publicPumpsKey).Bytes()
	}
	if err != nil {
		return nil, err
	}
	var payload struct {
		Pumps []struct {
			Base string `json:"base"`
		} `json:"pumps"`
	}
	if err := json.Unmarshal(raw, &payload); err != nil {
		return nil, err
	}
	bases := make([]string, 0, len(payload.Pumps))
	for _, pump := range payload.Pumps {
		if pump.Base != "" {
			bases = append(bases, pump.Base)
		}
	}
	return bases, nil
}

// runSignalsTicker refreshes signals:{base} in Redis every minute for all measured
// pumps. The public feed remains filtered at the entry floor for UI and notifications.
// Runs immediately on start so the first tick has data, then every 60 seconds.
func runSignalsTicker(ctx context.Context, rdb *redis.Client, h *pumps.Handler) {
	refresh := func() {
		bases, err := loadSignalBases(ctx, rdb)
		if err != nil {
			if !errors.Is(err, redis.Nil) {
				slog.Warn("signals.ticker.load_error", "err", err)
			}
			return
		}
		for _, base := range bases {
			if err := h.CacheSignals(ctx, base); err != nil {
				slog.Warn("signals.ticker.cache_error", "base", base, "err", err)
			}
		}
	}

	refresh()
	ticker := time.NewTicker(60 * time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			refresh()
		}
	}
}

type config struct {
	PostgresDSN        string
	RedisAddr          string
	NATSUrl            string
	PasswordHash       string
	JWTSecret          string
	Port               string
	Env                string
	ExecutionURL       string
	RuntimeMetricsPath string
	DiskUsagePath      string
}

func loadConfig() config {
	return config{
		PostgresDSN:  getEnv("DATABASE_URL", "postgresql://schurfer:schurfer_dev@localhost:5432/schurfer"),
		RedisAddr:    getEnv("REDIS_ADDR", "localhost:6379"),
		NATSUrl:      getEnv("NATS_URL", "nats://localhost:4222"),
		PasswordHash: mustEnv("ADMIN_PASSWORD_HASH"),
		JWTSecret:    mustEnv("JWT_SECRET"),
		Port:         getEnv("PORT", "8000"),
		Env:          getEnv("ENV", "development"),
		ExecutionURL: getEnv("EXECUTION_URL", "http://localhost:8001"),
		RuntimeMetricsPath: getEnv(
			"RUNTIME_METRICS_PATH",
			"/runtime/container-metrics.snapshot",
		),
		DiskUsagePath: getEnv(
			"DISK_USAGE_PATH",
			"/runtime/disk-usage.snapshot",
		),
	}
}

func getEnv(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

func mustEnv(key string) string {
	v := os.Getenv(key)
	if v == "" {
		slog.Error("required env var not set", "key", key)
		os.Exit(1)
	}
	return v
}
