package health

import (
	"context"
	"math"
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
		"MemTotal:       4096000 kB\nMemFree:        1000000 kB\nMemAvailable:   3072000 kB\nSwapTotal:      2097152 kB\nSwapFree:       1572864 kB\n",
	)
	writeFixture("vmstat", "pswpin 42\npswpout 7\n")
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
	if got.memAvailableBytes != 3072000*1024 {
		t.Fatalf("unexpected MemAvailable: %+v", got)
	}
	if got.swapTotalBytes != 2097152*1024 || got.swapUsedBytes != 524288*1024 {
		t.Fatalf("unexpected swap values: %+v", got)
	}
	if got.swapInPages != 42 || got.swapOutPages != 7 {
		t.Fatalf("unexpected swap page counters: %+v", got)
	}
	if got.uptimeSeconds != 86461.5 {
		t.Fatalf("unexpected uptime: %f", got.uptimeSeconds)
	}
}

func TestReadVMStatRejectsMissingCounters(t *testing.T) {
	root := t.TempDir()
	path := filepath.Join(root, "vmstat")
	if err := os.WriteFile(path, []byte("pswpin 42\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, _, ok := readVMStat(path); ok {
		t.Fatal("expected missing pswpout to fail closed")
	}
}

func TestSystemSamplerCalculatesIntervalCPUUtilization(t *testing.T) {
	root := t.TempDir()
	writeFixture := func(name, value string) {
		t.Helper()
		if err := os.WriteFile(filepath.Join(root, name), []byte(value), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	writeFixture("loadavg", "1.00 0.50 0.25 1/100 123\n")
	writeFixture(
		"meminfo",
		"MemTotal: 4096000 kB\nMemAvailable: 3072000 kB\nSwapTotal: 1024 kB\nSwapFree: 512 kB\n",
	)
	writeFixture("vmstat", "pswpin 100\npswpout 50\n")
	writeFixture("uptime", "100.0 10.0\n")
	writeFixture("stat", "cpu  100 0 100 800 0 0 0 0 0 0\n")

	sampler := &systemSampler{procRoot: root, diskRoot: root}
	first := sampler.sample()
	if first == nil || first.CPUUtilizationPct != nil {
		t.Fatalf("expected first sample without interval utilization, got %+v", first)
	}
	if first.SwapInBytesPerSec != nil || first.SwapOutBytesPerSec != nil {
		t.Fatalf("expected first sample without an interval swap rate, got %+v", first)
	}
	if first.MemAvailableBytes != 3072000*1024 {
		t.Fatalf("unexpected MemAvailable on first sample: %+v", first)
	}

	// Force the cached-sample short-circuit to be skipped so the second call
	// actually re-reads /proc instead of returning the first sample back.
	sampler.lastRead = sampler.lastRead.Add(-sampler.minInterval)
	writeFixture("stat", "cpu  150 0 150 900 0 0 0 0 0 0\n")
	writeFixture("vmstat", "pswpin 105\npswpout 60\n")
	second := sampler.sample()
	if second == nil || second.CPUUtilizationPct == nil {
		t.Fatalf("expected second sample with utilization, got %+v", second)
	}
	if *second.CPUUtilizationPct != 50 {
		t.Fatalf("expected 50%% CPU utilization, got %f", *second.CPUUtilizationPct)
	}
	if second.SwapInBytesPerSec == nil || second.SwapOutBytesPerSec == nil {
		t.Fatalf("expected an interval swap rate on the second sample, got %+v", second)
	}
	if *second.SwapInBytesPerSec <= 0 || *second.SwapOutBytesPerSec <= 0 {
		t.Fatalf(
			"expected positive swap rates, got in=%f out=%f",
			*second.SwapInBytesPerSec, *second.SwapOutBytesPerSec,
		)
	}
	// The page deltas are 5 in / 10 out, so the ratio is exact regardless of
	// the actual wall-clock elapsed time between the two calls, which this
	// test does not control precisely.
	ratio := *second.SwapOutBytesPerSec / *second.SwapInBytesPerSec
	if math.Abs(ratio-2) > 1e-9 {
		t.Fatalf("expected swap-out rate to be exactly 2x swap-in rate, got ratio %f", ratio)
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

func TestStatusFromPresence(t *testing.T) {
	if statusFromPresence(true) != StatusUp {
		t.Fatal("expected present telemetry to map to StatusUp")
	}
	if statusFromPresence(false) != StatusDown {
		t.Fatal("expected absent telemetry to map to StatusDown")
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
		"window_max_lag_ms", "97",
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
	if got.MaxLagMS != 2617 || got.WindowMaxLagMS != 97 {
		t.Fatalf("unexpected lag fields: %+v", got)
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

func TestCheckOrderflowPilot(t *testing.T) {
	server := miniredis.RunT(t)
	client := redis.NewClient(&redis.Options{Addr: server.Addr()})
	t.Cleanup(func() { _ = client.Close() })
	server.HSet(
		"market:orderflow:health",
		"updated_at_ms", "1785245544572",
		"started_at_ms", "1785241944572",
		"status", "ok",
		"observed_symbols", "696",
		"event_rate_per_sec", "1843.40",
		"active_captures", "2",
		"activation_total", "5",
		"records_persisted_total", "6593",
		"storage_bytes", "594922",
		"storage_bytes_per_day", "9600000",
		"last_lag_ms", "83",
		"window_max_lag_ms", "103",
		"queue_dropped_total", "0",
		"pending_dropped_total", "0",
		"persist_errors_total", "0",
		"storage_limited_total", "0",
		"left_censored_total", "1",
		"capacity_rejected_total", "0",
	)

	got := (&Checker{rdb: client}).checkOrderflowPilot(context.Background())
	if got == nil {
		t.Fatal("expected order-flow telemetry")
	}
	if got.Status != "ok" || got.ObservedSymbols != 696 || got.ActiveCaptures != 2 {
		t.Fatalf("unexpected order-flow state: %+v", got)
	}
	if got.RecordsPersisted != 6593 || got.StorageBytesPerDay != 9600000 {
		t.Fatalf("unexpected order-flow persistence values: %+v", got)
	}
}

func TestCheckOrderflowPilotOmitsMalformedTelemetry(t *testing.T) {
	server := miniredis.RunT(t)
	client := redis.NewClient(&redis.Options{Addr: server.Addr()})
	t.Cleanup(func() { _ = client.Close() })
	checker := &Checker{rdb: client}

	if got := checker.checkOrderflowPilot(context.Background()); got != nil {
		t.Fatalf("expected nil for missing telemetry, got %+v", got)
	}
	server.HSet(
		"market:orderflow:health",
		"updated_at_ms", "invalid",
		"started_at_ms", "1785241944572",
		"status", "ok",
		"observed_symbols", "696",
		"event_rate_per_sec", "1843.40",
		"storage_bytes_per_day", "9600000",
	)
	if got := checker.checkOrderflowPilot(context.Background()); got != nil {
		t.Fatalf("expected nil for malformed telemetry, got %+v", got)
	}
}
