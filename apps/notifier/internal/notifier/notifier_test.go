package notifier

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
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
			RedisAddr: mr.Addr(),
			BotToken:  botToken,
			ChatID:    chatID,
			Interval:  time.Minute,
		},
		rdb: rdb,
	}
}

func setPumpsPayload(t *testing.T, mr *miniredis.Miniredis, p payload) {
	t.Helper()
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
