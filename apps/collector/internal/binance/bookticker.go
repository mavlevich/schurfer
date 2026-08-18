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

// wsPublicBaseURL is Binance's UNROUTED combined-stream endpoint.
// wsMarketBaseURL's own doc comment (trades.go) documents the 2026-04-23
// WebSocket-category split this constant is the other half of: bookTicker
// is one of the streams that stayed on the OLD "public" category and
// still resolves under /stream, not /market/stream. Using wsMarketBaseURL
// here would repeat the exact incident that constant's own comment warns
// about -- a successful-looking handshake that silently never pushes a
// single bookTicker frame.
const wsPublicBaseURL = "wss://fstream.binance.com/stream"

const (
	// Binance's own documented combined-stream limits are not yet verified
	// against a live bounded probe (see wsMarketBaseURL's neighboring
	// tradeStreamsPerConnection comment for the same caveat) -- this shard
	// size matches tradeStreamsPerConnection rather than assuming a wider
	// shard is safe without having verified it live.
	bookTickerStreamsPerConnection = 200
	// A book-ticker update is a state snapshot (best bid/ask right now),
	// not a discrete timestamped event the way a trade is -- there is no
	// meaningful "how far in the future can this legitimately be" bound to
	// enforce the way maxTradeFutureSkew does for aggTrade, so none is
	// applied here.
)

// PublicBookTicker is one normalized Binance best-bid/ask update. Unlike
// PublicTrade, there is no update id kept for diagnostics: streamed
// updateId ("u") is book-internal sequencing, not comparable across
// symbols or reconnects the way AggTradeID is, and nothing here currently
// consumes it.
type PublicBookTicker struct {
	Symbol     string
	EventAt    time.Time
	ReceivedAt time.Time
	BidPrice   float64
	AskPrice   float64
}

type BookTickerFn func(context.Context, PublicBookTicker) error

// BookTickerLifecycleEvent mirrors TradeLifecycleEvent exactly -- same
// shard/session/reconnect semantics, a distinct type only because Go has
// no structural typing and this package's own convention (see
// TradeLifecycleEvent's doc comment) keeps each stream's lifecycle event
// self-contained rather than sharing one generic type across streams with
// otherwise-unrelated payloads.
type BookTickerLifecycleEvent struct {
	ShardSessionID string
	Symbols        []string
	ConnectedAt    time.Time
	DisconnectedAt time.Time
	Reason         string
	ReadTimeout    bool
}

type BookTickerLifecycleFn func(BookTickerLifecycleEvent)

func (s *Source) RunBookTicker(ctx context.Context, symbols []string, consume BookTickerFn) error {
	return s.RunBookTickerWithLifecycle(ctx, symbols, consume, func(BookTickerLifecycleEvent) {})
}

// RunBookTickerWithLifecycle streams bookTicker for symbols, sharded
// across connections of at most bookTickerStreamsPerConnection each --
// same combined-stream-endpoint, one-connection-per-shard design as
// RunTradesWithLifecycle.
func (s *Source) RunBookTickerWithLifecycle(
	ctx context.Context,
	symbols []string,
	consume BookTickerFn,
	onLifecycle BookTickerLifecycleFn,
) error {
	slog.Info("binance.bookticker.subscribing", "count", len(symbols))
	chunks := wsstream.ChunkSlice(symbols, bookTickerStreamsPerConnection)

	var wg sync.WaitGroup
	for _, chunk := range chunks {
		wg.Add(1)
		go func(connectionSymbols []string) {
			defer wg.Done()
			s.bookTickerStreamLoop(ctx, connectionSymbols, consume, onLifecycle)
		}(chunk)
	}
	wg.Wait()
	return nil
}

func (s *Source) bookTickerStreamLoop(
	ctx context.Context,
	symbols []string,
	consume BookTickerFn,
	onLifecycle BookTickerLifecycleFn,
) {
	for {
		if err := s.bookTickerStream(ctx, symbols, consume, onLifecycle); err != nil {
			if ctx.Err() != nil {
				return
			}
			slog.Warn("binance.bookticker.reconnecting", "err", err, "read_timeout", wsstream.IsReadTimeout(err), "delay", reconnectDelay)
		}
		select {
		case <-ctx.Done():
			return
		case <-time.After(reconnectDelay):
		}
	}
}

