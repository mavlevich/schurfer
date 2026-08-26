package notifier

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/redis/go-redis/v9"
)

const (
	liquidationCaptureMonitorInterval     = 30 * time.Second
	liquidationCaptureMissingGraceDefault = 90 * time.Second
	liquidationCaptureIncidentRetention   = 30 * 24 * time.Hour
)

type liquidationCaptureMonitor struct {
	notifier     *Notifier
	exchanges    []string
	missingGrace time.Duration
	now          func() time.Time
}

func newLiquidationCaptureMonitor(n *Notifier) *liquidationCaptureMonitor {
	exchanges := parseMonitoredExchanges(os.Getenv("LIQUIDATION_CAPTURE_MONITORED_EXCHANGES"))
	missingGrace := liquidationCaptureMissingGraceDefault
	if raw := strings.TrimSpace(os.Getenv("LIQUIDATION_CAPTURE_MONITOR_START_GRACE_SECONDS")); raw != "" {
		seconds, err := strconv.Atoi(raw)
		if err != nil || seconds < 0 {
			slog.Warn("liquidation_capture_monitor.invalid_start_grace", "value", raw)
		} else {
			missingGrace = time.Duration(seconds) * time.Second
		}
	}
	return &liquidationCaptureMonitor{
		notifier: n, exchanges: exchanges, missingGrace: missingGrace, now: time.Now,
	}
}

func parseMonitoredExchanges(raw string) []string {
	seen := make(map[string]struct{})
	var exchanges []string
	for _, value := range strings.Split(raw, ",") {
		exchange := strings.ToLower(strings.TrimSpace(value))
		if exchange != "bybit" && exchange != "binance" {
			continue
		}
		if _, exists := seen[exchange]; exists {
			continue
		}
		seen[exchange] = struct{}{}
		exchanges = append(exchanges, exchange)
	}
	return exchanges
}

func (m *liquidationCaptureMonitor) Run(ctx context.Context) {
	if len(m.exchanges) == 0 {
		slog.Info("liquidation_capture_monitor.disabled")
		return
	}
	m.checkHealth(ctx)
	ticker := time.NewTicker(liquidationCaptureMonitorInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			m.checkHealth(ctx)
		}
	}
}

func (m *liquidationCaptureMonitor) checkHealth(ctx context.Context) {
	for _, exchange := range m.exchanges {
		m.checkFatalIncidents(ctx, exchange)
		m.checkExchange(ctx, exchange)
	}
}

func liquidationHealthKey(exchange string) string {
	return "market:liquidationcapture:health:" + exchange
}

func liquidationIncidentIndexKey(exchange string) string {
	return "market:liquidationcapture:incidents:" + exchange
}

func liquidationIncidentKey(exchange, sessionID string) string {
	return "market:liquidationcapture:incident:" + exchange + ":" + sessionID
}

func liquidationMonitorStateKey(exchange string) string {
	return "notifier:liquidation_capture_health_state:" + exchange
}

func liquidationTransitionClaimKey(exchange, transitionID string) string {
	return "notifier:liquidation_capture_transition_seen:" + exchange + ":" + transitionID
}

func liquidationIncidentClaimKey(exchange, sessionID string) string {
	return "notifier:liquidation_capture_incident_seen:" + exchange + ":" + sessionID
}

