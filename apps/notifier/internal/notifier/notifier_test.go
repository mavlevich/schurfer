package notifier

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strconv"
	"testing"
	"time"

	"github.com/alicebob/miniredis/v2"
	"github.com/redis/go-redis/v9"
)

func newTestNotifier(t *testing.T, mr *miniredis.Miniredis, botToken, chatID string) *Notifier {
	t.Helper()
	rdb := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	t.Cleanup(func() { _ = rdb.Close() })
	return &Notifier{
		cfg: Config{
			RedisAddr:  mr.Addr(),
			BotToken:   botToken,
			ChatID:     chatID,
			Interval:   time.Minute,
			StaleAfter: 5 * time.Minute,
		},
		rdb: rdb,
	}
}

func setPumpsPayload(t *testing.T, mr *miniredis.Miniredis, p payload) {
	t.Helper()
	if p.Ts == 0 {
		p.Ts = time.Now().UnixMilli() // default to a fresh scan unless a test sets it
	}
	raw, err := json.Marshal(p)
	if err != nil {
		t.Fatal(err)
	}
	if err := mr.Set(redisKeyPumps, string(raw)); err != nil {
		t.Fatal(err)
	}
}

func TestRun_DisabledSkipsHeartbeat(t *testing.T) {
	mr := miniredis.RunT(t)
	n := newTestNotifier(t, mr, "", "") // no token/chatID

	ctx, cancel := context.WithCancel(context.Background())
	cancel()

	_ = n.Run(ctx)

	if mr.Exists(redisKeyHeartbeat) {
		t.Error("heartbeat must not be written when notifier is disabled")
	}
}

func TestTick_HeartbeatWrittenWhenKeyMissing(t *testing.T) {
	mr := miniredis.RunT(t)
	n := newTestNotifier(t, mr, "tok", "cid")

	// pumps:latest does not exist yet
	_ = n.tick(context.Background())

	if !mr.Exists(redisKeyHeartbeat) {
		t.Error("heartbeat should be written even when pumps:latest is missing")
	}
}

func TestTick_HeartbeatWrittenWhenScannedEmpty(t *testing.T) {
	mr := miniredis.RunT(t)
	n := newTestNotifier(t, mr, "tok", "cid")

	setPumpsPayload(t, mr, payload{Scanned: []string{}, Pumps: nil})
	_ = n.tick(context.Background())

	if !mr.Exists(redisKeyHeartbeat) {
		t.Error("heartbeat should be written even when all exchanges failed (scanned empty)")
	}
}

func TestTick_EmptyScannedNoAlerts(t *testing.T) {
	requests := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		requests++
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()

	orig := _telegramAPI
	_telegramAPI = srv.URL + "/%s/sendMessage"
	defer func() { _telegramAPI = orig }()

	mr := miniredis.RunT(t)
	n := newTestNotifier(t, mr, "tok", "cid")

	setPumpsPayload(t, mr, payload{
		Scanned: []string{}, // all exchanges failed
		Pumps:   []pump{{Base: "BTC", MaxChangePct: 40.0}},
	})

	_ = n.tick(context.Background())

	if requests > 0 {
		t.Errorf("expected 0 Telegram requests for empty scanned, got %d", requests)
	}
}

func TestTick_SuccessfulAlertMarksSeen(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"ok":true}`))
	}))
	defer srv.Close()

	orig := _telegramAPI
	_telegramAPI = srv.URL + "/%s/sendMessage"
	defer func() { _telegramAPI = orig }()

	mr := miniredis.RunT(t)
	n := newTestNotifier(t, mr, "tok", "cid")

	setPumpsPayload(t, mr, payload{
		Scanned: []string{"binance"},
		Pumps: []pump{{Base: "BTC", MaxChangePct: 40.0, Exchanges: []exchange{
			{Exchange: "binance", ChangePct: 40.0, VolumeUSD: 1_000_000},
		}}},
	})

	_ = n.tick(context.Background())

	if !mr.Exists(redisKeySeenPfx + "BTC") {
		t.Error("notifier:seen:BTC should be set after successful alert")
	}
}

func TestTick_FailedAlertDoesNotMarkSeen(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusUnauthorized)
	}))
	defer srv.Close()

	orig := _telegramAPI
	_telegramAPI = srv.URL + "/%s/sendMessage"
	defer func() { _telegramAPI = orig }()

	mr := miniredis.RunT(t)
	n := newTestNotifier(t, mr, "tok", "cid")

	setPumpsPayload(t, mr, payload{
		Scanned: []string{"binance"},
		Pumps: []pump{{Base: "BTC", MaxChangePct: 40.0, Exchanges: []exchange{
			{Exchange: "binance", ChangePct: 40.0, VolumeUSD: 1_000_000},
		}}},
	})

	_ = n.tick(context.Background())

	if mr.Exists(redisKeySeenPfx + "BTC") {
		t.Error("notifier:seen:BTC must NOT be set after failed Telegram send")
	}
}

func TestTick_AlreadySeenSkipsAlert(t *testing.T) {
	requests := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		requests++
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"ok":true}`))
	}))
	defer srv.Close()

	orig := _telegramAPI
	_telegramAPI = srv.URL + "/%s/sendMessage"
	defer func() { _telegramAPI = orig }()

	mr := miniredis.RunT(t)
	n := newTestNotifier(t, mr, "tok", "cid")

	// Mark BTC as already seen
	if err := mr.Set(redisKeySeenPfx+"BTC", "1"); err != nil {
		t.Fatal(err)
	}

	setPumpsPayload(t, mr, payload{
		Scanned: []string{"binance"},
		Pumps: []pump{{Base: "BTC", MaxChangePct: 40.0, Exchanges: []exchange{
			{Exchange: "binance", ChangePct: 40.0, VolumeUSD: 1_000_000},
		}}},
	})

	_ = n.tick(context.Background())

	if requests > 0 {
		t.Errorf("expected 0 alerts for already-seen token, got %d", requests)
	}
}

