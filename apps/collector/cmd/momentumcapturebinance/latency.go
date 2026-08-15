// This file is a deliberate, documented duplicate of
// cmd/momentumcapture/latency.go: a bounded, dependency-free latency
// histogram with no venue-specific logic at all. This is NOT the only
// option -- apps/collector/internal/wsstream proves a shared package
// extraction can be done with bybit's own already-merged call sites left
// completely unchanged (thin same-named wrapper functions), and the same
// trick was available here (a type alias plus wrapper functions would let
// cmd/momentumcapture's unqualified latencyHistogram/durationMicroseconds
// references keep compiling untouched). It was deliberately not taken in
// this PR: unlike wsstream, this is pure future-maintenance risk reduction
// (two copies could drift), not something needed for correctness today,
// and cmd/momentumcapture/main.go is the source of the currently-running
// Bybit canary process -- touching it again this session for a refactor
// with no behavior change is a cost this PR chose not to pay. A future PR
// extracting this (and deriveHealthStatus below, same reasoning) into a
// shared package is legitimate follow-up work, not a closed question.
package main

import "time"

var latencyBounds = [...]time.Duration{
	10 * time.Microsecond,
	25 * time.Microsecond,
	50 * time.Microsecond,
	100 * time.Microsecond,
	250 * time.Microsecond,
	500 * time.Microsecond,
	time.Millisecond,
	2500 * time.Microsecond,
	5 * time.Millisecond,
	10 * time.Millisecond,
	25 * time.Millisecond,
	50 * time.Millisecond,
	100 * time.Millisecond,
	250 * time.Millisecond,
	500 * time.Millisecond,
	time.Second,
	2500 * time.Millisecond,
	5 * time.Second,
	10 * time.Second,
	30 * time.Second,
}

type latencyHistogram struct {
	buckets [len(latencyBounds) + 1]uint64
	count   uint64
	max     time.Duration
}

type latencySummary struct {
	P50 time.Duration
	P95 time.Duration
	P99 time.Duration
	Max time.Duration
}

func (histogram *latencyHistogram) observe(duration time.Duration) {
	if duration < 0 {
		duration = 0
	}
	index := len(latencyBounds)
	for candidate, bound := range latencyBounds {
		if duration <= bound {
			index = candidate
			break
		}
	}
	histogram.buckets[index]++
	histogram.count++
	if duration > histogram.max {
		histogram.max = duration
	}
}

func (histogram latencyHistogram) summary() latencySummary {
	return latencySummary{
		P50: histogram.percentile(50),
		P95: histogram.percentile(95),
		P99: histogram.percentile(99),
		Max: histogram.max,
	}
}

func (histogram latencyHistogram) percentile(percent uint64) time.Duration {
	if histogram.count == 0 {
		return 0
	}
	target := (histogram.count*percent + 99) / 100
	var cumulative uint64
	for index, count := range histogram.buckets {
		cumulative += count
		if cumulative < target {
			continue
		}
		if index == len(latencyBounds) {
			return histogram.max
		}
		return latencyBounds[index]
	}
	return histogram.max
}

func durationMicroseconds(duration time.Duration) int64 {
	return duration.Microseconds()
}
