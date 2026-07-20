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
	redisKeyPumps        = "pumps:latest"
	redisKeyHeartbeat    = "notifier:heartbeat"
	redisKeySeenPfx      = "notifier:seen:"
	redisKeyStaleAlerted = "notifier:stale_alerted"
	redisKeyMissingSince = "notifier:pumps_missing_since"
	seenTTL              = 24 * time.Hour
)

type Config struct {
	RedisAddr string
	BotToken  string
	ChatID    string
	Interval  time.Duration
	// StaleAfter is how old the last scan may be before the scanner is reported
	// stale. A silently dead scanner is the main way the dataset develops gaps.
	StaleAfter time.Duration
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

// reportMissing handles a missing pumps:latest with a grace window, so a cold
// start (the scanner has not written its first snapshot yet) does not cry wolf.
// It alerts only once the key has been absent for StaleAfter. A scanner that
// dies while running is caught earlier by the aging timestamp path, before its
// pumps:latest TTL expires.
func (n *Notifier) reportMissing(ctx context.Context) {
	now := time.Now().Unix()
	since, err := n.rdb.Get(ctx, redisKeyMissingSince).Int64()
	switch {
	case err == redis.Nil:
		if serr := n.rdb.Set(ctx, redisKeyMissingSince, now, 0).Err(); serr != nil {
			slog.Warn("notifier.missing.mark.failed", "err", serr)
		}
		since = now
	case err != nil:
		slog.Warn("notifier.missing.check.failed", "err", err)
		return
	}
	if now-since >= int64(n.cfg.StaleAfter.Seconds()) {
		n.reportStaleness(ctx, true, "pumps:latest missing (scanner produced nothing)")
	}
}

// reportStaleness sends an edge-triggered Telegram alert when the scanner is
// unhealthy, and a recovery notice when it is fine again. The flag lives in
// Redis so it survives a notifier restart. It claims the flag before sending and
// releases it if the send fails, so a Redis or Telegram error does not turn into
// a stream of repeated alerts (nor a permanently swallowed one).
func (n *Notifier) reportStaleness(ctx context.Context, stale bool, reason string) {
	if stale {
		claimed, err := n.rdb.SetNX(ctx, redisKeyStaleAlerted, time.Now().Unix(), 0).Result()
		if err != nil {
			slog.Warn("notifier.stale.claim.failed", "err", err)
			return
		}
		if !claimed {
			return // already alerted
		}
		if err := sendMessage(ctx, "Schurfer scanner stale: "+reason, n.cfg.BotToken, n.cfg.ChatID); err != nil {
			slog.Warn("notifier.stale.alert.failed", "err", err)
			if derr := n.rdb.Del(ctx, redisKeyStaleAlerted).Err(); derr != nil {
				slog.Warn("notifier.stale.claim.release.failed", "err", derr)
			}
			return
		}
		slog.Warn("notifier.stale.detected", "reason", reason)
		return
	}

	// Fresh again: release the flag first, then send recovery. If the send fails,
	// restore the flag so recovery is retried on the next tick.
	removed, err := n.rdb.Del(ctx, redisKeyStaleAlerted).Result()
	if err != nil {
		slog.Warn("notifier.stale.clear.failed", "err", err)
		return
	}
	if removed == 0 {
		return // was not in an alerted state
	}
	if err := sendMessage(ctx, "Schurfer scanner recovered", n.cfg.BotToken, n.cfg.ChatID); err != nil {
		slog.Warn("notifier.stale.recovery.failed", "err", err)
		if serr := n.rdb.Set(ctx, redisKeyStaleAlerted, time.Now().Unix(), 0).Err(); serr != nil {
			slog.Warn("notifier.stale.flag.restore.failed", "err", serr)
		}
		return
	}
	slog.Info("notifier.stale.recovered")
}

func (n *Notifier) tick(ctx context.Context) error {
	raw, err := n.rdb.Get(ctx, redisKeyPumps).Bytes()
	if err != nil && err != redis.Nil {
		return fmt.Errorf("redis.get: %w", err)
	}

	// Redis is reachable — notifier is alive (bot configured, checked in Run)
	_ = n.rdb.Set(ctx, redisKeyHeartbeat, time.Now().Unix(), 3*n.cfg.Interval).Err()

	// A silently dead, stuck, or garbage-producing scanner is the main way the
	// dataset develops gaps, so alert on those before doing anything else.
	if err == redis.Nil {
		n.reportMissing(ctx)
		return nil
	}
	// Key is present, so cancel any missing-grace timer.
	if derr := n.rdb.Del(ctx, redisKeyMissingSince).Err(); derr != nil {
		slog.Warn("notifier.missing.clear.failed", "err", derr)
	}

	var p payload
	if err := json.Unmarshal(raw, &p); err != nil {
		n.reportStaleness(ctx, true, "pumps:latest contains invalid JSON")
		return fmt.Errorf("json.unmarshal: %w", err)
	}

	if p.Ts == 0 {
		n.reportStaleness(ctx, true, "scan timestamp missing")
		return nil
	}
	ageSec := time.Now().Unix() - p.Ts/1000
	switch {
	case ageSec < -5:
		// Fail closed on a timestamp from the future (clock skew or junk) rather
		// than treating a negative age as fresh forever.
		n.reportStaleness(ctx, true, "scan timestamp is in the future")
		return nil
	case ageSec >= int64(n.cfg.StaleAfter.Seconds()):
		n.reportStaleness(ctx, true, fmt.Sprintf("last scan %ds ago", ageSec))
		return nil
	}
	n.reportStaleness(ctx, false, "")

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
