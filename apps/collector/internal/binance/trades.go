package binance

import (
	"context"
	"crypto/rand"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/gorilla/websocket"
	"github.com/mavlevich/schurfer/collector/internal/wsstream"
)

const (
	// wsMarketBaseURL is Binance's routed "market" category combined-stream
	// endpoint. Named explicitly (not just wsBaseURL) so a future stream
	// added to this package -- bookTicker, depth, a private/user-data
	// stream -- cannot casually reuse this constant without whoever adds
	// it noticing the category mismatch: Binance routes WS streams by
	// category (public/market/private) since their 2026-04-23 WebSocket
	// migration (see Binance's own "Important WebSocket Change Notice"),
	// and only "market" streams (aggTrade, markPrice, kline, liquidations)
	// live under /market/stream -- "public" streams (bookTicker among
	// them) still resolve under the old unrouted /stream path, which is
	// exactly why bookTicker kept working during the incident this
	// constant fixes (a code-review/production finding, 2026-08-15): the
	// old unrouted /stream URL completes the WS handshake successfully
	// for a market-category stream name (101 Switching Protocols, no
	// error) but never pushes a single application frame -- transport
	// stays alive via ping/pong (see wsstream.ConfigureReadLiveness),
	// masking what is actually a silent, 100%, indefinite data outage.
	wsMarketBaseURL = "wss://fstream.binance.com/market/stream"
	// Binance's own documented combined-stream limits are not yet verified
	// against a live bounded probe (see docs/research/binance-momentum-
	// capability-preflight-v1.md's own "Live WebSocket throughput -- not
	// measured here" section) -- this shard size intentionally matches
	// bybit.tradeTopicsPerConnection's own conservative choice rather than
	// assuming Binance's stated maximum is safe to use at full width
	// without having verified it live.
	tradeStreamsPerConnection = 200
	pingInterval              = 20 * time.Second
	readTimeout               = 3 * pingInterval
	reconnectDelay            = 5 * time.Second
	maxTradeFutureSkew        = 5 * time.Second
)

// PublicTrade is one normalized Binance aggTrade. Side is DERIVED (see
// momentumsource.ValueProvenance) from Binance's own buyer-maker flag: a
// message where the buyer is the maker means the TAKER was the seller.
// AggTradeID is Binance's own "a" field, stored verbatim -- documented as
// monotonically increasing but not gap-free (100ms aggregation can skip
// ids), kept for diagnostics only, same discipline as bybit.PublicTrade's
// own Seq field.
type PublicTrade struct {
	Symbol     string
	AggTradeID string
	Side       string
	EventAt    time.Time
	ReceivedAt time.Time
	Price      float64
	Size       float64
}

type TradeFn func(context.Context, PublicTrade) error

// TradeLifecycleEvent mirrors bybit.TradeLifecycleEvent exactly -- see its
// own doc comment for the shard/session semantics this preserves.
type TradeLifecycleEvent struct {
	ShardSessionID string
	Symbols        []string
	ConnectedAt    time.Time
	DisconnectedAt time.Time
	Reason         string
	ReadTimeout    bool
}

type TradeLifecycleFn func(TradeLifecycleEvent)

func (s *Source) RunTrades(ctx context.Context, symbols []string, consume TradeFn) error {
	return s.RunTradesWithLifecycle(ctx, symbols, consume, func(TradeLifecycleEvent) {})
}

// RunTradesWithLifecycle streams aggTrade for symbols, sharded across
// connections of at most tradeStreamsPerConnection each, using Binance's
// own combined-stream endpoint (one connection, multiple streams
// multiplexed by name) rather than one connection per symbol.
func (s *Source) RunTradesWithLifecycle(
	ctx context.Context,
	symbols []string,
	consume TradeFn,
	onLifecycle TradeLifecycleFn,
) error {
	slog.Info("binance.trades.subscribing", "count", len(symbols))
	chunks := wsstream.ChunkSlice(symbols, tradeStreamsPerConnection)

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
			slog.Warn("binance.trades.reconnecting", "err", err, "read_timeout", wsstream.IsReadTimeout(err), "delay", reconnectDelay)
		}
		select {
		case <-ctx.Done():
			return
		case <-time.After(reconnectDelay):
		}
	}
}

