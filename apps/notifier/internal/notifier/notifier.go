package notifier

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"sync"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/redis/go-redis/v9"
)

const (
	redisKeyPumps             = "pumps:latest"
	redisKeyHeartbeat         = "notifier:heartbeat"
	redisKeySeenPfx           = "notifier:seen:"
	redisKeyReopenCooldownPfx = "notifier:reopen_cooldown:"
	redisKeyStaleAlerted      = "notifier:stale_alerted"
	redisKeyMissingSince      = "notifier:pumps_missing_since"
	redisKeyAlertOutbox       = "notifier:alert_delivery_outbox"
	redisKeyAlertDLQ          = "notifier:alert_delivery_dlq"
	// Event ids are immutable and episodes may stay above the scanner threshold
	// longer than one day. Retain the compact de-dup key well beyond a normal
	// episode without growing Redis indefinitely.
	seenTTL = 30 * 24 * time.Hour
	// A pump on a thin or flaky venue can drop out of the scanner's live list for
	// a few scan ticks and reappear as a brand-new pump_event_id (app.pump_events
	// closes an episode after PUMP_CLOSE_AFTER_MISSES consecutive absences and
	// opens a fresh one on the next detection) even though the real move never
	// stopped. The per-event seenTTL key above treats that new id as unseen and
	// re-alerts. Observed on 2026-08-03: CATE/LBank sent 4 alerts across ~70
	// minutes for one continuous move, with gaps up to 37 minutes between
	// reopens. reopenCooldown is set above that observed gap (with margin) and is
	// refreshed on every reopen it suppresses, so it keeps sliding for as long as
	// the base keeps reopening and only lets a new alert through once the base
	// has been fully quiet for the whole window. This does not touch
	// app.pump_events' own episode-lifecycle semantics or its miss-count
	// threshold — only notifier-side alerting.
	reopenCooldown = 45 * time.Minute
)

type Config struct {
	RedisAddr   string
	DatabaseURL string
	BotToken    string
	ChatID      string
	Interval    time.Duration
	// StaleAfter is how old the last scan may be before the scanner is reported
	// stale. A silently dead scanner is the main way the dataset develops gaps.
	StaleAfter time.Duration
	// MinPct is the smallest max-change percent a pump must reach to trigger a
	// Telegram alert. The public scanner feed is already filtered at
	// PUMP_ENTRY_MIN_PCT; this is
	// a second, notifier-side gate that keeps the channel from being flooded by
	// every small pump. A sub-threshold pump is not marked seen, so it still alerts
	// if it later grows past the gate.
	MinPct float64
}

type Notifier struct {
	cfg              Config
	rdb              *redis.Client
	recorder         alertRecorder
	sourceLeadHealth sourceLeadHealthReader
	momentumFlow     momentumFlowReader
	consumer         *StreamConsumer
}

func New(ctx context.Context, cfg Config) (*Notifier, error) {
	rdb := redis.NewClient(&redis.Options{Addr: cfg.RedisAddr})
	notifier := &Notifier{cfg: cfg, rdb: rdb}
	if cfg.DatabaseURL != "" {
		postgresRecorder, err := newPostgresAlertRecorder(ctx, cfg.DatabaseURL)
		if err != nil {
			_ = rdb.Close()
			return nil, fmt.Errorf("alert recorder: %w", err)
		}
		notifier.recorder = postgresRecorder
		notifier.sourceLeadHealth = postgresRecorder
		notifier.momentumFlow = postgresRecorder
		pool, ok := postgresRecorder.pool.(*pgxpool.Pool)
		if ok {
			notifier.consumer = NewStreamConsumer(rdb, pool, cfg.BotToken, cfg.ChatID)
		}
	}
	return notifier, nil
}

func (n *Notifier) Close() {
	_ = n.rdb.Close()
	if n.recorder != nil {
		n.recorder.Close()
	}
}