func TestTick_BelowThresholdNoAlertNotSeen(t *testing.T) {
	calls, done := newTelegramCounter(t)
	defer done()

	mr := miniredis.RunT(t)
	n := newTestNotifier(t, mr, "tok", "cid")
	n.cfg.MinPct = 60

	setPumpsPayload(t, mr, payload{
		Scanned: []string{"binance"},
		Pumps: []pump{{Base: "BTC", MaxChangePct: 40.0, Exchanges: []exchange{
			{Exchange: "binance", ChangePct: 40.0, VolumeUSD: 1_000_000},
		}}},
	})

	_ = n.tick(context.Background())

	if *calls != 0 {
		t.Errorf("expected no alert for a pump below the notifier gate, got %d", *calls)
	}
	// Not marked seen, so it can still alert if it later grows past the gate.
	if mr.Exists(redisKeySeenPfx + "BTC") {
		t.Error("a sub-threshold pump must not be marked seen")
	}
}

func TestTick_AtThresholdAlerts(t *testing.T) {
	calls, done := newTelegramCounter(t)
	defer done()

	mr := miniredis.RunT(t)
	n := newTestNotifier(t, mr, "tok", "cid")
	n.cfg.MinPct = 60

	// Exactly at the threshold must alert: the gate is `< MinPct`, so equal passes.
	setPumpsPayload(t, mr, payload{
		Scanned: []string{"binance"},
		Pumps: []pump{{Base: "BTC", MaxChangePct: 60.0, Exchanges: []exchange{
			{Exchange: "binance", ChangePct: 60.0, VolumeUSD: 1_000_000},
		}}},
	})

	_ = n.tick(context.Background())

	if *calls != 1 {
		t.Errorf("expected 1 alert for a pump exactly at the gate, got %d", *calls)
	}
	if !mr.Exists(redisKeySeenPfx + "BTC") {
		t.Error("a pump past the gate should be marked seen after alerting")
	}
}

// --- scanner staleness alerts ---

