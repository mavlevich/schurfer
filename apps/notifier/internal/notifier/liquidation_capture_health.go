package notifier

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"strings"
	"time"
)

type liquidationCaptureMonitor struct {
	notifier     *Notifier
	exchanges    []string
	lastAlerts   map[string]string // exchange -> last state
	missingSince map[string]time.Time
}

func newLiquidationCaptureMonitor(n *Notifier) *liquidationCaptureMonitor {
	val := os.Getenv("LIQUIDATION_CAPTURE_MONITORED_EXCHANGES")
	var exchanges []string
	if val != "" {
		for _, e := range strings.Split(val, ",") {
			e = strings.TrimSpace(e)
			if e != "" {
				exchanges = append(exchanges, e)
			}
		}
	}
	return &liquidationCaptureMonitor{
		notifier:     n,
		exchanges:    exchanges,
		lastAlerts:   make(map[string]string),
		missingSince: make(map[string]time.Time),
	}
}

func (m *liquidationCaptureMonitor) Run(ctx context.Context) {
	if len(m.exchanges) == 0 {
		slog.Info("liquidation_capture_monitor.disabled")
		return
	}
	ticker := time.NewTicker(30 * time.Second)
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
		m.checkExchange(ctx, exchange)
	}
}

func (m *liquidationCaptureMonitor) checkExchange(ctx context.Context, exchange string) {
	key := "market:liquidationcapture:health:" + exchange
	res, err := m.notifier.rdb.HGetAll(ctx, key).Result()
	if err != nil {
		slog.Error("liquidation_capture_monitor.redis_err", "exchange", exchange, "err", err)
		return
	}

	state := "ok"
	var reason string

	if len(res) == 0 {
		state = "critical"
		reason = "health key missing or expired"
	} else {
		status := res["status"]
		reasonCodes := res["reason_codes"]
		sessionID := res["process_session_id"]

		if status == "failed" {
			state = "critical"
			reason = fmt.Sprintf("failed: %s (session: %s)", reasonCodes, sessionID)
		} else if status == "degraded" {
			state = "warning"
			reason = fmt.Sprintf("degraded: %s (session: %s)", reasonCodes, sessionID)
		} else if status == "starting" {
			state = "warning"
			reason = fmt.Sprintf("starting (session: %s)", sessionID)
		}
	}

	last := m.lastAlerts[exchange]
	if state != last {
		if state != "ok" {
			msg := fmt.Sprintf("Liquidation Capture %s: %s", exchange, reason)
			severity := state
			if severity == "warning" {
				severity = "info" // Fallback since warning is not allowed
			}
			dedup := fmt.Sprintf("liquidation_capture_health_%s_%s", exchange, state)
			err := m.notifier.publishEnvelope(
				ctx,
				"liquidation_capture_monitor",
				"operational_health",
				severity,
				dedup,
				msg,
				map[string]any{"exchange": exchange, "state": state},
			)
			if err != nil {
				slog.Error("liquidation_capture_monitor.publish_failed", "err", err)
			} else {
				m.lastAlerts[exchange] = state
			}
		} else if last != "" {
			msg := fmt.Sprintf("Liquidation Capture %s is now OK", exchange)
			dedup := fmt.Sprintf("liquidation_capture_health_%s_ok_%d", exchange, time.Now().Unix()) // unique recovery to not clash
			err := m.notifier.publishEnvelope(
				ctx,
				"liquidation_capture_monitor",
				"operational_health",
				"info",
				dedup,
				msg,
				map[string]any{"exchange": exchange, "state": state},
			)
			if err != nil {
				slog.Error("liquidation_capture_monitor.publish_failed", "err", err)
			} else {
				m.lastAlerts[exchange] = state
			}
		}
	}
}
