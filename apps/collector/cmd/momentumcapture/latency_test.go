package main

import (
	"testing"
	"time"
)

func TestLatencyHistogramReportsBoundedPercentilesAndExactMaximum(t *testing.T) {
	t.Parallel()
	var histogram latencyHistogram
	for range 95 {
		histogram.observe(80 * time.Microsecond)
	}
	for range 4 {
		histogram.observe(4 * time.Millisecond)
	}
	histogram.observe(42 * time.Second)

	summary := histogram.summary()
	if summary.P50 != 100*time.Microsecond {
		t.Fatalf("p50 = %v, want 100us bucket", summary.P50)
	}
	if summary.P95 != 100*time.Microsecond {
		t.Fatalf("p95 = %v, want 100us bucket", summary.P95)
	}
	if summary.P99 != 5*time.Millisecond {
		t.Fatalf("p99 = %v, want 5ms bucket", summary.P99)
	}
	if summary.Max != 42*time.Second {
		t.Fatalf("max = %v, want exact 42s", summary.Max)
	}
}

func TestLatencyHistogramClampsNegativeDurations(t *testing.T) {
	t.Parallel()
	var histogram latencyHistogram
	histogram.observe(-time.Second)
	summary := histogram.summary()
	if summary.Max != 0 || summary.P99 != 10*time.Microsecond {
		t.Fatalf("negative duration was not clamped: %+v", summary)
	}
}
