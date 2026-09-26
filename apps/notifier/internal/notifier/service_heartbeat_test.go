package notifier

import (
	"context"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/alicebob/miniredis/v2"
)

func outboxLen(t *testing.T, n *Notifier) int {
	t.Helper()
	return int(n.rdb.XLen(context.Background(), StreamOutboxV1).Val())
}

func TestParseServiceHeartbeats(t *testing.T) {
	specs := parseServiceHeartbeats(
		"exit=market:x:health@60, bad, noage=key@, dup=a@5, dup=b@6, zero=k@0",
	)
	if len(specs) != 2 {
		t.Fatalf("specs = %+v, want exit and dup", specs)
	}
	if specs[0] != (serviceHeartbeat{Name: "exit", Key: "market:x:health", MaxAge: time.Minute}) {
		t.Fatalf("first spec = %+v", specs[0])
	}
	if specs[1].Key != "a" {
		t.Fatalf("a duplicate name must keep the first spec, got %+v", specs[1])
	}
}

func TestHeartbeatProblem(t *testing.T) {
	now := time.Date(2026, 9, 26, 12, 0, 0, 0, time.UTC)
	fresh := now.Add(-10 * time.Second).Format(time.RFC3339Nano)
	cases := []struct {
		name   string
		fields map[string]string
		want   string
	}{
		{"missing", nil, "heartbeat missing"},
		{"bad time", map[string]string{"generated_at": "x", "status": "ok"}, "heartbeat has no valid generated_at"},
		{"stale", map[string]string{"generated_at": now.Add(-2 * time.Minute).Format(time.RFC3339Nano), "status": "ok"}, "heartbeat 120s old"},
		{"degraded", map[string]string{"generated_at": fresh, "status": "degraded", "last_error": "boom"}, "status=degraded (boom)"},
		{"ok", map[string]string{"generated_at": fresh, "status": "ok"}, ""},
		{"future", map[string]string{"generated_at": now.Add(time.Minute).Format(time.RFC3339Nano), "status": "ok"}, "heartbeat 60s in the future"},
		{"slight skew", map[string]string{"generated_at": now.Add(4 * time.Second).Format(time.RFC3339Nano), "status": "ok"}, ""},
		{"starting", map[string]string{"generated_at": fresh, "status": "starting"}, ""},
		// Python writes isoformat with a +00:00 offset.
		{"python offset", map[string]string{"generated_at": "2026-09-26T11:59:55.123456+00:00", "status": "ok"}, ""},
	}
	for _, tc := range cases {
		if got := heartbeatProblem(tc.fields, time.Minute, now); got != tc.want {
			t.Errorf("%s: got %q, want %q", tc.name, got, tc.want)
		}
	}
}

func TestServiceHeartbeatAlertsOnceAfterDebounceThenRecoversAfterHold(t *testing.T) {
	mr := miniredis.RunT(t)
	n := newTestNotifier(t, mr, "tok", "cid")
	spec := serviceHeartbeat{Name: "exit", Key: "market:x:health", MaxAge: time.Minute}
	ctx := context.Background()
	t0 := time.Date(2026, 9, 26, 12, 0, 0, 0, time.UTC)

	// Missing heartbeat: no alert until it has been unhealthy for one full limit.
	n.checkServiceHeartbeat(ctx, spec, t0)
	n.checkServiceHeartbeat(ctx, spec, t0.Add(30*time.Second))
	if outboxLen(t, n) != 0 {
		t.Fatal("must not alert before the debounce")
	}
	n.checkServiceHeartbeat(ctx, spec, t0.Add(61*time.Second))
	n.checkServiceHeartbeat(ctx, spec, t0.Add(90*time.Second))
	if outboxLen(t, n) != 1 {
		t.Fatalf("alerts = %d, want exactly 1", outboxLen(t, n))
	}

	// Healthy again: recovery only after 60 s of continuous health.
	healthyAt := t0.Add(2 * time.Minute)
	mr.HSet(spec.Key, "status", "ok", "generated_at", healthyAt.Format(time.RFC3339Nano))
	n.checkServiceHeartbeat(ctx, spec, healthyAt)
	if outboxLen(t, n) != 1 {
		t.Fatal("recovery must wait for the hold")
	}
	mr.HSet(spec.Key, "generated_at", healthyAt.Add(61*time.Second).Format(time.RFC3339Nano))
	n.checkServiceHeartbeat(ctx, spec, healthyAt.Add(61*time.Second))
	if outboxLen(t, n) != 2 {
		t.Fatalf("alerts after recovery = %d, want 2", outboxLen(t, n))
	}
	if mr.Exists(heartbeatStateKey("exit", "alerted")) {
		t.Fatal("alerted flag must clear after recovery")
	}
}

func TestServiceHeartbeatBlipDuringRecoveryResetsTheHold(t *testing.T) {
	mr := miniredis.RunT(t)
	n := newTestNotifier(t, mr, "tok", "cid")
	spec := serviceHeartbeat{Name: "exit", Key: "market:x:health", MaxAge: time.Minute}
	ctx := context.Background()
	t0 := time.Date(2026, 9, 26, 12, 0, 0, 0, time.UTC)
	if err := mr.Set(heartbeatStateKey("exit", "alerted"), "1"); err != nil {
		t.Fatal(err)
	}

	mr.HSet(spec.Key, "status", "ok", "generated_at", t0.Format(time.RFC3339Nano))
	n.checkServiceHeartbeat(ctx, spec, t0)
	mr.HSet(spec.Key, "status", "degraded")
	n.checkServiceHeartbeat(ctx, spec, t0.Add(30*time.Second))
	mr.HSet(spec.Key, "status", "ok", "generated_at", t0.Add(70*time.Second).Format(time.RFC3339Nano))
	n.checkServiceHeartbeat(ctx, spec, t0.Add(70*time.Second))
	if outboxLen(t, n) != 0 {
		t.Fatal("a degraded blip must restart the recovery hold")
	}
}

