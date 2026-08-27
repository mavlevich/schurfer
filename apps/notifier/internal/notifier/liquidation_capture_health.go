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

	// liquidationCoverageMinConsecutiveMinutes is the number of consecutive
	// incomplete heartbeat minutes a purely transient coverage gap must reach
	// before it is worth paging anyone. A single incomplete minute is routine
	// noise (the current minute is, by construction, incomplete until it
	// closes) and must never itself produce a Telegram message.
	liquidationCoverageMinConsecutiveMinutes = 2

	// liquidationCoverageRecoveryHold is how long raw health must report ok,
	// continuously, before a recovery message is sent. This prevents a
	// single healthy poll sandwiched between two degraded ones from flipping
	// Telegram between "degraded" and "is now OK".
	liquidationCoverageRecoveryHold = 60 * time.Second
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

// checkExchange routes on the coarse classified state. "starting" and the
// two non-debounced critical paths (missing-after-grace, an actually failed
// session) behave exactly as before. "ok" and "warning" (degraded, neither
// fatal nor missing) go through evaluateCoverage, which owns its own
// incident/alert-class state instead of relying on the raw health hash's
// churn-prone status_changed_at_ms.
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

	if state == "starting" {
		if monitorState["state"] == "" {
			m.storeMonitorState(ctx, stateKey, state, transitionID)
		}
		return
	}

	if state == "critical" {
		m.handleCritical(ctx, exchange, stateKey, monitorState, state, transitionID, reason, sessionID)
		return
	}

	if state == "ok" {
		m.handleOk(ctx, exchange, stateKey, now, transitionID, monitorState)
		return
	}

	// state == "warning": route through the coverage/operational state
	// machine, which is keyed on its own incident_id, not transitionID.
	consecutiveIncomplete, _ := strconv.Atoi(health["consecutive_incomplete_minutes"])
	m.storeMonitorState(ctx, stateKey, state, transitionID)
	m.evaluateCoverage(
		ctx, exchange, stateKey, now, health["reason_codes"], sessionID, consecutiveIncomplete,
		loadLiquidationCoverageState(monitorState),
	)
}

// handleOk decides how a transition into "ok" is reported, and separately
// always gives evaluateCoverageRecovery a chance to close (and, once stable
// for liquidationCoverageRecoveryHold, announce) any open coverage/
// operational incident. The two are independent: recovering from a fatal or
// missing-health "critical" state keeps the original immediate message
// (matching "critical/fatal stay undebounced"); recovering from "warning"
// sends nothing here at all -- that recovery message belongs entirely to
// evaluateCoverageRecovery, once raw health has been stably ok for the hold
// window, so a single unhealthy period never produces two recoveries.
func (m *liquidationCaptureMonitor) handleOk(
	ctx context.Context,
	exchange string,
	stateKey string,
	now time.Time,
	transitionID string,
	monitorState map[string]string,
) {
	previousState := monitorState["state"]
	previousTransition := monitorState["transition_id"]
	if !(previousState == "ok" && previousTransition == transitionID) {
		switch previousState {
		case "", "ok", "starting", "warning":
			m.storeMonitorState(ctx, stateKey, "ok", transitionID)
		case "critical":
			m.sendImmediateOkRecovery(ctx, exchange, stateKey, transitionID)
		}
	}
	m.evaluateCoverageRecovery(ctx, exchange, stateKey, now, loadLiquidationCoverageState(monitorState))
}

func (m *liquidationCaptureMonitor) sendImmediateOkRecovery(
	ctx context.Context, exchange string, stateKey string, transitionID string,
) {
	claimKey := liquidationTransitionClaimKey(exchange, transitionID)
	claimed, err := m.notifier.rdb.SetNX(
		ctx, claimKey, m.now().UTC().UnixMilli(), liquidationCaptureIncidentRetention,
	).Result()
	if err != nil {
		slog.Error("liquidation_capture_monitor.transition_claim_failed", "exchange", exchange, "err", err)
		return
	}
	if claimed {
		message := fmt.Sprintf("Liquidation Capture %s is now OK", exchange)
		dedupKey := fmt.Sprintf("liquidation_capture_health_%s_%s", exchange, transitionID)
		if err := m.notifier.publishEnvelope(
			ctx,
			"liquidation_capture_monitor",
			"operational_health",
			"info",
			dedupKey,
			message,
			map[string]any{"exchange": exchange, "state": "ok"},
		); err != nil {
			slog.Error("liquidation_capture_monitor.publish_failed", "err", err)
			if delErr := m.notifier.rdb.Del(ctx, claimKey).Err(); delErr != nil {
				slog.Error("liquidation_capture_monitor.claim_release_failed", "err", delErr)
			}
			return
		}
	}
	m.storeMonitorState(ctx, stateKey, "ok", transitionID)
}

