package notifier

import (
	"context"
	"encoding/json"
	"strconv"
	"testing"
	"time"

	"github.com/alicebob/miniredis/v2"
	"github.com/redis/go-redis/v9"
)

func newTestLiquidationMonitor(
	t *testing.T,
	rdb *redis.Client,
	now time.Time,
) *liquidationCaptureMonitor {
	t.Helper()
	return &liquidationCaptureMonitor{
		notifier: &Notifier{rdb: rdb}, exchanges: []string{"bybit"},
		missingGrace: 90 * time.Second, now: func() time.Time { return now },
	}
}

func TestParseMonitoredExchangesNormalizesAndDeduplicates(t *testing.T) {
	got := parseMonitoredExchanges(" BYBIT,binance,bybit,unknown ")
	if len(got) != 2 || got[0] != "bybit" || got[1] != "binance" {
		t.Fatalf("exchanges = %v", got)
	}
}

func TestLiquidationCaptureMonitorMissingHealthUsesPersistentGrace(t *testing.T) {
	mr := miniredis.RunT(t)
	rdb := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	ctx := context.Background()
	now := time.Date(2026, 8, 26, 12, 0, 0, 0, time.UTC)

	first := newTestLiquidationMonitor(t, rdb, now)
	first.checkExchange(ctx, "bybit")
	if got := outboxMessages(t, rdb); len(got) != 0 {
		t.Fatalf("cold-start grace emitted %d messages", len(got))
	}
	missingSince := mr.HGet(liquidationMonitorStateKey("bybit"), "missing_since_ms")
	if missingSince == "" {
		t.Fatal("missing_since_ms was not persisted")
	}

	// A notifier restart must not reset the grace window.
	restarted := newTestLiquidationMonitor(t, rdb, now.Add(91*time.Second))
	restarted.checkExchange(ctx, "bybit")
	messages := outboxMessages(t, rdb)
	if len(messages) != 1 {
		t.Fatalf("messages after persistent grace = %d", len(messages))
	}
	if env := decodeEnvelope(t, messages[0]); env.Severity != "critical" {
		t.Fatalf("missing health severity = %s", env.Severity)
	}
}

func TestLiquidationCaptureMonitorRecoveryAndRestartAreIdempotent(t *testing.T) {
	mr := miniredis.RunT(t)
	rdb := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	ctx := context.Background()
	now := time.Date(2026, 8, 26, 12, 0, 0, 0, time.UTC)
	monitor := newTestLiquidationMonitor(t, rdb, now)

	if err := rdb.HSet(ctx, liquidationMonitorStateKey("bybit"), map[string]any{
		"state": "critical", "transition_id": "missing:1",
	}).Err(); err != nil {
		t.Fatal(err)
	}
	setLiquidationHealth(t, rdb, "ok", "", "session-2", now.UnixMilli())
	monitor.checkExchange(ctx, "bybit")
	if got := outboxMessages(t, rdb); len(got) != 1 {
		t.Fatalf("recovery messages = %d", len(got))
	}

	// Recreating the monitor simulates a notifier restart. Redis state and the
	// transition claim prevent a duplicate recovery.
	restarted := newTestLiquidationMonitor(t, rdb, now.Add(time.Second))
	restarted.checkExchange(ctx, "bybit")
	if got := outboxMessages(t, rdb); len(got) != 1 {
		t.Fatalf("restart duplicated recovery: %d messages", len(got))
	}
}

func TestLiquidationCaptureMonitorAlertsEveryFatalSessionExactlyOnce(t *testing.T) {
	mr := miniredis.RunT(t)
	rdb := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	ctx := context.Background()
	now := time.Date(2026, 8, 26, 12, 0, 0, 0, time.UTC)
	monitor := newTestLiquidationMonitor(t, rdb, now)

	addFatalIncident(t, rdb, "bybit", "session-1", now, "queue_drop_critical")
	monitor.checkFatalIncidents(ctx, "bybit")
	addFatalIncident(t, rdb, "bybit", "session-2", now.Add(time.Second), "fatal_payload_mismatch")
	monitor.checkFatalIncidents(ctx, "bybit")
	monitor.checkFatalIncidents(ctx, "bybit")

	messages := outboxMessages(t, rdb)
	if len(messages) != 2 {
		t.Fatalf("fatal incident messages = %d, want one per session", len(messages))
	}
	for _, message := range messages {
		if env := decodeEnvelope(t, message); env.Severity != "critical" {
			t.Fatalf("fatal severity = %s", env.Severity)
		}
	}
}

