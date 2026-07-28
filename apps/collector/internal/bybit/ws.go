package bybit

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"strings"
	"sync"
	"time"

	"github.com/gorilla/websocket"
)

const (
	wsURL        = "wss://stream.bybit.com/v5/public/linear"
	subChunk     = 10
	maxTopics    = 200
	pingInterval = 20 * time.Second
	reconnDelay  = 5 * time.Second
)

// Run streams tickers for the given symbols. Blocks until ctx is cancelled.
func (s *Source) Run(ctx context.Context, symbols []string, publish PublishFn) error {
	slog.Info("bybit.subscribing", "count", len(symbols))
	chunks := chunkSlice(symbols, maxTopics)

	var wg sync.WaitGroup
	for _, ch := range chunks {
		wg.Add(1)
		go func(syms []string) {
			defer wg.Done()
			s.streamLoop(ctx, syms, publish)
		}(ch)
	}
	wg.Wait()
	return nil
}

func (s *Source) streamLoop(ctx context.Context, symbols []string, publish PublishFn) {
	// Each goroutine owns its state map — no locking needed.
	state := make(map[string]tickerState)
	for {
		if err := s.stream(ctx, symbols, state, publish); err != nil {
			if ctx.Err() != nil {
				return
			}
			slog.Warn("bybit.reconnecting", "err", err, "delay", reconnDelay)
		}
		select {
		case <-ctx.Done():
			return
		case <-time.After(reconnDelay):
		}
	}
}

func (s *Source) stream(ctx context.Context, symbols []string, state map[string]tickerState, publish PublishFn) error {
	dialer := websocket.Dialer{HandshakeTimeout: 10 * time.Second}
	conn, resp, err := dialer.DialContext(ctx, wsURL, http.Header{})
	if err != nil {
		if resp != nil {
			_ = resp.Body.Close()
		}
		return fmt.Errorf("dial: %w", err)
	}
	defer func() { _ = conn.Close() }()

	// Serialise writes: gorilla/websocket allows one concurrent reader + one concurrent writer.
	var wmu sync.Mutex
	writeJSON := func(v any) error {
		wmu.Lock()
		defer wmu.Unlock()
		_ = conn.SetWriteDeadline(time.Now().Add(5 * time.Second))
		return conn.WriteJSON(v)
	}

	slog.Info("bybit.connected", "symbols", len(symbols))

	// Subscribe in batches of subChunk.
	topics := make([]string, len(symbols))
	for i, sym := range symbols {
		topics[i] = "tickers." + sym
	}
	for i := 0; i < len(topics); i += subChunk {
		end := min(i+subChunk, len(topics))
		if err := writeJSON(map[string]any{"op": "subscribe", "args": topics[i:end]}); err != nil {
			return fmt.Errorf("subscribe: %w", err)
		}
	}

	// Ping goroutine — exits on pingCtx cancel or write failure.
	pingCtx, pingCancel := context.WithCancel(ctx)
	pingDone := make(chan struct{})
	go func() {
		defer close(pingDone)
		t := time.NewTicker(pingInterval)
		defer t.Stop()
		for {
			select {
			case <-t.C:
				if err := writeJSON(map[string]string{"op": "ping"}); err != nil {
					return
				}
			case <-pingCtx.Done():
				return
			}
		}
	}()
	// Defer order (LIFO): stop AfterFunc → cancel ping → wait ping → close conn.
	defer func() { <-pingDone }()
	defer pingCancel()

	// Unblock ReadMessage when ctx is cancelled.
	stop := context.AfterFunc(ctx, func() {
		_ = conn.SetReadDeadline(time.Now())
	})
	defer stop()

	for {
		_, b, err := conn.ReadMessage()
		if err != nil {
			if ctx.Err() != nil {
				return nil
			}
			return err
		}

		var msg struct {
			Op      string          `json:"op"`
			Success *bool           `json:"success"`
			Topic   string          `json:"topic"`
			TS      int64           `json:"ts"`
			Data    json.RawMessage `json:"data"`
		}
		if err := json.Unmarshal(b, &msg); err != nil {
			continue
		}

		switch {
		case msg.Op == "subscribe":
			if msg.Success != nil && !*msg.Success {
				return fmt.Errorf("subscribe nack: %s", string(b))
			}
		case strings.HasPrefix(msg.Topic, "tickers."):
			handleTicker(msg.Data, msg.TS, state, publish, ctx)
		}
	}
}

func handleTicker(data json.RawMessage, ts int64, state map[string]tickerState, publish PublishFn, ctx context.Context) {
	var fields tickerFields
	if err := json.Unmarshal(data, &fields); err != nil || fields.Symbol == "" {
		return
	}
	cur := state[fields.Symbol]
	cur.merge(fields)
	state[fields.Symbol] = cur

	if err := publish(ctx, cur.toEvent(ts)); err != nil {
		slog.Warn("bybit.publish_failed", "symbol", fields.Symbol, "err", err)
	}
}

// tickerFields maps the raw Bybit WebSocket data fields.
type tickerFields struct {
	Symbol      string `json:"symbol"`
	LastPrice   string `json:"lastPrice"`
	Price24hPct string `json:"price24hPcnt"`
	High24h     string `json:"highPrice24h"`
	Low24h      string `json:"lowPrice24h"`
	Volume24h   string `json:"volume24h"`
	Turnover24h string `json:"turnover24h"`
	Bid         string `json:"bid1Price"`
	Ask         string `json:"ask1Price"`
}

// tickerState holds the merged snapshot+delta state for one symbol.
type tickerState struct {
	Symbol      string
	LastPrice   string
	Price24hPct string
	High24h     string
	Low24h      string
	Volume24h   string
	Turnover24h string
	Bid         string
	Ask         string
}

func (st *tickerState) merge(f tickerFields) {
	if f.Symbol != "" {
		st.Symbol = f.Symbol
	}
	if f.LastPrice != "" {
		st.LastPrice = f.LastPrice
	}
	if f.Price24hPct != "" {
		st.Price24hPct = f.Price24hPct
	}
	if f.High24h != "" {
		st.High24h = f.High24h
	}
	if f.Low24h != "" {
		st.Low24h = f.Low24h
	}
	if f.Volume24h != "" {
		st.Volume24h = f.Volume24h
	}
	if f.Turnover24h != "" {
		st.Turnover24h = f.Turnover24h
	}
	if f.Bid != "" {
		st.Bid = f.Bid
	}
	if f.Ask != "" {
		st.Ask = f.Ask
	}
}

func (st tickerState) toEvent(ts int64) TickerEvent {
	return TickerEvent{
		SchemaVersion: 1,
		Source:        "bybit",
		Symbol:        st.Symbol,
		TS:            ts,
		LastPrice:     nonEmpty(st.LastPrice),
		Price24hPct:   nonEmpty(st.Price24hPct),
		High24h:       nonEmpty(st.High24h),
		Low24h:        nonEmpty(st.Low24h),
		Volume24h:     nonEmpty(st.Volume24h),
		Turnover24h:   nonEmpty(st.Turnover24h),
		Bid:           nonEmpty(st.Bid),
		Ask:           nonEmpty(st.Ask),
	}
}

func nonEmpty(s string) *string {
	if s == "" {
		return nil
	}
	return &s
}

func chunkSlice[T any](s []T, size int) [][]T {
	var out [][]T
	for i := 0; i < len(s); i += size {
		end := min(i+size, len(s))
		out = append(out, s[i:end])
	}
	return out
}
