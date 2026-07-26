package health

import (
	"context"
	"testing"

	"github.com/alicebob/miniredis/v2"
	"github.com/redis/go-redis/v9"
)

func TestCheckSignalReadiness(t *testing.T) {
	server := miniredis.RunT(t)
	client := redis.NewClient(&redis.Options{Addr: server.Addr()})
	t.Cleanup(func() { _ = client.Close() })

	server.HSet(
		"execution:signal_readiness",
		"updated_at_ms", "1720000000000",
		"pump_count", "7",
		"evaluated", "5",
		"ready", "3",
		"deferred", "2",
		"reasons", `{"signal_missing":2}`,
	)

	got := (&Checker{rdb: client}).checkSignalReadiness(context.Background())
	if got == nil {
		t.Fatal("expected signal-readiness telemetry")
	}
	if got.UpdatedAtMS != 1720000000000 || got.PumpCount != 7 {
		t.Fatalf("unexpected snapshot metadata: %+v", got)
	}
	if got.Evaluated != 5 || got.Ready != 3 || got.Deferred != 2 {
		t.Fatalf("unexpected readiness counts: %+v", got)
	}
	if got.Reasons["signal_missing"] != 2 {
		t.Fatalf("unexpected reasons: %+v", got.Reasons)
	}
}

func TestCheckSignalReadinessOmitsMissingOrMalformedTelemetry(t *testing.T) {
	server := miniredis.RunT(t)
	client := redis.NewClient(&redis.Options{Addr: server.Addr()})
	t.Cleanup(func() { _ = client.Close() })
	checker := &Checker{rdb: client}

	if got := checker.checkSignalReadiness(context.Background()); got != nil {
		t.Fatalf("expected nil for missing telemetry, got %+v", got)
	}

	server.HSet("execution:signal_readiness", "updated_at_ms", "invalid")
	if got := checker.checkSignalReadiness(context.Background()); got != nil {
		t.Fatalf("expected nil for malformed telemetry, got %+v", got)
	}
}