func TestLiquidationCaptureMonitorFatalRestartRecoversOnlyAfterOk(t *testing.T) {
	mr := miniredis.RunT(t)
	rdb := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	ctx := context.Background()
	now := time.Date(2026, 8, 26, 12, 0, 0, 0, time.UTC)
	monitor := newTestLiquidationMonitor(t, rdb, now)

	addFatalIncident(t, rdb, "bybit", "session-failed", now, "queue_drop_critical")
	setLiquidationHealth(t, rdb, "starting", "awaiting_first_complete_minute", "session-new", now.Add(time.Second).UnixMilli())
	monitor.checkHealth(ctx)
	if got := outboxMessages(t, rdb); len(got) != 1 {
		t.Fatalf("fatal plus starting produced %d messages", len(got))
	}

	setLiquidationHealth(t, rdb, "ok", "", "session-new", now.Add(time.Minute).UnixMilli())
	monitor.checkHealth(ctx)
	messages := outboxMessages(t, rdb)
	if len(messages) != 2 {
		t.Fatalf("fatal recovery produced %d messages", len(messages))
	}
	if env := decodeEnvelope(t, messages[1]); env.Severity != "info" {
		t.Fatalf("recovery severity = %s", env.Severity)
	}
}

// Reason codes churning within the same alert class (both "disconnected_streams"
// and "persist_error" are operational, non-transient reasons) must not resend --
// this is the exact flapping pattern the coverage/operational state machine
// exists to collapse. Prior to that state machine every distinct
// status_changed_at_ms produced a new transitionID and therefore a new message;
// that is now intentionally no longer true.
func TestLiquidationCaptureMonitorReasonChurnWithinSameClassDoesNotResend(t *testing.T) {
	mr := miniredis.RunT(t)
	rdb := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	ctx := context.Background()
	now := time.Date(2026, 8, 26, 12, 0, 0, 0, time.UTC)
	monitor := newTestLiquidationMonitor(t, rdb, now)

	setLiquidationHealth(t, rdb, "degraded", "disconnected_streams", "session-1", now.UnixMilli())
	monitor.checkExchange(ctx, "bybit")
	setLiquidationHealth(t, rdb, "degraded", "persist_error", "session-1", now.Add(time.Minute).UnixMilli())
	monitor.checkExchange(ctx, "bybit")

	messages := outboxMessages(t, rdb)
	if len(messages) != 1 {
		t.Fatalf("reason churn within the same class produced %d messages, want 1", len(messages))
	}
	if env := decodeEnvelope(t, messages[0]); env.Severity != "warning" {
		t.Fatalf("operational alert severity = %s, want warning", env.Severity)
	}
}

func TestLiquidationCaptureMonitorSingleIncompleteMinuteStaysSilent(t *testing.T) {
	mr := miniredis.RunT(t)
	rdb := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	ctx := context.Background()
	now := time.Date(2026, 8, 26, 12, 0, 0, 0, time.UTC)
	monitor := newTestLiquidationMonitor(t, rdb, now)

	setLiquidationHealthFull(t, rdb, "degraded", "incomplete_minute", "session-1", now.UnixMilli(), 1)
	monitor.checkExchange(ctx, "bybit")

	if got := outboxMessages(t, rdb); len(got) != 0 {
		t.Fatalf("single incomplete minute produced %d messages, want 0", len(got))
	}
}