func (m *liquidationCaptureMonitor) checkFatalIncidents(ctx context.Context, exchange string) {
	now := m.now().UTC()
	sessions, err := m.notifier.rdb.ZRangeByScore(
		ctx,
		liquidationIncidentIndexKey(exchange),
		&redis.ZRangeBy{
			Min: fmt.Sprintf("%d", now.Add(-liquidationCaptureIncidentRetention).UnixMilli()),
			Max: "+inf",
		},
	).Result()
	if err != nil {
		slog.Error("liquidation_capture_monitor.incident_index_failed", "exchange", exchange, "err", err)
		return
	}
	for _, sessionID := range sessions {
		incident, readErr := m.notifier.rdb.HGetAll(
			ctx, liquidationIncidentKey(exchange, sessionID),
		).Result()
		if readErr != nil {
			slog.Error(
				"liquidation_capture_monitor.incident_read_failed",
				"exchange", exchange, "session", sessionID, "err", readErr,
			)
			continue
		}
		if len(incident) == 0 {
			continue
		}
		published, newlyPublished := m.publishFatalIncident(
			ctx, exchange, sessionID, incident["reason_codes"],
		)
		if published && newlyPublished {
			m.storeMonitorState(
				ctx,
				liquidationMonitorStateKey(exchange),
				"critical",
				"fatal:"+sessionID,
			)
		}
	}
}

func (m *liquidationCaptureMonitor) publishFatalIncident(
	ctx context.Context,
	exchange string,
	sessionID string,
	reasonCodes string,
) (published bool, newlyPublished bool) {
	if sessionID == "" {
		return false, false
	}
	claimKey := liquidationIncidentClaimKey(exchange, sessionID)
	claimed, err := m.notifier.rdb.SetNX(
		ctx, claimKey, m.now().UTC().UnixMilli(), liquidationCaptureIncidentRetention,
	).Result()
	if err != nil {
		slog.Error("liquidation_capture_monitor.incident_claim_failed", "exchange", exchange, "err", err)
		return false, false
	}
	if !claimed {
		return true, false
	}

	message := fmt.Sprintf(
		"Liquidation Capture %s failed: %s (session: %s)", exchange, reasonCodes, sessionID,
	)
	dedupKey := fmt.Sprintf("liquidation_capture_fatal_%s_%s", exchange, sessionID)
	if err := m.notifier.publishEnvelope(
		ctx,
		"liquidation_capture_monitor",
		"operational_health",
		"critical",
		dedupKey,
		message,
		map[string]any{"exchange": exchange, "state": "critical", "session_id": sessionID},
	); err != nil {
		slog.Error("liquidation_capture_monitor.publish_failed", "err", err)
		if delErr := m.notifier.rdb.Del(ctx, claimKey).Err(); delErr != nil {
			slog.Error("liquidation_capture_monitor.claim_release_failed", "err", delErr)
		}
		return false, false
	}
	return true, true
}

func (m *liquidationCaptureMonitor) checkExchange(ctx context.Context, exchange string) {
	now := m.now().UTC()
	health, err := m.notifier.rdb.HGetAll(ctx, liquidationHealthKey(exchange)).Result()
	if err != nil {
		slog.Error("liquidation_capture_monitor.redis_err", "exchange", exchange, "err", err)
		return
	}

	stateKey := liquidationMonitorStateKey(exchange)
	monitorState, err := m.notifier.rdb.HGetAll(ctx, stateKey).Result()
	if err != nil {
		slog.Error("liquidation_capture_monitor.state_read_failed", "exchange", exchange, "err", err)
		return
	}

	state, transitionID, reason, sessionID, ready := m.classifyHealth(
		ctx, exchange, now, health, monitorState,
	)
	if !ready {
		return
	}
	previousState := monitorState["state"]
	previousTransition := monitorState["transition_id"]
	if state == previousState && transitionID == previousTransition {
		return
	}

	if state == "starting" {
		if previousState == "" {
			m.storeMonitorState(ctx, stateKey, state, transitionID)
		}
		return
	}

	if state == "ok" && (previousState == "" || previousState == "ok" || previousState == "starting") {
		m.storeMonitorState(ctx, stateKey, state, transitionID)
		return
	}

	if state == "critical" && strings.HasPrefix(transitionID, "fatal:") {
		published, _ := m.publishFatalIncident(ctx, exchange, sessionID, reason)
		if !published {
			return
		}
		m.storeMonitorState(ctx, stateKey, state, transitionID)
		return
	}

	severity := "info"
	message := fmt.Sprintf("Liquidation Capture %s: %s", exchange, reason)
	if state == "critical" {
		severity = "critical"
	}
	if state == "ok" {
		message = fmt.Sprintf("Liquidation Capture %s is now OK", exchange)
	}
	claimKey := liquidationTransitionClaimKey(exchange, transitionID)
	claimed, err := m.notifier.rdb.SetNX(
		ctx, claimKey, now.UnixMilli(), liquidationCaptureIncidentRetention,
	).Result()
	if err != nil {
		slog.Error("liquidation_capture_monitor.transition_claim_failed", "exchange", exchange, "err", err)
		return
	}
	if claimed {
		dedupKey := fmt.Sprintf("liquidation_capture_health_%s_%s", exchange, transitionID)
		if err := m.notifier.publishEnvelope(
			ctx,
			"liquidation_capture_monitor",
			"operational_health",
			severity,
			dedupKey,
			message,
			map[string]any{"exchange": exchange, "state": state, "session_id": sessionID},
		); err != nil {
			slog.Error("liquidation_capture_monitor.publish_failed", "err", err)
			if delErr := m.notifier.rdb.Del(ctx, claimKey).Err(); delErr != nil {
				slog.Error("liquidation_capture_monitor.claim_release_failed", "err", delErr)
			}
			return
		}
	}
	m.storeMonitorState(ctx, stateKey, state, transitionID)
}

