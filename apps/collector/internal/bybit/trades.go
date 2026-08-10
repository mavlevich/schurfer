package bybit

import (
	"context"
	"crypto/rand"
	"encoding/json"
	"fmt"
	"log/slog"
	"math"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/gorilla/websocket"
)

// PublicTrade is one normalized Bybit linear-perpetual public trade.
// Side is the taker side reported by Bybit. BlockTrade/RPI/Seq are Bybit's
// own "BT"/"RPI"/"seq" fields, stored verbatim: Seq is documented only as
// an integer (no contiguity guarantee), so it is kept for bounded
// diagnostics, never as a gap-detection signal on its own.
type PublicTrade struct {
	Symbol     string
	TradeID    string
	Side       string
	EventAt    time.Time
	ReceivedAt time.Time
	Price      float64
	Size       float64

	BlockTrade bool
	RPI        bool
	Seq        int64
}

type TradeFn func(context.Context, PublicTrade) error

// TradeLifecycleEvent reports one connect or disconnect for a single trade
// WebSocket shard (a shard being a fixed subset of symbols on one physical
// connection, see tradeTopicsPerConnection). Source's own StreamStats()
// reconnect/timeout counters are global across every shard and cannot say
// which one failed; a consumer that needs to mark exactly the affected
// symbols as trade-feed-interrupted (not the whole universe) needs this
// per-shard detail instead.
type TradeLifecycleEvent struct {
	ShardSessionID string
	Symbols        []string
	ConnectedAt    time.Time
	// DisconnectedAt is the zero time for a "connected" event.
	DisconnectedAt time.Time
	// Reason is empty for a "connected" event, and the error text for a
	// "disconnected" event.
	Reason      string
	ReadTimeout bool
}

// TradeLifecycleFn receives every TradeLifecycleEvent as it happens. It is
// called synchronously from the shard's own goroutine and must not block.
type TradeLifecycleFn func(TradeLifecycleEvent)

const (
	maxTradeFutureSkew       = 5 * time.Second
	tradeTopicsPerConnection = 200
)

// RunTrades streams public trades without publishing the raw firehose to
// NATS. It is RunTradesWithLifecycle with a no-op lifecycle callback, kept
// so existing callers (cmd/orderflow) are unaffected.
// Bybit currently permits more futures topics on one public connection. The
// smaller shard is deliberate: it bounds head-of-line blocking and reconnect loss
// while keeping the connection count tiny.
func (s *Source) RunTrades(ctx context.Context, symbols []string, consume TradeFn) error {
	return s.RunTradesWithLifecycle(ctx, symbols, consume, func(TradeLifecycleEvent) {})
}

// RunTradesWithLifecycle is RunTrades plus onLifecycle, fired on every
// shard connect and disconnect with exactly that shard's own symbols and a
// fresh session id per connection attempt (mirroring the ticker decoder's
// StreamSessionID from the bybit-ticker-oi-contract-v1 PR).
func (s *Source) RunTradesWithLifecycle(
	ctx context.Context,
	symbols []string,
	consume TradeFn,
	onLifecycle TradeLifecycleFn,
) error {
	slog.Info("bybit.trades.subscribing", "count", len(symbols))
	chunks := chunkSlice(symbols, tradeTopicsPerConnection)

	var wg sync.WaitGroup
	for _, chunk := range chunks {
		wg.Add(1)
		go func(connectionSymbols []string) {
			defer wg.Done()
			s.tradeStreamLoop(ctx, connectionSymbols, consume, onLifecycle)
		}(chunk)
	}
	wg.Wait()
	return nil
}

func (s *Source) tradeStreamLoop(
	ctx context.Context,
	symbols []string,
	consume TradeFn,
	onLifecycle TradeLifecycleFn,
) {
	for {
		if err := s.tradeStream(ctx, symbols, consume, onLifecycle); err != nil {
			if ctx.Err() != nil {
				return
			}
			readTimedOut := isReadTimeout(err)
			reconnects := s.tradeReconnectTotal.Add(1)
			if readTimedOut {
				s.tradeReadTimeoutTotal.Add(1)
			}
			stats := s.StreamStats()
			slog.Warn(
				"bybit.trades.reconnecting",
				"stream", "public_trade",
				"err", err,
				"read_timeout", readTimedOut,
				"reconnect_total", reconnects,
				"read_timeout_total", stats.TradeReadTimeoutTotal,
				"delay", s.streamConfig.ReconnectDelay,
			)
		}
		select {
		case <-ctx.Done():
			return
		case <-time.After(s.streamConfig.ReconnectDelay):
		}
	}
}