func TestLiquidationCaptureMonitorRecoveryAfterSingleIncompleteMinuteStaysSilent(t *testing.T) {
	mr := miniredis.RunT(t)
	rdb := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	ctx := context.Background()
	now := time.Date(2026, 8, 26, 12, 0, 0, 0, time.UTC)
	monitor := newTestLiquidationMonitor(t, rdb, now)

	setLiquidationHealthFull(t, rdb, "degraded", "incomplete_minute", "session-1", now.UnixMilli(), 1)
	monitor.checkExchange(ctx, "bybit")
	setLiquidationHealth(t, rdb, "ok", "", "session-1", now.Add(30*time.Second).UnixMilli())
	monitor.checkExchange(ctx, "bybit")
	afterHold := newTestLiquidationMonitor(t, rdb, now.Add(90*time.Second))
	afterHold.checkExchange(ctx, "bybit")

	if got := outboxMessages(t, rdb); len(got) != 0 {
		t.Fatalf("recovery from a never-escalated gap produced %d messages, want 0", len(got))
	}
}

func TestLiquidationCaptureMonitorTwoConsecutiveIncompleteMinutesSendExactlyOneWarning(t *testing.T) {
	mr := miniredis.RunT(t)
	rdb := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	ctx := context.Background()
	now := time.Date(2026, 8, 26, 12, 0, 0, 0, time.UTC)
	monitor := newTestLiquidationMonitor(t, rdb, now)

	setLiquidationHealthFull(t, rdb, "degraded", "incomplete_minute", "session-1", now.UnixMilli(), 1)
	monitor.checkExchange(ctx, "bybit")
	setLiquidationHealthFull(t, rdb, "degraded", "incomplete_minute", "session-1", now.Add(30*time.Second).UnixMilli(), 2)
	monitor.checkExchange(ctx, "bybit")

	messages := outboxMessages(t, rdb)
	if len(messages) != 1 {
		t.Fatalf("two consecutive incomplete minutes produced %d messages, want 1", len(messages))
	}
	if env := decodeEnvelope(t, messages[0]); env.Severity != "warning" {
		t.Fatalf("coverage alert severity = %s, want warning", env.Severity)
	}
}

// current_minute_incomplete toggling in and out of the joined reason string
// within the same still-open gap (the root cause of the original flapping,
// since it bumps status_changed_at_ms every time) must not create a second
// warning once consecutive_incomplete_minutes has already crossed the
// escalation threshold.
func TestLiquidationCaptureMonitorCurrentMinuteReasonChurnWithinSameGapDoesNotResend(t *testing.T) {
	mr := miniredis.RunT(t)
	rdb := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	ctx := context.Background()
	now := time.Date(2026, 8, 26, 12, 0, 0, 0, time.UTC)
	monitor := newTestLiquidationMonitor(t, rdb, now)

	setLiquidationHealthFull(t, rdb, "degraded", "incomplete_minute", "session-1", now.UnixMilli(), 2)
	monitor.checkExchange(ctx, "bybit")
	setLiquidationHealthFull(
		t, rdb, "degraded", "incomplete_minute,current_minute_incomplete", "session-1",
		now.Add(5*time.Second).UnixMilli(), 2,
	)
	monitor.checkExchange(ctx, "bybit")
	setLiquidationHealthFull(t, rdb, "degraded", "incomplete_minute", "session-1", now.Add(10*time.Second).UnixMilli(), 2)
	monitor.checkExchange(ctx, "bybit")

	if got := outboxMessages(t, rdb); len(got) != 1 {
		t.Fatalf("reason churn within the same open gap produced %d messages, want 1", len(got))
	}
}

