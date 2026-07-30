package health

import (
	"context"
	"os"
	"path/filepath"
	"testing"

	"github.com/alicebob/miniredis/v2"
	"github.com/redis/go-redis/v9"
)

func TestReadProcSnapshot(t *testing.T) {
	root := t.TempDir()
	writeFixture := func(name, value string) {
		t.Helper()
		if err := os.WriteFile(filepath.Join(root, name), []byte(value), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	writeFixture("loadavg", "0.42 0.21 0.10 1/100 123\n")
	writeFixture(
		"meminfo",
		"MemTotal:       4096000 kB\nMemFree:        1000000 kB\nMemAvailable:   3072000 kB\n",
	)
	writeFixture("uptime", "86461.5 100.0\n")

	got, ok := readProcSnapshot(root)
	if !ok {
		t.Fatal("expected valid proc snapshot")
	}
	if got.load1M != 0.42 || got.load5M != 0.21 || got.load15M != 0.10 {
		t.Fatalf("unexpected load averages: %+v", got)
	}
	if got.memoryTotalBytes != 4096000*1024 || got.memoryUsedBytes != 1024000*1024 {
		t.Fatalf("unexpected memory values: %+v", got)
	}
	if got.uptimeSeconds != 86461.5 {
		t.Fatalf("unexpected uptime: %f", got.uptimeSeconds)
	}
}

func TestReadProcSnapshotRejectsMissingAvailableMemory(t *testing.T) {
	root := t.TempDir()
	for name, value := range map[string]string{
		"loadavg": "0.42 0.21 0.10 1/100 123\n",
		"meminfo": "MemTotal: 4096000 kB\n",
		"uptime":  "1.0 0.0\n",
	} {
		if err := os.WriteFile(filepath.Join(root, name), []byte(value), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	if _, ok := readProcSnapshot(root); ok {
		t.Fatal("expected missing MemAvailable to fail closed")
	}
}

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

func TestCheckMarketPipeline(t *testing.T) {
	server := miniredis.RunT(t)
	client := redis.NewClient(&redis.Options{Addr: server.Addr()})
	t.Cleanup(func() { _ = client.Close() })
	server.HSet(
		"market:hotset:health",
		"updated_at_ms", "1785245544572",
		"observed_symbols", "696",
		"hot_symbols", "4",
		"event_rate_per_sec", "1843.40",
		"last_lag_ms", "83",
		"max_lag_ms", "2617",
		"nats_dropped_total", "2",
		"pending_dropped_total", "3",
		"persist_errors_total", "1",
		"bars_persisted_total", "10",
		"pump_feed_status", "ok",
	)

	got := (&Checker{rdb: client}).checkMarketPipeline(context.Background())
	if got == nil {
		t.Fatal("expected market pipeline telemetry")
	}
	if got.ObservedSymbols != 696 || got.HotSymbols != 4 {
		t.Fatalf("unexpected symbol counts: %+v", got)
	}
	if got.EventRatePerSecond != 1843.40 || got.LastLagMS != 83 {
		t.Fatalf("unexpected throughput: %+v", got)
	}
	if got.NATSDroppedTotal+got.PendingDroppedTotal != 5 {
		t.Fatalf("unexpected drops: %+v", got)
	}
}

func TestCheckMarketPipelineOmitsMalformedTelemetry(t *testing.T) {
	server := miniredis.RunT(t)
	client := redis.NewClient(&redis.Options{Addr: server.Addr()})
	t.Cleanup(func() { _ = client.Close() })
	checker := &Checker{rdb: client}

	if got := checker.checkMarketPipeline(context.Background()); got != nil {
		t.Fatalf("expected nil for missing telemetry, got %+v", got)
	}
	server.HSet(
		"market:hotset:health",
		"updated_at_ms", "1785245544572",
		"observed_symbols", "696",
		"event_rate_per_sec", "invalid",
	)
	if got := checker.checkMarketPipeline(context.Background()); got != nil {
		t.Fatalf("expected nil for malformed telemetry, got %+v", got)
	}
}