func (s *Source) tradeStream(
	ctx context.Context,
	symbols []string,
	consume TradeFn,
	onLifecycle TradeLifecycleFn,
) (err error) {
	sessionID, sessionErr := newStreamSessionID(rand.Reader)
	if sessionErr != nil {
		return fmt.Errorf("trade stream session id: %w", sessionErr)
	}
	connectedAt := time.Now()
	var connected bool
	// Fires exactly one "disconnected" event per failed connection attempt
	// that actually got as far as subscribing, covering every error-return
	// path below uniformly instead of duplicating the call at each site. A
	// clean shutdown (ctx cancelled, err == nil) fires nothing: it is not a
	// feed interruption.
	defer func() {
		if err != nil && connected {
			onLifecycle(TradeLifecycleEvent{
				ShardSessionID: sessionID,
				Symbols:        symbols,
				ConnectedAt:    connectedAt,
				DisconnectedAt: time.Now(),
				Reason:         err.Error(),
				ReadTimeout:    isReadTimeout(err),
			})
		}
	}()

	dialer := websocket.Dialer{HandshakeTimeout: 10 * time.Second}
	conn, response, dialErr := dialer.DialContext(ctx, s.streamConfig.URL, http.Header{})
	if dialErr != nil {
		if response != nil {
			_ = response.Body.Close()
		}
		return fmt.Errorf("dial: %w", dialErr)
	}
	defer func() { _ = conn.Close() }()

	var writeMu sync.Mutex
	writeJSON := func(value any) error {
		writeMu.Lock()
		defer writeMu.Unlock()
		_ = conn.SetWriteDeadline(time.Now().Add(5 * time.Second))
		return conn.WriteJSON(value)
	}

	topics := make([]string, len(symbols))
	for index, symbol := range symbols {
		topics[index] = "publicTrade." + symbol
	}
	for index := 0; index < len(topics); index += subChunk {
		end := min(index+subChunk, len(topics))
		if err := writeJSON(map[string]any{"op": "subscribe", "args": topics[index:end]}); err != nil {
			return fmt.Errorf("subscribe: %w", err)
		}
	}
	slog.Info("bybit.trades.connected", "symbols", len(symbols), "session_id", sessionID)
	connected = true
	onLifecycle(TradeLifecycleEvent{ShardSessionID: sessionID, Symbols: symbols, ConnectedAt: connectedAt})

	pingCtx, pingCancel := context.WithCancel(ctx)
	pingDone := make(chan struct{})
	go func() {
		defer close(pingDone)
		ticker := time.NewTicker(s.streamConfig.PingInterval)
		defer ticker.Stop()
		for {
			select {
			case <-ticker.C:
				if err := writeJSON(map[string]string{"op": "ping"}); err != nil {
					return
				}
			case <-pingCtx.Done():
				return
			}
		}
	}()
	defer func() { <-pingDone }()
	defer pingCancel()

	stop := context.AfterFunc(ctx, func() {
		_ = conn.SetReadDeadline(time.Now())
	})
	defer stop()
	if err := configureReadLiveness(conn, s.streamConfig.ReadTimeout); err != nil {
		return fmt.Errorf("configure read liveness: %w", err)
	}

	for {
		_, payload, readErr := conn.ReadMessage()
		if readErr != nil {
			if ctx.Err() != nil {
				return nil
			}
			return classifyReadError(readErr)
		}
		if err := refreshReadDeadline(conn, s.streamConfig.ReadTimeout); err != nil {
			return fmt.Errorf("refresh read deadline: %w", err)
		}
		if err := handleTradePayload(ctx, payload, time.Now(), consume); err != nil {
			return err
		}
	}
}

func handleTradePayload(
	ctx context.Context,
	payload []byte,
	receivedAt time.Time,
	consume TradeFn,
) error {
	var message struct {
		Op      string `json:"op"`
		Success *bool  `json:"success"`
		Topic   string `json:"topic"`
		Data    []struct {
			EventAt    int64  `json:"T"`
			Symbol     string `json:"s"`
			Side       string `json:"S"`
			Size       string `json:"v"`
			Price      string `json:"p"`
			TradeID    string `json:"i"`
			BlockTrade bool   `json:"BT"`
			RPI        bool   `json:"RPI"`
			Seq        int64  `json:"seq"`
		} `json:"data"`
	}
	if err := json.Unmarshal(payload, &message); err != nil {
		return fmt.Errorf("decode public trades: %w", err)
	}
	if message.Op == "subscribe" {
		if message.Success != nil && !*message.Success {
			return fmt.Errorf("subscribe nack: %s", string(payload))
		}
		return nil
	}
	if !strings.HasPrefix(message.Topic, "publicTrade.") {
		return nil
	}

	for _, item := range message.Data {
		price, priceErr := strconv.ParseFloat(item.Price, 64)
		size, sizeErr := strconv.ParseFloat(item.Size, 64)
		side := strings.ToLower(strings.TrimSpace(item.Side))
		eventAt := time.UnixMilli(item.EventAt)
		if priceErr != nil || sizeErr != nil || !finitePositiveNumber(price) ||
			!finitePositiveNumber(size) || item.EventAt <= 0 ||
			eventAt.After(receivedAt.Add(maxTradeFutureSkew)) ||
			strings.TrimSpace(item.Symbol) == "" || (side != "buy" && side != "sell") {
			continue
		}
		trade := PublicTrade{
			Symbol:     strings.ToUpper(strings.TrimSpace(item.Symbol)),
			TradeID:    strings.TrimSpace(item.TradeID),
			Side:       side,
			EventAt:    eventAt,
			ReceivedAt: receivedAt,
			Price:      price,
			Size:       size,
			BlockTrade: item.BlockTrade,
			RPI:        item.RPI,
			Seq:        item.Seq,
		}
		if err := consume(ctx, trade); err != nil {
			return err
		}
	}
	return nil
}

func finitePositiveNumber(value float64) bool {
	return !math.IsNaN(value) && !math.IsInf(value, 0) && value > 0
}