func TestLiquidationCaptureMonitorRestartDoesNotResetCoverageDedup(t *testing.T) {
	mr := miniredis.RunT(t)
	rdb := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	ctx := context.Background()
	now := time.Date(2026, 8, 26, 12, 0, 0, 0, time.UTC)
	monitor := newTestLiquidationMonitor(t, rdb, now)

	setLiquidationHealthFull(t, rdb, "degraded", "incomplete_minute", "session-1", now.UnixMilli(), 2)
	monitor.checkExchange(ctx, "bybit")
	if got := outboxMessages(t, rdb); len(got) != 1 {
		t.Fatalf("initial escalation produced %d messages, want 1", len(got))
	}

	restarted := newTestLiquidationMonitor(t, rdb, now.Add(30*time.Second))
	setLiquidationHealthFull(t, rdb, "degraded", "incomplete_minute", "session-1", now.Add(30*time.Second).UnixMilli(), 3)
	restarted.checkExchange(ctx, "bybit")

	if got := outboxMessages(t, rdb); len(got) != 1 {
		t.Fatalf("restart duplicated the coverage warning: %d messages", len(got))
	}
}

func TestLiquidationCaptureMonitorOkUnder60SecondsDoesNotSendRecovery(t *testing.T) {
	mr := miniredis.RunT(t)
	rdb := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	ctx := context.Background()
	now := time.Date(2026, 8, 26, 12, 0, 0, 0, time.UTC)
	monitor := newTestLiquidationMonitor(t, rdb, now)

	setLiquidationHealthFull(t, rdb, "degraded", "persist_error", "session-1", now.UnixMilli(), 0)
	monitor.checkExchange(ctx, "bybit")
	setLiquidationHealth(t, rdb, "ok", "", "session-1", now.Add(time.Minute).UnixMilli())
	stillWithinHold := newTestLiquidationMonitor(t, rdb, now.Add(90*time.Second))
	stillWithinHold.checkExchange(ctx, "bybit")

	messages := outboxMessages(t, rdb)
	if len(messages) != 1 {
		t.Fatalf("recovery under the 60s hold sent %d messages, want 1 (only the original warning)", len(messages))
	}
}

func TestLiquidationCaptureMonitorStableOkAfter60SecondsSendsOneRecovery(t *testing.T) {
	mr := miniredis.RunT(t)
	rdb := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	ctx := context.Background()
	now := time.Date(2026, 8, 26, 12, 0, 0, 0, time.UTC)
	monitor := newTestLiquidationMonitor(t, rdb, now)

	setLiquidationHealthFull(t, rdb, "degraded", "persist_error", "session-1", now.UnixMilli(), 0)
	monitor.checkExchange(ctx, "bybit")
	setLiquidationHealth(t, rdb, "ok", "", "session-1", now.Add(time.Minute).UnixMilli())

	firstOkPoll := newTestLiquidationMonitor(t, rdb, now.Add(time.Minute))
	firstOkPoll.checkExchange(ctx, "bybit")
	if got := outboxMessages(t, rdb); len(got) != 1 {
		t.Fatalf("ok observed for the first time already sent recovery: %d messages", len(got))
	}

	stable := newTestLiquidationMonitor(t, rdb, now.Add(time.Minute+61*time.Second))
	stable.checkExchange(ctx, "bybit")

	messages := outboxMessages(t, rdb)
	if len(messages) != 2 {
		t.Fatalf("stable ok after 60s produced %d messages, want 2 (warning + recovery)", len(messages))
	}
	if env := decodeEnvelope(t, messages[1]); env.Severity != "info" {
		t.Fatalf("recovery severity = %s, want info", env.Severity)
	}

	// Recovering again must not resend.
	again := newTestLiquidationMonitor(t, rdb, now.Add(time.Minute+90*time.Second))
	again.checkExchange(ctx, "bybit")
	if got := outboxMessages(t, rdb); len(got) != 2 {
		t.Fatalf("re-polling a closed incident produced %d messages, want 2", len(got))
	}
}

