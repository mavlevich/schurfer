package momentum

import (
	"fmt"
	"testing"
	"time"
)

// BenchmarkEngineAddTradeBurst keeps every trade inside the rolling 30-second
// window. It models the exact workload that can fill momentum-capture's input
// queue even when average CPU looks healthy: a single suddenly active symbol.
// Keep this benchmark as a regression gate for the queue-pressure remediation.
func BenchmarkEngineAddTradeBurst(b *testing.B) {
	for _, tradeCount := range []int{100, 1_000, 5_000} {
		b.Run(fmt.Sprintf("trades_%d", tradeCount), func(b *testing.B) {
			b.ReportAllocs()
			b.ReportMetric(float64(tradeCount), "trades/run")
			for range b.N {
				engine := New()
				start := time.Date(2026, 8, 13, 12, 0, 0, 0, time.UTC)
				for index := range tradeCount {
					eventAt := start.Add(time.Duration(index) * time.Millisecond)
					_, err := engine.AddTrade(Trade{
						Symbol:     "BURSTUSDT",
						Side:       SideBuy,
						Price:      1,
						Size:       100,
						EventAt:    eventAt,
						ReceivedAt: eventAt,
						TradeID:    fmt.Sprintf("trade-%d", index),
					})
					if err != nil {
						b.Fatalf("add trade %d: %v", index, err)
					}
				}
			}
		})
	}
}
