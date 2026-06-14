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
	return notifier.Config{
		RedisAddr: getEnv("REDIS_ADDR", "localhost:6379"),
		BotToken:  os.Getenv("TELEGRAM_BOT_TOKEN"),
		ChatID:    os.Getenv("TELEGRAM_CHAT_ID"),
		Interval:  interval,
	}
}

func getEnv(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}
