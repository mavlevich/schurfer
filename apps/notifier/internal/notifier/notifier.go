package notifier

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"sync"
	"time"

	"github.com/redis/go-redis/v9"
)

const (
	redisKeyPumps     = "pumps:latest"
	redisKeyHeartbeat = "notifier:heartbeat"
	redisKeySeenPfx   = "notifier:seen:"
	seenTTL           = 24 * time.Hour
)

type Config struct {
	RedisAddr string
	BotToken  string
	ChatID    string
	Interval  time.Duration
}

type Notifier struct {
	cfg Config
	rdb *redis.Client
}

func New(cfg Config) *Notifier {
	rdb := redis.NewClient(&redis.Options{Addr: cfg.RedisAddr})
	return &Notifier{cfg: cfg, rdb: rdb}
}

func (n *Notifier) Close() {
	_ = n.rdb.Close()
}

func (n *Notifier) Run(ctx context.Context) error {
	if n.cfg.BotToken == "" || n.cfg.ChatID == "" {
		slog.Warn("notifier.disabled", "reason", "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set")
		<-ctx.Done()
		return nil
	}

	slog.Info("notifier.starting", "interval", n.cfg.Interval)

	if err := n.tick(ctx); err != nil {
		slog.Warn("notifier.tick.failed", "err", err)
	}

	ticker := time.NewTicker(n.cfg.Interval)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return nil
		case <-ticker.C:
			if err := n.tick(ctx); err != nil {
				slog.Warn("notifier.tick.failed", "err", err)
			}
		}
	}
}

func (n *Notifier) tick(ctx context.Context) error {
	raw, err := n.rdb.Get(ctx, redisKeyPumps).Bytes()
	if err != nil && err != redis.Nil {
		return fmt.Errorf("redis.get: %w", err)
	}

	// Redis is reachable — notifier is alive (bot configured, checked in Run)
	_ = n.rdb.Set(ctx, redisKeyHeartbeat, time.Now().Unix(), 3*n.cfg.Interval).Err()

	if err == redis.Nil {
		return nil // scanner hasn't produced a snapshot yet
	}

	var p payload
	if err := json.Unmarshal(raw, &p); err != nil {
		return fmt.Errorf("json.unmarshal: %w", err)
	}

	// All exchanges failed — skip alerts, heartbeat already written above
	if len(p.Scanned) == 0 {
		return nil
	}

	newPumps := make([]pump, 0)
	for _, pump := range p.Pumps {
		key := redisKeySeenPfx + pump.Base
		exists, err := n.rdb.Exists(ctx, key).Result()
		if err != nil {
			slog.Warn("notifier.seen.check.failed", "base", pump.Base, "err", err)
			continue
		}
		if exists == 0 {
			newPumps = append(newPumps, pump)
		}
	}

	if len(newPumps) == 0 {
		return nil
	}

	var mu sync.Mutex
	sent := make([]pump, 0, len(newPumps))

	var wg sync.WaitGroup
	sem := make(chan struct{}, 4)

	for _, entry := range newPumps {
		wg.Add(1)
		go func(p pump) {
			defer wg.Done()
			sem <- struct{}{}
			defer func() { <-sem }()

			if err := sendAlert(ctx, p, n.cfg.BotToken, n.cfg.ChatID); err != nil {
				slog.Warn("notifier.alert.failed", "base", p.Base, "err", err)
				return
			}
			slog.Info("notifier.alert.sent", "base", p.Base, "pct", p.MaxChangePct)
			mu.Lock()
			sent = append(sent, p)
			mu.Unlock()
		}(entry)
	}
	wg.Wait()

	for _, sp := range sent {
		key := redisKeySeenPfx + sp.Base
		if err := n.rdb.Set(ctx, key, 1, seenTTL).Err(); err != nil {
			slog.Warn("notifier.seen.set.failed", "base", sp.Base, "err", err)
		}
	}

	return nil
}