func TestServiceHeartbeatMaintenanceSuppressesOnlyThatService(t *testing.T) {
	mr := miniredis.RunT(t)
	n := newTestNotifier(t, mr, "tok", "cid")
	ctx := context.Background()
	t0 := time.Date(2026, 9, 26, 12, 0, 0, 0, time.UTC)
	quiet := serviceHeartbeat{Name: "exit", Key: "market:exit:health", MaxAge: time.Minute}
	other := serviceHeartbeat{Name: "other", Key: "market:other:health", MaxAge: time.Minute}
	if err := mr.Set(maintenanceKey("exit"), "deploy"); err != nil {
		t.Fatal(err)
	}
	mr.SetTTL(maintenanceKey("exit"), 15*time.Minute)

	for _, at := range []time.Duration{0, 61 * time.Second} {
		n.checkServiceHeartbeat(ctx, quiet, t0.Add(at))
		n.checkServiceHeartbeat(ctx, other, t0.Add(at))
	}
	if outboxLen(t, n) != 1 {
		t.Fatalf("alerts = %d, want 1 (only the service not in maintenance)", outboxLen(t, n))
	}
	if mr.Exists(heartbeatStateKey("exit", "unhealthy_since")) {
		t.Fatal("a service in maintenance must not start its debounce")
	}
}

func TestSourceLeadQuietWarnsOnlyWhileTheScannerIsFresh(t *testing.T) {
	mr := miniredis.RunT(t)
	n := newTestNotifier(t, mr, "tok", "cid")
	n.sourceLeadCaptureEnabled = true
	ctx := context.Background()
	sevenHours := (7 * time.Hour).Seconds()
	oneHour := time.Hour.Seconds()

	// Scanner stale: the scanner alert covers it, no quiet warning.
	n.reportSourceLeadQuiet(ctx, &sevenHours)
	if outboxLen(t, n) != 0 {
		t.Fatal("no quiet warning while the scanner itself is stale")
	}

	setPumpsPayload(t, mr, payload{Scanned: []string{"gate", "bybit"}})
	n.reportSourceLeadQuiet(ctx, &sevenHours)
	n.reportSourceLeadQuiet(ctx, &sevenHours)
	if outboxLen(t, n) != 1 {
		t.Fatalf("quiet warnings = %d, want 1", outboxLen(t, n))
	}
	// Rows vanishing is not a recovery; only a new row is.
	n.reportSourceLeadQuiet(ctx, nil)
	if outboxLen(t, n) != 1 {
		t.Fatal("no row at all must never send a recovery")
	}
	n.reportSourceLeadQuiet(ctx, &oneHour)
	if outboxLen(t, n) != 2 {
		t.Fatalf("after resume = %d, want 2 (one recovery)", outboxLen(t, n))
	}
}

func TestSourceLeadQuietCountsAnEmptyTableFromFirstObservation(t *testing.T) {
	mr := miniredis.RunT(t)
	n := newTestNotifier(t, mr, "tok", "cid")
	n.sourceLeadCaptureEnabled = true
	ctx := context.Background()
	setPumpsPayload(t, mr, payload{Scanned: []string{"gate"}})

	n.reportSourceLeadQuiet(ctx, nil)
	if outboxLen(t, n) != 0 {
		t.Fatal("an empty table is not quiet yet on first sight")
	}
	started := time.Now().Add(-7 * time.Hour).Unix()
	if err := mr.Set(redisKeySourceLeadEmptySince, strconv.FormatInt(started, 10)); err != nil {
		t.Fatal(err)
	}
	n.reportSourceLeadQuiet(ctx, nil)
	if outboxLen(t, n) != 1 {
		t.Fatal("no capture ever for 7 h must warn")
	}
}

func TestSourceLeadQuietIsSilentWhenCaptureIsDisabled(t *testing.T) {
	mr := miniredis.RunT(t)
	n := newTestNotifier(t, mr, "tok", "cid")
	n.sourceLeadCaptureEnabled = false
	setPumpsPayload(t, mr, payload{Scanned: []string{"gate"}})
	sevenHours := (7 * time.Hour).Seconds()
	n.reportSourceLeadQuiet(context.Background(), &sevenHours)
	if outboxLen(t, n) != 0 {
		t.Fatal("intentionally disabled capture must not warn")
	}
}

func TestSourceLeadQuietNamesAMissingGateScan(t *testing.T) {
	mr := miniredis.RunT(t)
	n := newTestNotifier(t, mr, "tok", "cid")
	n.sourceLeadCaptureEnabled = true
	setPumpsPayload(t, mr, payload{Scanned: []string{"bybit"}})
	sevenHours := (7 * time.Hour).Seconds()
	n.reportSourceLeadQuiet(context.Background(), &sevenHours)
	entries := n.rdb.XRange(context.Background(), StreamOutboxV1, "-", "+").Val()
	if len(entries) != 1 || !strings.Contains(entries[0].Values["data"].(string), "Gate is missing") {
		t.Fatalf("want a Gate-specific quiet warning, got %v", entries)
	}
}

func TestSourceLeadCaptureEnabledFromEnvMatchesAnalytics(t *testing.T) {
	for value, want := range map[string]bool{"": true, "true": true, "1": true, "false": false, "OFF": false, "no": false} {
		t.Setenv("SOURCE_LEAD_CAPTURE_ENABLED", value)
		if got := sourceLeadCaptureEnabledFromEnv(); got != want {
			t.Errorf("%q: got %v, want %v", value, got, want)
		}
	}
}