func TestLiquidationCaptureMonitorNewGapAfterConfirmedRecoveryCanAlertAgain(t *testing.T) {
	mr := miniredis.RunT(t)
	rdb := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	ctx := context.Background()
	now := time.Date(2026, 8, 26, 12, 0, 0, 0, time.UTC)
	monitor := newTestLiquidationMonitor(t, rdb, now)

	setLiquidationHealthFull(t, rdb, "degraded", "persist_error", "session-1", now.UnixMilli(), 0)
	monitor.checkExchange(ctx, "bybit")
	setLiquidationHealth(t, rdb, "ok", "", "session-1", now.Add(time.Minute).UnixMilli())
	firstOkPoll := newTestLiquidationMonitor(t, rdb, now.Add(time.Minute))
	firstOkPoll.checkExchange(ctx, "bybit")
	monitor2 := newTestLiquidationMonitor(t, rdb, now.Add(time.Minute+61*time.Second))
	monitor2.checkExchange(ctx, "bybit")
	if got := outboxMessages(t, rdb); len(got) != 2 {
		t.Fatalf("setup: warning+recovery = %d messages, want 2", len(got))
	}

	setLiquidationHealthFull(
		t, rdb, "degraded", "persist_error", "session-1",
		now.Add(2*time.Minute).UnixMilli(), 0,
	)
	monitor3 := newTestLiquidationMonitor(t, rdb, now.Add(2*time.Minute))
	monitor3.checkExchange(ctx, "bybit")

	if got := outboxMessages(t, rdb); len(got) != 3 {
		t.Fatalf("a new gap after confirmed recovery produced %d messages, want 3", len(got))
	}
}

func TestLiquidationCaptureMonitorPersistErrorIsNotHiddenByCoverageDebounce(t *testing.T) {
	mr := miniredis.RunT(t)
	rdb := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	ctx := context.Background()
	now := time.Date(2026, 8, 26, 12, 0, 0, 0, time.UTC)
	monitor := newTestLiquidationMonitor(t, rdb, now)

	// consecutive_incomplete_minutes = 0: a pure coverage-only reason at this
	// count would stay silent, but persist_error is operational and must
	// escalate immediately regardless.
	setLiquidationHealthFull(t, rdb, "degraded", "persist_error", "session-1", now.UnixMilli(), 0)
	monitor.checkExchange(ctx, "bybit")

	if got := outboxMessages(t, rdb); len(got) != 1 {
		t.Fatalf("persist_error produced %d messages, want 1 (undebounced)", len(got))
	}
}

// A single reason_codes value mixing a transient coverage code with a real
// operational one (EvaluateHealth in the collector appends reasons from
// independent checks into one joined string, so this combination is a real
// possibility) must be treated as operational end-to-end, not coverage.
func TestLiquidationCaptureMonitorMixedReasonsAreNeverDebounced(t *testing.T) {
	mr := miniredis.RunT(t)
	rdb := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	ctx := context.Background()
	now := time.Date(2026, 8, 26, 12, 0, 0, 0, time.UTC)
	monitor := newTestLiquidationMonitor(t, rdb, now)

	setLiquidationHealthFull(
		t, rdb, "degraded", "incomplete_minute,persist_error", "session-1", now.UnixMilli(), 1,
	)
	monitor.checkExchange(ctx, "bybit")

	if got := outboxMessages(t, rdb); len(got) != 1 {
		t.Fatalf("mixed transient+operational reasons produced %d messages, want 1 (undebounced)", len(got))
	}
}

// Once a coverage warning has been sent for an incident, a genuinely new
// operational cause joining the same still-open incident must still escalate
// -- this is the exact gap a plain "already warned" boolean would hide.
func TestLiquidationCaptureMonitorEscalatesFromCoverageToOperational(t *testing.T) {
	mr := miniredis.RunT(t)
	rdb := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	ctx := context.Background()
	now := time.Date(2026, 8, 26, 12, 0, 0, 0, time.UTC)
	monitor := newTestLiquidationMonitor(t, rdb, now)

	setLiquidationHealthFull(t, rdb, "degraded", "incomplete_minute", "session-1", now.UnixMilli(), 2)
	monitor.checkExchange(ctx, "bybit")
	if got := outboxMessages(t, rdb); len(got) != 1 {
		t.Fatalf("setup: coverage warning = %d messages, want 1", len(got))
	}

	setLiquidationHealthFull(
		t, rdb, "degraded", "incomplete_minute,persist_error", "session-1",
		now.Add(30*time.Second).UnixMilli(), 2,
	)
	monitor.checkExchange(ctx, "bybit")

	messages := outboxMessages(t, rdb)
	if len(messages) != 2 {
		t.Fatalf("escalation to operational produced %d messages, want 2", len(messages))
	}
	if env := decodeEnvelope(t, messages[1]); env.Severity != "warning" {
		t.Fatalf("escalation severity = %s, want warning", env.Severity)
	}

	// A further poll still showing the same operational cause must not
	// resend a third time.
	monitor.checkExchange(ctx, "bybit")
	if got := outboxMessages(t, rdb); len(got) != 2 {
		t.Fatalf("re-polling the same operational class produced %d messages, want 2", len(got))
	}
}