// handleCritical preserves the pre-existing, unchanged behavior for the two
// non-fatal critical paths (missing health after grace, and the defensive
// "unknown status" branch): immediate, SetNX-claimed delivery, no debounce.
// An actually failed session is routed to the same fatal-incident mechanism
// checkFatalIncidents already uses, keeping exactly one code path for it.
func (m *liquidationCaptureMonitor) handleCritical(
	ctx context.Context,
	exchange string,
	stateKey string,
	monitorState map[string]string,
	state, transitionID, reason, sessionID string,
) {
	previousState := monitorState["state"]
	previousTransition := monitorState["transition_id"]
	if state == previousState && transitionID == previousTransition {
		return
	}

	// Escalating straight into a fatal or missing-health incident supersedes
	// any coverage/operational tracking in flight: the eventual recovery
	// from critical already announces "is now OK" once, so any open
	// coverage incident must not also fire its own recovery later.
	m.storeCoverageState(ctx, stateKey, liquidationCoverageState{})

	if strings.HasPrefix(transitionID, "fatal:") {
		published, _ := m.publishFatalIncident(ctx, exchange, sessionID, reason)
		if !published {
			return
		}
		m.storeMonitorState(ctx, stateKey, state, transitionID)
		return
	}

	claimKey := liquidationTransitionClaimKey(exchange, transitionID)
	claimed, err := m.notifier.rdb.SetNX(
		ctx, claimKey, m.now().UTC().UnixMilli(), liquidationCaptureIncidentRetention,
	).Result()
	if err != nil {
		slog.Error("liquidation_capture_monitor.transition_claim_failed", "exchange", exchange, "err", err)
		return
	}
	if claimed {
		message := fmt.Sprintf("Liquidation Capture %s: %s", exchange, reason)
		dedupKey := fmt.Sprintf("liquidation_capture_health_%s_%s", exchange, transitionID)
		if err := m.notifier.publishEnvelope(
			ctx,
			"liquidation_capture_monitor",
			"operational_health",
			"critical",
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

// --- coverage/operational alert-class state machine ---
//
// Real production data: EvaluateHealth in the collector accumulates reason
// codes from several independent checks into one comma-joined string, and
// bumps status_changed_at_ms whenever that string changes -- including when
// only "current_minute_incomplete" toggles in and out as the in-progress
// minute advances. That alone can happen every few seconds within a single
// real incident, which is why nothing here keys dedup off transitionID:
// incidentID is assigned once by the notifier, the first time it observes a
// non-ok status, and only cleared once raw health has been continuously ok
// for liquidationCoverageRecoveryHold.

type liquidationAlertClass string

const (
	liquidationAlertNone        liquidationAlertClass = "none"
	liquidationAlertCoverage    liquidationAlertClass = "coverage"
	liquidationAlertOperational liquidationAlertClass = "operational"
)

var liquidationAlertClassRank = map[liquidationAlertClass]int{
	liquidationAlertNone:        0,
	liquidationAlertCoverage:    1,
	liquidationAlertOperational: 2,
}

// liquidationTransientCoverageReasons are the only reason codes considered
// pure data-coverage noise. Any other reason present downgrades the whole
// reason set to operational -- a real operational cause co-occurring with a
// coverage blip must never be hidden by the coverage debounce.
var liquidationTransientCoverageReasons = map[string]bool{
	"current_minute_incomplete": true,
	"incomplete_minute":         true,
}

func classifyCoverageReasons(reasonCodes string) liquidationAlertClass {
	trimmed := strings.TrimSpace(reasonCodes)
	if trimmed == "" {
		return liquidationAlertNone
	}
	for _, code := range strings.Split(trimmed, ",") {
		if !liquidationTransientCoverageReasons[strings.TrimSpace(code)] {
			return liquidationAlertOperational
		}
	}
	return liquidationAlertCoverage
}

type liquidationCoverageState struct {
	incidentID      string
	enqueuedClass   liquidationAlertClass
	pendingClass    liquidationAlertClass
	pendingDedupKey string
	pendingMessage  string
	okSinceMs       int64
}

func loadLiquidationCoverageState(monitorState map[string]string) liquidationCoverageState {
	okSinceMs, _ := strconv.ParseInt(monitorState["coverage_ok_since_ms"], 10, 64)
	return liquidationCoverageState{
		incidentID:      monitorState["coverage_incident_id"],
		enqueuedClass:   liquidationAlertClass(monitorState["coverage_enqueued_class"]),
		pendingClass:    liquidationAlertClass(monitorState["coverage_pending_class"]),
		pendingDedupKey: monitorState["coverage_pending_dedup_key"],
		pendingMessage:  monitorState["coverage_pending_message"],
		okSinceMs:       okSinceMs,
	}
}

func (m *liquidationCaptureMonitor) storeCoverageState(
	ctx context.Context, stateKey string, s liquidationCoverageState,
) {
	if err := m.notifier.rdb.HSet(ctx, stateKey, map[string]any{
		"coverage_incident_id":       s.incidentID,
		"coverage_enqueued_class":    string(s.enqueuedClass),
		"coverage_pending_class":     string(s.pendingClass),
		"coverage_pending_dedup_key": s.pendingDedupKey,
		"coverage_pending_message":   s.pendingMessage,
		"coverage_ok_since_ms":       s.okSinceMs,
	}).Err(); err != nil {
		slog.Error("liquidation_capture_monitor.coverage_state_store_failed", "err", err)
	}
}

// evaluateCoverage is only ever called for a live "warning" (degraded,
// non-fatal, non-missing) classification -- recovery into "ok" is owned by
// handleOk/evaluateCoverageRecovery instead.
func (m *liquidationCaptureMonitor) evaluateCoverage(
	ctx context.Context,
	exchange string,
	stateKey string,
	now time.Time,
	reasonCodes string,
	sessionID string,
	consecutiveIncomplete int,
	s liquidationCoverageState,
) {
	s.okSinceMs = 0
	if s.incidentID == "" {
		s.incidentID = fmt.Sprintf("%s:%s:%d", exchange, sessionID, now.UnixMilli())
		s.enqueuedClass = liquidationAlertNone
		s.pendingClass = liquidationAlertNone
	}

	// A publish attempted before a crash but never confirmed: retry with the
	// exact frozen payload rather than recomputing from (possibly different
	// by now) live reason codes, so a retried send always matches the
	// dedup_key's already-recorded payload hash.
	if s.pendingClass != liquidationAlertNone && s.pendingClass != s.enqueuedClass {
		m.publishCoverageAlert(ctx, exchange, stateKey, s)
		return
	}

	class := classifyCoverageReasons(reasonCodes)
	if class == liquidationAlertCoverage && consecutiveIncomplete < liquidationCoverageMinConsecutiveMinutes {
		// Transient coverage gap that has not persisted long enough to page
		// anyone. Keep the incident open (so an eventual escalation or
		// recovery still has a stable identity) but stay silent.
		m.storeCoverageState(ctx, stateKey, s)
		return
	}

	if liquidationAlertClassRank[class] <= liquidationAlertClassRank[s.enqueuedClass] {
		// Already announced at this class or higher for this incident.
		// Reason-code churn within the same class must never resend.
		m.storeCoverageState(ctx, stateKey, s)
		return
	}

	s.pendingClass = class
	s.pendingDedupKey = fmt.Sprintf(
		"liquidation_capture_health_%s_%s_%s", exchange, s.incidentID, class,
	)
	s.pendingMessage = fmt.Sprintf(
		"Liquidation Capture %s: %s (session: %s)", exchange, reasonCodes, sessionID,
	)
	// Freeze the payload before publishing: a crash between a successful
	// XAdd and this state being marked enqueued must retry the identical
	// dedup_key and message, never a message recomputed from newer data.
	m.storeCoverageState(ctx, stateKey, s)
	m.publishCoverageAlert(ctx, exchange, stateKey, s)
}

func (m *liquidationCaptureMonitor) publishCoverageAlert(
	ctx context.Context, exchange string, stateKey string, s liquidationCoverageState,
) {
	if err := m.notifier.publishEnvelope(
		ctx,
		"liquidation_capture_monitor",
		"operational_health",
		"warning",
		s.pendingDedupKey,
		s.pendingMessage,
		map[string]any{"exchange": exchange, "class": string(s.pendingClass)},
	); err != nil {
		slog.Error("liquidation_capture_monitor.publish_failed", "err", err)
		return
	}
	s.enqueuedClass = s.pendingClass
	m.storeCoverageState(ctx, stateKey, s)
}

func (m *liquidationCaptureMonitor) evaluateCoverageRecovery(
	ctx context.Context,
	exchange string,
	stateKey string,
	now time.Time,
	s liquidationCoverageState,
) {
	if s.incidentID == "" {
		if s.okSinceMs != 0 {
			s.okSinceMs = 0
			m.storeCoverageState(ctx, stateKey, s)
		}
		return
	}
	if s.okSinceMs == 0 {
		s.okSinceMs = now.UnixMilli()
		m.storeCoverageState(ctx, stateKey, s)
		return
	}
	if now.Sub(time.UnixMilli(s.okSinceMs)) < liquidationCoverageRecoveryHold {
		return
	}

	if s.enqueuedClass != liquidationAlertNone {
		dedupKey := fmt.Sprintf("liquidation_capture_health_%s_%s_recovery", exchange, s.incidentID)
		message := fmt.Sprintf("Liquidation Capture %s is now OK", exchange)
		if err := m.notifier.publishEnvelope(
			ctx,
			"liquidation_capture_monitor",
			"operational_health",
			"info",
			dedupKey,
			message,
			map[string]any{"exchange": exchange, "state": "ok"},
		); err != nil {
			slog.Error("liquidation_capture_monitor.publish_failed", "err", err)
			return
		}
	}
	// Fully close the incident whether or not a warning was ever sent for
	// it -- a gap that self-healed before crossing the threshold recovers
	// silently, as it should.
	m.storeCoverageState(ctx, stateKey, liquidationCoverageState{})
}