func (n *Notifier) Run(ctx context.Context) error {
	if n.cfg.BotToken == "" || n.cfg.ChatID == "" {
		slog.Warn("notifier.disabled", "reason", "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set")
		<-ctx.Done()
		return nil
	}

	slog.Info("notifier.starting", "interval", n.cfg.Interval)

	if n.consumer != nil {
		go n.consumer.Run(ctx)
	}

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
		if err := sendMessage(ctx, "🔴 Schurfer scanner stale: "+reason, n.cfg.BotToken, n.cfg.ChatID); err != nil {
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
	if err := sendMessage(ctx, "🟢 Schurfer scanner recovered", n.cfg.BotToken, n.cfg.ChatID); err != nil {
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
	n.drainAlertOutbox(ctx)
	n.reportSourceLeadHealth(ctx)
	n.reportMomentumFlow(ctx)

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
	scanPublishedAt := payloadPublishedAt(p)

	newPumps := make([]pump, 0)
	for _, pump := range p.Pumps {
		// Below the notifier gate: skip without marking seen, so the same token
		// still alerts if a later scan shows it grown past the threshold.
		if pump.MaxChangePct < n.cfg.MinPct {
			continue
		}
		exists, err := n.rdb.Exists(ctx, seenKeys(pump)...).Result()
		if err != nil {
			slog.Warn("notifier.seen.check.failed", "base", pump.Base, "err", err)
			continue
		}
		if exists > 0 {
			continue
		}
		// A new event id for this base — could be a genuinely new pump, or a
		// reopen of one that briefly dropped out of the scan (see reopenCooldown).
		inCooldown, err := n.extendReopenCooldown(ctx, pump.Base)
		if err != nil {
			slog.Warn("notifier.reopen_cooldown.check.failed", "base", pump.Base, "err", err)
			continue
		}
		if inCooldown {
			continue
		}
		newPumps = append(newPumps, pump)
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

			startedAt := time.Now().UTC()
			if err := sendAlert(ctx, p, n.cfg.BotToken, n.cfg.ChatID); err != nil {
				slog.Warn("notifier.alert.failed", "base", p.Base, "err", err)
				return
			}
			sentAt := time.Now().UTC()
			delivery := deliveryFrom(p, n.cfg.MinPct, scanPublishedAt, startedAt, sentAt)
			if n.recorder != nil && p.PumpEventID > 0 {
				n.recordOrEnqueueAlert(ctx, delivery)
			}
			slog.Info(
				"notifier.alert.sent",
				"base", p.Base,
				"pump_event_id", p.PumpEventID,
				"pct", p.MaxChangePct,
				"observation_to_notification_ms",
				sentAt.Sub(delivery.ScannerObservedAt).Milliseconds(),
				"publish_to_notification_ms",
				sentAt.Sub(delivery.ScanPublishedAt).Milliseconds(),
			)
			mu.Lock()
			sent = append(sent, p)
			mu.Unlock()
		}(entry)
	}
	wg.Wait()

	for _, sp := range sent {
		key := seenKey(sp)
		if err := n.rdb.Set(ctx, key, 1, seenTTL).Err(); err != nil {
			slog.Warn("notifier.seen.set.failed", "base", sp.Base, "err", err)
		}
	}

	return nil
}

// extendReopenCooldown reports whether base is still within its reopen
// cooldown window, and unconditionally (re)sets the key with a fresh TTL —
// so a base that keeps reopening keeps sliding the window forward, and only
// clears it once genuinely quiet for the full reopenCooldown duration.
func (n *Notifier) extendReopenCooldown(ctx context.Context, base string) (bool, error) {
	key := redisKeyReopenCooldownPfx + base
	existed, err := n.rdb.Exists(ctx, key).Result()
	if err != nil {
		return false, err
	}
	if err := n.rdb.Set(ctx, key, 1, reopenCooldown).Err(); err != nil {
		return false, err
	}
	return existed > 0, nil
}

func seenKey(p pump) string {
	if p.PumpEventID > 0 {
		return fmt.Sprintf("%s%d", redisKeySeenPfx, p.PumpEventID)
	}
	return redisKeySeenPfx + p.Base
}

func seenKeys(p pump) []string {
	if p.PumpEventID > 0 {
		// Read the old base-scoped key during the rollout so deploying this schema
		// does not re-alert every currently active token. New writes are event-scoped.
		return []string{seenKey(p), redisKeySeenPfx + p.Base}
	}
	return []string{seenKey(p)}
}

func payloadPublishedAt(p payload) time.Time {
	publishedAtMS := p.PublishedAtMS
	if publishedAtMS == 0 {
		publishedAtMS = p.Ts
	}
	return time.UnixMilli(publishedAtMS).UTC()
}

func deliveryFrom(
	p pump,
	thresholdPct float64,
	scanPublishedAt time.Time,
	startedAt time.Time,
	sentAt time.Time,
) alertDelivery {
	top := topExchange(p.Exchanges)
	scannerObservedAt := scanPublishedAt
	var tickerAt *time.Time
	var exchangeName string
	var high24h *float64
	if top != nil {
		exchangeName = top.Exchange
		if top.ScannerObservedAt != nil {
			scannerObservedAt = time.UnixMilli(*top.ScannerObservedAt).UTC()
		}
		if top.TickerTimestamp != nil {
			value := time.UnixMilli(*top.TickerTimestamp).UTC()
			tickerAt = &value
		}
		value := exchangeHigh24hPct(*top)
		if value > 0 {
			high24h = &value
		}
	}
	return alertDelivery{
		EventID:               p.PumpEventID,
		Base:                  p.Base,
		Exchange:              exchangeName,
		ThresholdPct:          thresholdPct,
		ObservedChangePct:     p.MaxChangePct,
		Exchange24hHighPct:    high24h,
		TickerAt:              tickerAt,
		ScannerObservedAt:     scannerObservedAt,
		ScanPublishedAt:       scanPublishedAt,
		NotificationStartedAt: startedAt,
		NotificationSentAt:    sentAt,
	}
}

func topExchange(exchanges []exchange) *exchange {
	if len(exchanges) == 0 {
		return nil
	}
	top := &exchanges[0]
	for i := 1; i < len(exchanges); i++ {
		if exchanges[i].ChangePct > top.ChangePct {
			top = &exchanges[i]
		}
	}
	return top
}
