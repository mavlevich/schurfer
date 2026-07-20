package main

import (
	"context"
	"log/slog"
	"os"
	"os/signal"
	"strconv"
	"syscall"
	"time"

	"github.com/mavlevich/schurfer/notifier/internal/notifier"
)

func main() {
	if err := run(); err != nil {
		slog.Error("fatal", "err", err)
		os.Exit(1)
	}
}

func run() error {
	cfg := loadConfig()

	n := notifier.New(cfg)
	defer n.Close()

	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()

	return n.Run(ctx)
}

func loadConfig() notifier.Config {
	interval := 60 * time.Second
	if s := os.Getenv("SCAN_INTERVAL"); s != "" {
		if secs, err := strconv.Atoi(s); err == nil {
			interval = time.Duration(secs) * time.Second
		}
	}
	staleAfter := 240 * time.Second // ~4 missed scans at the default interval
	// Must be below the pumps:latest Redis TTL (300s) so a dead scanner is caught by
	// the aging timestamp before the key disappears; a larger value only delays the
	// alert by an extra grace window, so reject it and keep the default.
	if s := os.Getenv("STALE_AFTER_SECONDS"); s != "" {
		if secs, err := strconv.Atoi(s); err == nil && secs > 0 && secs < 300 {
			staleAfter = time.Duration(secs) * time.Second
		} else {
			slog.Warn("notifier.stale_after.invalid", "value", s, "using_default_seconds", 240)
		}
	}
	// Notifier-side pump gate on top of the scanner's PUMP_MIN_PCT, so the Telegram
	// channel is not flooded by every small pump. 0 alerts on everything the scanner
	// reports; the default only pings on larger moves.
	minPct := 60.0
	if s := os.Getenv("NOTIFY_MIN_PCT"); s != "" {
		if v, err := strconv.ParseFloat(s, 64); err == nil && v >= 0 {
			minPct = v
		} else {
			slog.Warn("notifier.min_pct.invalid", "value", s, "using_default", 60)
		}
	}
	return notifier.Config{
		RedisAddr:  getEnv("REDIS_ADDR", "localhost:6379"),
		BotToken:   os.Getenv("TELEGRAM_BOT_TOKEN"),
		ChatID:     os.Getenv("TELEGRAM_CHAT_ID"),
		Interval:   interval,
		StaleAfter: staleAfter,
		MinPct:     minPct,
	}
}

func getEnv(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}