// A crash between a successful publish and the state update that marks it
// enqueued must retry with the exact frozen dedup_key and message on the next
// poll, not a message recomputed from newer (by then different) reason
// codes -- otherwise the retry would carry the same dedup_key but a
// different payload hash and the consumer's ON CONFLICT check would treat it
// as a conflicting duplicate.
func TestLiquidationCaptureMonitorRetriesFrozenPayloadAfterUnconfirmedPublish(t *testing.T) {
	mr := miniredis.RunT(t)
	rdb := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	ctx := context.Background()
	now := time.Date(2026, 8, 26, 12, 0, 0, 0, time.UTC)
	monitor := newTestLiquidationMonitor(t, rdb, now)

	// Simulate the moment right after XADD succeeded but before enqueuedClass
	// was persisted: pending fields are set, enqueued is still none.
	stateKey := liquidationMonitorStateKey("bybit")
	frozenMessage := "Liquidation Capture bybit: persist_error (session: session-1)"
	frozenDedupKey := "liquidation_capture_health_bybit_bybit:session-1:" + strconv.FormatInt(now.UnixMilli(), 10) + "_operational"
	if err := rdb.HSet(ctx, stateKey, map[string]any{
		"coverage_incident_id":       "bybit:session-1:" + strconv.FormatInt(now.UnixMilli(), 10),
		"coverage_enqueued_class":    "none",
		"coverage_pending_class":     "operational",
		"coverage_pending_dedup_key": frozenDedupKey,
		"coverage_pending_message":   frozenMessage,
		"coverage_ok_since_ms":       0,
	}).Err(); err != nil {
		t.Fatal(err)
	}
	// Live reason codes have since changed -- the retry must ignore this and
	// reuse the frozen message instead of recomputing from it.
	setLiquidationHealthFull(
		t, rdb, "degraded", "persist_error,high_backlog", "session-1", now.Add(30*time.Second).UnixMilli(), 0,
	)

	monitor.checkExchange(ctx, "bybit")

	messages := outboxMessages(t, rdb)
	if len(messages) != 1 {
		t.Fatalf("retry produced %d messages, want 1", len(messages))
	}
	env := decodeEnvelope(t, messages[0])
	if env.Payload.Text != frozenMessage {
		t.Fatalf("retry text = %q, want frozen %q", env.Payload.Text, frozenMessage)
	}
	if env.DedupKey != frozenDedupKey {
		t.Fatalf("retry dedup_key = %q, want frozen %q", env.DedupKey, frozenDedupKey)
	}
}

