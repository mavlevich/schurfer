package main

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/go-chi/chi/v5/middleware"
	"github.com/mavlevich/schurfer/api-gateway/internal/auth"
	"github.com/mavlevich/schurfer/api-gateway/internal/health"
	"github.com/mavlevich/schurfer/api-gateway/internal/pumps"
	"github.com/mavlevich/schurfer/api-gateway/internal/ws"
	"github.com/redis/go-redis/v9"
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
		PostgresDSN: cfg.PostgresDSN,
		RedisAddr:   cfg.RedisAddr,
		NATSUrl:     cfg.NATSUrl,
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

	r := chi.NewRouter()
	r.Use(middleware.RequestID)
	r.Use(middleware.RealIP)
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
		r.Get("/api/pumps/{base}", pumpsHandler.Token)
		r.Get("/api/pumps/{base}/ohlcv", pumpsHandler.OHLCV)
		r.Get("/api/pumps/{base}/history", pumpsHandler.TokenHistory)
		r.Get("/api/pumps/{base}/oi", pumpsHandler.OI)
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

type config struct {
	PostgresDSN  string
	RedisAddr    string
	NATSUrl      string
	PasswordHash string
	JWTSecret    string
	Port         string
	Env          string
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
