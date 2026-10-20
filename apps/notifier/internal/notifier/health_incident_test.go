package notifier

import (
	"context"
	"encoding/json"
	"sync"
	"testing"

	"github.com/alicebob/miniredis/v2"
)

func queuedEnvelopes(t *testing.T, n *Notifier) []Envelope {
	t.Helper()
	messages, err := n.rdb.XRange(context.Background(), StreamOutboxV1, "-", "+").Result()
	if err != nil {
		t.Fatal(err)
	}
	envelopes := make([]Envelope, 0, len(messages))
	for _, message := range messages {
		var envelope Envelope
		if err := json.Unmarshal([]byte(message.Values["data"].(string)), &envelope); err != nil {
			t.Fatal(err)
		}
		envelopes = append(envelopes, envelope)
	}
	return envelopes
}

func TestHealthIncidentsHaveDistinctDeliveryKeys(t *testing.T) {
	for _, source := range []string{"scanner", "source-lead"} {
		t.Run(source, func(t *testing.T) {
			n := newTestNotifier(t, miniredis.RunT(t), "tok", "cid")
			report := func(unhealthy bool) {
				if source == "scanner" {
					n.reportStaleness(context.Background(), unhealthy, "same reason")
				} else {
					count := 0
					if unhealthy {
						count = 1
					}
					n.reportSourceLeadStaleHealth(context.Background(), count)
				}
			}
			for range 3 {
				report(true)
				report(true) // repeated polling must not create another alert
				report(false)
				report(false)
			}
			envelopes := queuedEnvelopes(t, n)
			if len(envelopes) != 6 {
				t.Fatalf("envelopes = %d, want 6", len(envelopes))
			}
			seen := map[string]bool{}
			for _, envelope := range envelopes {
				if seen[envelope.DedupKey] {
					t.Errorf("different incidents/transitions share delivery key %q", envelope.DedupKey)
				}
				seen[envelope.DedupKey] = true
			}
		})
	}
}

func TestCriticalCaptureDeliveryKeysDoNotCollideWithEachOtherOrStaleHealth(t *testing.T) {
	n := newTestNotifier(t, miniredis.RunT(t), "tok", "cid")
	ctx := context.Background()
	n.reportSourceLeadStaleHealth(ctx, 1)
	for range 2 {
		n.reportSourceLeadCriticalFailure(ctx, 42)
		n.reportSourceLeadCriticalFailure(ctx, 43)
	}
	envelopes := queuedEnvelopes(t, n)
	if len(envelopes) != 3 {
		t.Fatalf("envelopes = %d, want 3", len(envelopes))
	}
	seen := map[string]bool{}
	for _, envelope := range envelopes {
		if seen[envelope.DedupKey] {
			t.Errorf("distinct source failures share delivery key %q", envelope.DedupKey)
		}
		seen[envelope.DedupKey] = true
	}
}

func TestFailedRecoveryKeepsIncidentIdentityAcrossRestart(t *testing.T) {
	mr := miniredis.RunT(t)
	n := newTestNotifier(t, mr, "tok", "cid")
	ctx := context.Background()
	n.reportStaleness(ctx, true, "stale")
	incident, err := n.rdb.Get(ctx, redisKeyStaleAlerted).Result()
	if err != nil {
		t.Fatal(err)
	}
	// A wrong-type stream makes publication fail while Redis state remains readable.
	if err := n.rdb.Del(ctx, StreamOutboxV1).Err(); err != nil {
		t.Fatal(err)
	}
	if err := mr.Set(StreamOutboxV1, "wrong type"); err != nil {
		t.Fatal(err)
	}
	n.reportStaleness(ctx, false, "")
	if got := n.rdb.Get(ctx, redisKeyStaleAlerted).Val(); got != incident {
		t.Fatalf("failed recovery changed incident identity: %q -> %q", incident, got)
	}
	if err := n.rdb.Del(ctx, StreamOutboxV1).Err(); err != nil {
		t.Fatal(err)
	}
	restarted := newTestNotifier(t, mr, "tok", "cid")
	restarted.reportStaleness(ctx, false, "")
	restarted.reportStaleness(ctx, false, "")
	if got := len(queuedEnvelopes(t, restarted)); got != 1 {
		t.Fatalf("recovery envelopes after restart = %d, want 1", got)
	}
}

func TestConcurrentHealthPollingPublishesOneTransition(t *testing.T) {
	n := newTestNotifier(t, miniredis.RunT(t), "tok", "cid")
	ctx := context.Background()
	for _, unhealthy := range []bool{true, false} {
		var workers sync.WaitGroup
		for range 12 {
			workers.Go(func() { n.reportStaleness(ctx, unhealthy, "stale") })
		}
		workers.Wait()
	}
	if got := len(queuedEnvelopes(t, n)); got != 2 {
		t.Fatalf("envelopes = %d, want one alert and one recovery", got)
	}
}

func TestFailedIncidentPublicationDoesNotClaimMarker(t *testing.T) {
	for _, source := range []string{"scanner", "source-lead", "capture"} {
		t.Run(source, func(t *testing.T) {
			mr := miniredis.RunT(t)
			n := newTestNotifier(t, mr, "tok", "cid")
			ctx := context.Background()
			stateKey := redisKeyStaleAlerted
			report := func() { n.reportStaleness(ctx, true, "stale") }
			switch source {
			case "source-lead":
				stateKey = redisKeySourceLeadHealthAlerted
				report = func() { n.reportSourceLeadStaleHealth(ctx, 1) }
			case "capture":
				stateKey = redisKeySourceLeadFailureSeen + "42"
				report = func() { n.reportSourceLeadCriticalFailure(ctx, 42) }
			}
			if err := mr.Set(StreamOutboxV1, "wrong type"); err != nil {
				t.Fatal(err)
			}
			report()
			if mr.Exists(stateKey) {
				t.Fatal("failed publication claimed the incident")
			}
			if err := n.rdb.Del(ctx, StreamOutboxV1).Err(); err != nil {
				t.Fatal(err)
			}
			report()
			report()
			if got := len(queuedEnvelopes(t, n)); got != 1 {
				t.Fatalf("retry envelopes = %d, want 1", got)
			}
		})
	}
}

func TestLegacyIncidentMarkersRecoverOnce(t *testing.T) {
	for _, source := range []string{"scanner", "source-lead"} {
		t.Run(source, func(t *testing.T) {
			mr := miniredis.RunT(t)
			n := newTestNotifier(t, mr, "tok", "cid")
			key := redisKeyStaleAlerted
			report := func() { n.reportStaleness(context.Background(), false, "") }
			if source == "source-lead" {
				key = redisKeySourceLeadHealthAlerted
				report = func() { n.reportSourceLeadStaleHealth(context.Background(), 0) }
			}
			if err := mr.Set(key, "1792400000"); err != nil {
				t.Fatal(err)
			}
			report()
			report()
			if got := len(queuedEnvelopes(t, n)); got != 1 {
				t.Fatalf("legacy recovery envelopes = %d, want 1", got)
			}
			if mr.Exists(key) {
				t.Fatal("legacy incident did not clear")
			}
		})
	}
}