func TestLiquidationCaptureMonitorBinanceAndBybitHaveIndependentState(t *testing.T) {
	mr := miniredis.RunT(t)
	rdb := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	ctx := context.Background()
	now := time.Date(2026, 8, 26, 12, 0, 0, 0, time.UTC)
	monitor := &liquidationCaptureMonitor{
		notifier: &Notifier{rdb: rdb}, exchanges: []string{"bybit", "binance"},
		missingGrace: 90 * time.Second, now: func() time.Time { return now },
	}

	setExchangeHealth(t, rdb, "bybit", "degraded", "persist_error", "session-bybit", now.UnixMilli(), 0)
	setExchangeHealth(t, rdb, "binance", "ok", "", "session-binance", now.UnixMilli(), 0)
	monitor.checkHealth(ctx)

	messages := outboxMessages(t, rdb)
	if len(messages) != 1 {
		t.Fatalf("independent-exchange setup produced %d messages, want 1", len(messages))
	}
	env := decodeEnvelope(t, messages[0])
	if exchange, _ := env.Payload.Metadata["exchange"].(string); exchange != "bybit" {
		t.Fatalf("alert exchange = %v, want bybit", env.Payload.Metadata["exchange"])
	}

	// binance degrading afterward must not be suppressed by bybit's already-
	// open incident, and must not resend bybit's.
	setExchangeHealth(t, rdb, "binance", "degraded", "persist_error", "session-binance", now.Add(time.Minute).UnixMilli(), 0)
	later := &liquidationCaptureMonitor{
		notifier: monitor.notifier, exchanges: monitor.exchanges,
		missingGrace: monitor.missingGrace, now: func() time.Time { return now.Add(time.Minute) },
	}
	later.checkHealth(ctx)

	messages = outboxMessages(t, rdb)
	if len(messages) != 2 {
		t.Fatalf("binance degrading produced %d total messages, want 2", len(messages))
	}
	env = decodeEnvelope(t, messages[1])
	if exchange, _ := env.Payload.Metadata["exchange"].(string); exchange != "binance" {
		t.Fatalf("second alert exchange = %v, want binance", env.Payload.Metadata["exchange"])
	}
}

func setLiquidationHealth(
	t *testing.T,
	rdb *redis.Client,
	status string,
	reason string,
	sessionID string,
	changedAtMs int64,
) {
	t.Helper()
	setLiquidationHealthFull(t, rdb, status, reason, sessionID, changedAtMs, 0)
}

func setLiquidationHealthFull(
	t *testing.T,
	rdb *redis.Client,
	status string,
	reason string,
	sessionID string,
	changedAtMs int64,
	consecutiveIncomplete int,
) {
	t.Helper()
	setExchangeHealth(t, rdb, "bybit", status, reason, sessionID, changedAtMs, consecutiveIncomplete)
}

func setExchangeHealth(
	t *testing.T,
	rdb *redis.Client,
	exchange string,
	status string,
	reason string,
	sessionID string,
	changedAtMs int64,
	consecutiveIncomplete int,
) {
	t.Helper()
	if err := rdb.HSet(context.Background(), liquidationHealthKey(exchange), map[string]any{
		"status": status, "reason_codes": reason, "process_session_id": sessionID,
		"status_changed_at_ms": changedAtMs, "updated_at_ms": changedAtMs,
		"consecutive_incomplete_minutes": consecutiveIncomplete,
	}).Err(); err != nil {
		t.Fatal(err)
	}
}

func addFatalIncident(
	t *testing.T,
	rdb *redis.Client,
	exchange string,
	sessionID string,
	when time.Time,
	reason string,
) {
	t.Helper()
	ctx := context.Background()
	if err := rdb.HSet(ctx, liquidationIncidentKey(exchange, sessionID), map[string]any{
		"exchange": exchange, "process_session_id": sessionID,
		"occurred_at_ms": when.UnixMilli(), "reason_codes": reason,
	}).Err(); err != nil {
		t.Fatal(err)
	}
	if err := rdb.ZAdd(ctx, liquidationIncidentIndexKey(exchange), redis.Z{
		Score: float64(when.UnixMilli()), Member: sessionID,
	}).Err(); err != nil {
		t.Fatal(err)
	}
}

func outboxMessages(t *testing.T, rdb *redis.Client) []redis.XMessage {
	t.Helper()
	messages, err := rdb.XRange(context.Background(), StreamOutboxV1, "-", "+").Result()
	if err != nil {
		t.Fatal(err)
	}
	return messages
}

func decodeEnvelope(t *testing.T, message redis.XMessage) Envelope {
	t.Helper()
	var env Envelope
	if err := json.Unmarshal([]byte(message.Values["data"].(string)), &env); err != nil {
		t.Fatal(err)
	}
	return env
}