func newTelegramCounter(t *testing.T) (*int, func()) {
	t.Helper()
	var calls int
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		calls++
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"ok":true}`))
	}))
	orig := _telegramAPI
	_telegramAPI = srv.URL + "/%s/sendMessage"
	return &calls, func() { _telegramAPI = orig; srv.Close() }
}

func staleUnixMinutesAgo(m int) string {
	return strconv.FormatInt(time.Now().Add(-time.Duration(m)*time.Minute).Unix(), 10)
}

func TestTick_MissingKeyWithinGraceNoAlert(t *testing.T) {
	calls, done := newTelegramCounter(t)
	defer done()

	mr := miniredis.RunT(t)
	n := newTestNotifier(t, mr, "tok", "cid") // clean redis, key missing, no missing-since

	_ = n.tick(context.Background())

	if *calls != 0 {
		t.Errorf("expected no alert within grace on first missing tick, got %d", *calls)
	}
	if !mr.Exists(redisKeyMissingSince) {
		t.Error("missing-since timer should be recorded on the first missing tick")
	}
}

func TestTick_AlertsWhenPumpsMissingPastGrace(t *testing.T) {
	calls, done := newTelegramCounter(t)
	defer done()

	mr := miniredis.RunT(t)
	n := newTestNotifier(t, mr, "tok", "cid")
	if err := mr.Set(redisKeyMissingSince, staleUnixMinutesAgo(10)); err != nil { // missing past grace
		t.Fatal(err)
	}

	_ = n.tick(context.Background())

	if *calls != 1 {
		t.Errorf("expected 1 stale alert, got %d", *calls)
	}
	if !mr.Exists(redisKeyStaleAlerted) {
		t.Error("stale-alerted flag must be set")
	}
}

func TestTick_AlertsWhenMalformedJSON(t *testing.T) {
	calls, done := newTelegramCounter(t)
	defer done()

	mr := miniredis.RunT(t)
	n := newTestNotifier(t, mr, "tok", "cid")
	if err := mr.Set(redisKeyPumps, "{not valid json"); err != nil {
		t.Fatal(err)
	}

	_ = n.tick(context.Background())

	if *calls != 1 {
		t.Errorf("expected 1 alert for malformed payload, got %d", *calls)
	}
	if !mr.Exists(redisKeyStaleAlerted) {
		t.Error("stale-alerted flag must be set on malformed payload")
	}
}

func TestTick_AlertsWhenTimestampInFuture(t *testing.T) {
	calls, done := newTelegramCounter(t)
	defer done()

	mr := miniredis.RunT(t)
	n := newTestNotifier(t, mr, "tok", "cid")
	setPumpsPayload(t, mr, payload{
		Ts:      time.Now().Add(time.Hour).UnixMilli(),
		Scanned: []string{"binance"},
	})

	_ = n.tick(context.Background())

	if *calls != 1 {
		t.Errorf("expected 1 alert for a future timestamp, got %d", *calls)
	}
}

func TestTick_AlertsWhenScanTooOld(t *testing.T) {
	calls, done := newTelegramCounter(t)
	defer done()

	mr := miniredis.RunT(t)
	n := newTestNotifier(t, mr, "tok", "cid")
	setPumpsPayload(t, mr, payload{
		Ts:      time.Now().Add(-10 * time.Minute).UnixMilli(), // older than StaleAfter (5m)
		Scanned: []string{"binance"},
	})

	_ = n.tick(context.Background())

	if *calls != 1 {
		t.Errorf("expected 1 stale alert, got %d", *calls)
	}
	if !mr.Exists(redisKeyStaleAlerted) {
		t.Error("stale-alerted flag must be set")
	}
}

func TestTick_NoDoubleAlertWhenAlreadyStale(t *testing.T) {
	calls, done := newTelegramCounter(t)
	defer done()

	mr := miniredis.RunT(t)
	n := newTestNotifier(t, mr, "tok", "cid")
	if err := mr.Set(redisKeyMissingSince, staleUnixMinutesAgo(10)); err != nil { // past grace
		t.Fatal(err)
	}
	if err := mr.Set(redisKeyStaleAlerted, "1"); err != nil {
		t.Fatal(err)
	}

	_ = n.tick(context.Background()) // missing past grace, but already alerted

	if *calls != 0 {
		t.Errorf("expected no repeat alert, got %d", *calls)
	}
}

func TestTick_RecoveryAlertWhenFreshAgain(t *testing.T) {
	calls, done := newTelegramCounter(t)
	defer done()

	mr := miniredis.RunT(t)
	n := newTestNotifier(t, mr, "tok", "cid")
	if err := mr.Set(redisKeyStaleAlerted, "1"); err != nil { // previously alerted
		t.Fatal(err)
	}
	setPumpsPayload(t, mr, payload{Scanned: []string{"binance"}}) // fresh ts, no pumps

	_ = n.tick(context.Background())

	if *calls != 1 {
		t.Errorf("expected 1 recovery alert, got %d", *calls)
	}
	if mr.Exists(redisKeyStaleAlerted) {
		t.Error("stale-alerted flag must be cleared after recovery")
	}
}

func TestTick_NoStaleAlertWhenFresh(t *testing.T) {
	calls, done := newTelegramCounter(t)
	defer done()

	mr := miniredis.RunT(t)
	n := newTestNotifier(t, mr, "tok", "cid")
	setPumpsPayload(t, mr, payload{Scanned: []string{"binance"}}) // fresh, no pumps, no flag

	_ = n.tick(context.Background())

	if *calls != 0 {
		t.Errorf("expected no alert when fresh, got %d", *calls)
	}
}

func failingTelegram(t *testing.T) func() {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	}))
	orig := _telegramAPI
	_telegramAPI = srv.URL + "/%s/sendMessage"
	return func() { _telegramAPI = orig; srv.Close() }
}

func TestTick_StaleAlertFailureReleasesClaim(t *testing.T) {
	defer failingTelegram(t)()

	mr := miniredis.RunT(t)
	n := newTestNotifier(t, mr, "tok", "cid")
	if err := mr.Set(redisKeyMissingSince, staleUnixMinutesAgo(10)); err != nil {
		t.Fatal(err)
	}

	_ = n.tick(context.Background()) // stale, but the Telegram send fails

	if mr.Exists(redisKeyStaleAlerted) {
		t.Error("claim must be released when the stale alert fails to send, so the next tick retries")
	}
}

func TestTick_RecoveryFailureRestoresFlag(t *testing.T) {
	defer failingTelegram(t)()

	mr := miniredis.RunT(t)
	n := newTestNotifier(t, mr, "tok", "cid")
	if err := mr.Set(redisKeyStaleAlerted, "1"); err != nil { // previously alerted
		t.Fatal(err)
	}
	setPumpsPayload(t, mr, payload{Scanned: []string{"binance"}}) // fresh again

	_ = n.tick(context.Background()) // recovery attempted, but the Telegram send fails

	if !mr.Exists(redisKeyStaleAlerted) {
		t.Error("flag must be restored when the recovery notice fails to send, so recovery retries")
	}
}
