package main

import (
	"sync"

	"github.com/mavlevich/schurfer/collector/internal/binance"
)

// bookTickerMailbox is a bounded latest-value mailbox, not an event FIFO.
// The one-minute capture contract persists the last bid/ask seen for a
// symbol; retaining thousands of superseded quotes for the same liquid
// symbol only creates head-of-line blocking at minute flush boundaries.
//
// Offer is safe for the per-shard websocket goroutines. Take is owned by the
// application's single event loop. At most one pending value exists per
// symbol, so a correctly scoped mailbox can never exceed the frozen universe.
type bookTickerMailbox struct {
	mu        sync.Mutex
	pending   map[string]binance.PublicBookTicker
	capacity  int
	ready     chan struct{}
	peak      int
	coalesced uint64
	dropped   uint64
}

type bookTickerMailboxStats struct {
	Depth          int
	Peak           int
	CoalescedTotal uint64
	DropsTotal     uint64
}

func newBookTickerMailbox(capacity int) *bookTickerMailbox {
	if capacity < 1 {
		capacity = 1
	}
	return &bookTickerMailbox{
		pending:  make(map[string]binance.PublicBookTicker, capacity),
		capacity: capacity,
		ready:    make(chan struct{}, 1),
	}
}

func (m *bookTickerMailbox) Ready() <-chan struct{} { return m.ready }

// Offer keeps the newest exchange event for a symbol. Replacing a pending
// value is coalescing, not data loss: the superseded quote could never become
// the persisted last quote once a newer event for the same symbol exists.
func (m *bookTickerMailbox) Offer(update binance.PublicBookTicker) {
	m.mu.Lock()
	if current, exists := m.pending[update.Symbol]; exists {
		m.coalesced++
		if bookTickerNewer(update, current) {
			m.pending[update.Symbol] = update
		}
		m.mu.Unlock()
		m.signal()
		return
	}
	if len(m.pending) >= m.capacity {
		m.dropped++
		m.mu.Unlock()
		return
	}
	m.pending[update.Symbol] = update
	if len(m.pending) > m.peak {
		m.peak = len(m.pending)
	}
	m.mu.Unlock()
	m.signal()
}

func bookTickerNewer(candidate, current binance.PublicBookTicker) bool {
	if candidate.EventAt.After(current.EventAt) {
		return true
	}
	if candidate.EventAt.Before(current.EventAt) {
		return false
	}
	return !candidate.ReceivedAt.Before(current.ReceivedAt)
}

func (m *bookTickerMailbox) signal() {
	select {
	case m.ready <- struct{}{}:
	default:
	}
}

// Take removes at most limit symbols so the high-rate quote stream cannot
// starve trades, OI, mark price, flushes, or health publication.
func (m *bookTickerMailbox) Take(limit int) []binance.PublicBookTicker {
	if limit < 1 {
		return nil
	}
	m.mu.Lock()
	items := make([]binance.PublicBookTicker, 0, min(limit, len(m.pending)))
	for symbol, update := range m.pending {
		items = append(items, update)
		delete(m.pending, symbol)
		if len(items) == limit {
			break
		}
	}
	remaining := len(m.pending)
	m.mu.Unlock()
	if remaining > 0 {
		m.signal()
	}
	return items
}

func (m *bookTickerMailbox) Stats() bookTickerMailboxStats {
	m.mu.Lock()
	defer m.mu.Unlock()
	return bookTickerMailboxStats{
		Depth:          len(m.pending),
		Peak:           m.peak,
		CoalescedTotal: m.coalesced,
		DropsTotal:     m.dropped,
	}
}