func (s *Source) tradeStream(
	ctx context.Context,
	symbols []string,
	consume TradeFn,
	onLifecycle TradeLifecycleFn,
) (err error) {
	sessionID, sessionErr := wsstream.NewSessionID(rand.Reader)
	if sessionErr != nil {
		return fmt.Errorf("trade stream session id: %w", sessionErr)
	}
	connectedAt := time.Now()
	var connected bool
	defer func() {
		if err != nil && connected {
			onLifecycle(TradeLifecycleEvent{
				ShardSessionID: sessionID,
				Symbols:        symbols,
				ConnectedAt:    connectedAt,
				DisconnectedAt: time.Now(),
				Reason:         err.Error(),
				ReadTimeout:    wsstream.IsReadTimeout(err),
			})
		}
	}()

	streamURL := s.tradeStreamURL(symbols)
	dialer := websocket.Dialer{HandshakeTimeout: 10 * time.Second}
	conn, response, dialErr := dialer.DialContext(ctx, streamURL, http.Header{})
	if dialErr != nil {
		if response != nil {
			_ = response.Body.Close()
		}
		return fmt.Errorf("dial: %w", dialErr)
	}
	defer func() { _ = conn.Close() }()

	slog.Info("binance.trades.connected", "symbols", len(symbols), "session_id", sessionID)
	connected = true
	onLifecycle(TradeLifecycleEvent{ShardSessionID: sessionID, Symbols: symbols, ConnectedAt: connectedAt})

	// Binance's combined-stream endpoint does not require a client
	// subscribe message (unlike Bybit): the streams are named directly in
	// the connection URL's own query string. A ping/pong keepalive loop is
	// still required the same way -- Binance's own WS servers send a
	// server-side ping every ~3 minutes and expect a pong within 10
	// minutes, but this collector's own read-liveness convention (mirrored
	// from bybit.Source) is a tighter, self-initiated client ping instead
	// of relying solely on the server's cadence.
	pingCtx, pingCancel := context.WithCancel(ctx)
	pingDone := make(chan struct{})
	go func() {
		defer close(pingDone)
		ticker := time.NewTicker(pingInterval)
		defer ticker.Stop()
		for {
			select {
			case <-ticker.C:
				if err := conn.WriteControl(websocket.PingMessage, nil, time.Now().Add(5*time.Second)); err != nil {
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
	if err := wsstream.ConfigureReadLiveness(conn, readTimeout); err != nil {
		return fmt.Errorf("configure read liveness: %w", err)
	}

	for {
		_, payload, readErr := conn.ReadMessage()
		if readErr != nil {
			if ctx.Err() != nil {
				return nil
			}
			return wsstream.ClassifyReadError(readErr)
		}
		if err := wsstream.RefreshReadDeadline(conn, readTimeout); err != nil {
			return fmt.Errorf("refresh read deadline: %w", err)
		}
		if err := handleTradePayload(ctx, payload, time.Now(), consume); err != nil {
			return err
		}
	}
}

func (s *Source) tradeStreamURL(symbols []string) string {
	base := s.wsMarketBaseURL
	if base == "" {
		base = wsMarketBaseURL
	}
	streams := make([]string, len(symbols))
	for i, symbol := range symbols {
		streams[i] = strings.ToLower(symbol) + "@aggTrade"
	}
	return base + "?streams=" + strings.Join(streams, "/")
}

func handleTradePayload(
	ctx context.Context,
	payload []byte,
	receivedAt time.Time,
	consume TradeFn,
) error {
	var envelope struct {
		Stream string `json:"stream"`
		Data   struct {
			EventType  string `json:"e"`
			EventAt    int64  `json:"E"`
			Symbol     string `json:"s"`
			AggTradeID int64  `json:"a"`
			Price      string `json:"p"`
			Size       string `json:"q"`
			TradeAt    int64  `json:"T"`
			BuyerMaker bool   `json:"m"`
		} `json:"data"`
	}
	if err := json.Unmarshal(payload, &envelope); err != nil {
		return fmt.Errorf("decode agg trade: %w", err)
	}
	if envelope.Data.EventType != "aggTrade" {
		// Not a trade frame (e.g. a control/ack frame this endpoint does
		// not normally send, or a frame this decoder does not yet know
		// about) -- skipped, not fatal, same convention as bybit's own
		// handleTickerFrame for frames it does not recognize.
		return nil
	}
	data := envelope.Data
	price, priceErr := strconv.ParseFloat(data.Price, 64)
	size, sizeErr := strconv.ParseFloat(data.Size, 64)
	eventAt := time.UnixMilli(data.TradeAt)
	if priceErr != nil || sizeErr != nil || !wsstream.FinitePositiveNumber(price) ||
		!wsstream.FinitePositiveNumber(size) || data.TradeAt <= 0 ||
		eventAt.After(receivedAt.Add(maxTradeFutureSkew)) ||
		strings.TrimSpace(data.Symbol) == "" {
		return nil
	}
	side := "buy"
	if data.BuyerMaker {
		// Buyer is the maker => the TAKER was the seller.
		side = "sell"
	}
	trade := PublicTrade{
		Symbol:     wsstream.NormalizeSymbol(data.Symbol),
		AggTradeID: strconv.FormatInt(data.AggTradeID, 10),
		Side:       side,
		EventAt:    eventAt,
		ReceivedAt: receivedAt,
		Price:      price,
		Size:       size,
	}
	return consume(ctx, trade)
}