func (m *liquidationCaptureMonitor) classifyHealth(
	ctx context.Context,
	exchange string,
	now time.Time,
	health map[string]string,
	monitorState map[string]string,
) (state, transitionID, reason, sessionID string, ready bool) {
	stateKey := liquidationMonitorStateKey(exchange)
	if len(health) == 0 {
		missingSinceMs, _ := strconv.ParseInt(monitorState["missing_since_ms"], 10, 64)
		if missingSinceMs == 0 {
			missingSinceMs = now.UnixMilli()
			if err := m.notifier.rdb.HSet(
				ctx, stateKey, "missing_since_ms", missingSinceMs,
			).Err(); err != nil {
				slog.Error("liquidation_capture_monitor.missing_mark_failed", "exchange", exchange, "err", err)
			}
			return "", "", "", "", false
		}
		if now.Sub(time.UnixMilli(missingSinceMs)) < m.missingGrace {
			return "", "", "", "", false
		}
		return "critical", fmt.Sprintf("missing:%d", missingSinceMs), "health key missing or expired", "", true
	}
	if err := m.notifier.rdb.HDel(ctx, stateKey, "missing_since_ms").Err(); err != nil {
		slog.Error("liquidation_capture_monitor.missing_clear_failed", "exchange", exchange, "err", err)
	}

	status := health["status"]
	reasonCodes := health["reason_codes"]
	sessionID = health["process_session_id"]
	changedAt := health["status_changed_at_ms"]
	if changedAt == "" {
		changedAt = health["updated_at_ms"]
	}
	transitionID = fmt.Sprintf("%s:%s:%s", status, sessionID, changedAt)
	switch status {
	case "ok":
		return "ok", transitionID, "ok", sessionID, true
	case "starting":
		return "starting", transitionID, fmt.Sprintf("starting (session: %s)", sessionID), sessionID, true
	case "degraded":
		return "warning", transitionID, fmt.Sprintf("degraded: %s (session: %s)", reasonCodes, sessionID), sessionID, true
	case "failed":
		return "critical", "fatal:" + sessionID, reasonCodes, sessionID, true
	default:
		return "critical", transitionID, fmt.Sprintf("unknown status %q (session: %s)", status, sessionID), sessionID, true
	}
}

func (m *liquidationCaptureMonitor) storeMonitorState(
	ctx context.Context,
	key string,
	state string,
	transitionID string,
) {
	if err := m.notifier.rdb.HSet(ctx, key, map[string]any{
		"state": state, "transition_id": transitionID,
	}).Err(); err != nil {
		slog.Error("liquidation_capture_monitor.state_store_failed", "err", err)
	}
}