func (s *Source) bookTickerStream(
	ctx context.Context,
	symbols []string,
	consume BookTickerFn,
	onLifecycle BookTickerLifecycleFn,
) (err error) {
	sessionID, sessionErr := wsstream.NewSessionID(rand.Reader)
	if sessionErr != nil {
		return fmt.Errorf("book ticker stream session id: %w", sessionErr)
	}
	connectedAt := time.Now()
	var connected bool
	defer func() {
		if err != nil && connected {
			onLifecycle(BookTickerLifecycleEvent{
				ShardSessionID: sessionID,
				Symbols:        symbols,
				ConnectedAt:    connectedAt,
				DisconnectedAt: time.Now(),
				Reason:         err.Error(),
				ReadTimeout:    wsstream.IsReadTimeout(err),
			})
		}
	}()

	streamURL := s.bookTickerStreamURL(symbols)
	dialer := websocket.Dialer{HandshakeTimeout: 10 * time.Second}
	conn, response, dialErr := dialer.DialContext(ctx, streamURL, http.Header{})
	if dialErr != nil {
		if response != nil {
			_ = response.Body.Close()
		}
		return fmt.Errorf("dial: %w", dialErr)
	}
	defer func() { _ = conn.Close() }()

	slog.Info("binance.bookticker.connected", "symbols", len(symbols), "session_id", sessionID)
	connected = true
	onLifecycle(BookTickerLifecycleEvent{ShardSessionID: sessionID, Symbols: symbols, ConnectedAt: connectedAt})

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
		if err := handleBookTickerPayload(ctx, payload, time.Now(), consume); err != nil {
			return err
		}
	}
}

func (s *Source) bookTickerStreamURL(symbols []string) string {
	base := s.wsPublicBaseURL
	if base == "" {
		base = wsPublicBaseURL
	}
	streams := make([]string, len(symbols))
	for i, symbol := range symbols {
		streams[i] = strings.ToLower(symbol) + "@bookTicker"
	}
	return base + "?streams=" + strings.Join(streams, "/")
}

// handleBookTickerPayload decodes one combined-stream frame. Field names
// ("e","E","T","s","b","B","a","A") match Binance USD-M futures' own
// documented bookTicker payload -- "b"/"a" are best bid/ask PRICE, "B"/"A"
// their quantities (unused here, this feed exists for spread, not depth).
func handleBookTickerPayload(
	ctx context.Context,
	payload []byte,
	receivedAt time.Time,
	consume BookTickerFn,
) error {
	var envelope struct {
		Stream string `json:"stream"`
		Data   struct {
			EventType string `json:"e"`
			EventAt   int64  `json:"E"`
			Symbol    string `json:"s"`
			BidPrice  string `json:"b"`
			// BidQty/AskQty ("B"/"A") are declared and otherwise unused
			// only so they exist as their own EXACT-case struct fields --
			// encoding/json falls back to a case-INSENSITIVE match when a
			// JSON key has no exact-tag counterpart, so without these two
			// fields present, "B" would silently decode into BidPrice
			// (tag "b") after "b" itself already had, clobbering the real
			// bid price with the bid quantity (same for "A"/AskPrice).
			// Caught by this file's own test
			// (TestHandleBookTickerPayloadNormalizesValidRows) the first
			// time this struct omitted them.
			BidQty   string `json:"B"`
			AskPrice string `json:"a"`
			AskQty   string `json:"A"`
		} `json:"data"`
	}
	if err := json.Unmarshal(payload, &envelope); err != nil {
		return fmt.Errorf("decode book ticker: %w", err)
	}
	if envelope.Data.EventType != "bookTicker" {
		// Not a bookTicker frame -- skipped, not fatal, same convention as
		// handleTradePayload's own unrecognized-frame handling.
		return nil
	}
	data := envelope.Data
	bid, bidErr := strconv.ParseFloat(data.BidPrice, 64)
	ask, askErr := strconv.ParseFloat(data.AskPrice, 64)
	if bidErr != nil || askErr != nil || !wsstream.FinitePositiveNumber(bid) ||
		!wsstream.FinitePositiveNumber(ask) || ask < bid || data.EventAt <= 0 ||
		strings.TrimSpace(data.Symbol) == "" {
		// ask < bid (a crossed book) is treated as a malformed/transient
		// read, same discipline as the other guards here -- a real crossed
		// book cannot persist and is far more likely a parse/framing issue
		// than genuine venue state worth propagating into the engine.
		return nil
	}
	update := PublicBookTicker{
		Symbol:     wsstream.NormalizeSymbol(data.Symbol),
		EventAt:    time.UnixMilli(data.EventAt),
		ReceivedAt: receivedAt,
		BidPrice:   bid,
		AskPrice:   ask,
	}
	return consume(ctx, update)
}
