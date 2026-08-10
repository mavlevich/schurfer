package bybit

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
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
	readTimeout  = 3 * pingInterval
	reconnDelay  = 5 * time.Second
)

var errReadTimeout = errors.New("websocket read timeout")

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
	epoch := 0
	for {
		if err := s.stream(ctx, symbols, state, epoch, publish); err != nil {
			if ctx.Err() != nil {
				return
			}
			readTimedOut := isReadTimeout(err)
			reconnects := s.tickerReconnectTotal.Add(1)
			if readTimedOut {
				s.tickerReadTimeoutTotal.Add(1)
			}
			stats := s.StreamStats()
			slog.Warn(
				"bybit.reconnecting",
				"stream", "ticker",
				"err", err,
				"read_timeout", readTimedOut,
				"reconnect_total", reconnects,
				"read_timeout_total", stats.TickerReadTimeoutTotal,
				"delay", s.streamConfig.ReconnectDelay,
			)
		}
		// A reconnect gets a fresh Bybit snapshot for every resubscribed
		// topic, but until it arrives we do not know OI is still current.
		// OI state (unlike price/bid/ask, which have no observed-at and an
		// existing consumer whose behavior stays untouched) is reset here so
		// a stale value from the previous episode can never be republished
		// as if it were still fresh.
		resetOpenInterestState(state)
		epoch++
		select {
		case <-ctx.Done():
			return
		case <-time.After(s.streamConfig.ReconnectDelay):
		}
	}
}

func resetOpenInterestState(state map[string]tickerState) {
	for symbol, st := range state {
		st.OpenInterest = ""
		st.OpenInterestEventAtMs = 0
		st.OpenInterestObservedAtMs = 0
		st.OpenInterestValue = ""
		st.OpenInterestValueEventAtMs = 0
		st.OpenInterestValueObservedAtMs = 0
		state[symbol] = st
	}
}

// newStreamSessionID returns a random identifier for one physical
// connection, read from source (crypto/rand.Reader in production, an
// injectable io.Reader in tests so the error path is exercisable without
// swapping the package-level crypto/rand.Reader). Unlike ReconnectEpoch (a
// simple per-process ordinal that restarts at 0 every time the process
// restarts, and so cannot tell a fresh process from one that has run for
// days), this is generated fresh on every dial and is therefore, with
// overwhelming probability, unique across both reconnects and process
// restarts. A downstream consumer should treat a change in StreamSessionID
// as the authoritative "this is a different physical connection" boundary.
//
// Fails closed: a source read failure returns an error rather than a fixed
// fallback string. A fallback would be worse than an outright failure here,
// since every subsequent connection attempt would silently share that same
// non-unique value, defeating the exact guarantee this field exists to
// provide without anything downstream ever noticing.
func newStreamSessionID(source io.Reader) (string, error) {
	var b [8]byte
	if _, err := io.ReadFull(source, b[:]); err != nil {
		return "", fmt.Errorf("read random bytes: %w", err)
	}
	return hex.EncodeToString(b[:]), nil
}

func (s *Source) stream(
	ctx context.Context,
	symbols []string,
	state map[string]tickerState,
	epoch int,
	publish PublishFn,
) error {
	sessionID, err := newStreamSessionID(rand.Reader)
	if err != nil {
		return fmt.Errorf("stream session id: %w", err)
	}
	dialer := websocket.Dialer{HandshakeTimeout: 10 * time.Second}
	conn, resp, dialErr := dialer.DialContext(ctx, s.streamConfig.URL, http.Header{})
	if dialErr != nil {
		if resp != nil {
			_ = resp.Body.Close()
		}
		return fmt.Errorf("dial: %w", dialErr)
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
		t := time.NewTicker(s.streamConfig.PingInterval)
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
	if err := configureReadLiveness(conn, s.streamConfig.ReadTimeout); err != nil {
		return fmt.Errorf("configure read liveness: %w", err)
	}

	for {
		_, b, err := conn.ReadMessage()
		if err != nil {
			if ctx.Err() != nil {
				return nil
			}
			return classifyReadError(err)
		}
		if err := refreshReadDeadline(conn, s.streamConfig.ReadTimeout); err != nil {
			return fmt.Errorf("refresh read deadline: %w", err)
		}

		if err := handleTickerFrame(ctx, b, time.Now(), epoch, sessionID, state, publish); err != nil {
			return err
		}
	}
}

// handleTickerFrame decodes one raw WebSocket frame's envelope (op, topic,
// type, cs, ts) and dispatches it, mirroring handleTradePayload's shape so
// the full decode path, not just tickerState.merge in isolation, is
// directly testable against real Bybit JSON payloads.
func handleTickerFrame(
	ctx context.Context,
	frame []byte,
	receivedAt time.Time,
	epoch int,
	sessionID string,
	state map[string]tickerState,
	publish PublishFn,
) error {
	var msg struct {
		Op      string          `json:"op"`
		Success *bool           `json:"success"`
		Topic   string          `json:"topic"`
		Type    string          `json:"type"`
		CS      *int64          `json:"cs"`
		TS      int64           `json:"ts"`
		Data    json.RawMessage `json:"data"`
	}
	if err := json.Unmarshal(frame, &msg); err != nil {
		return nil //nolint:nilerr // malformed frames are skipped, not fatal to the connection
	}

	switch {
	case msg.Op == "subscribe":
		if msg.Success != nil && !*msg.Success {
			return fmt.Errorf("subscribe nack: %s", string(frame))
		}
	case strings.HasPrefix(msg.Topic, "tickers."):
		handleTicker(tickerMessage{
			data:        msg.Data,
			ts:          msg.TS,
			messageType: msg.Type,
			crossSeq:    msg.CS,
			receivedAt:  receivedAt,
			epoch:       epoch,
			sessionID:   sessionID,
		}, state, publish, ctx)
	}
	return nil
}

// tickerMessage carries everything handleTicker needs from one decoded
// WebSocket frame, beyond the raw "data" payload: Bybit's own message
// envelope fields (ts, type, cs) plus this process's own receive time,
// reconnect episode, and stream session id.
type tickerMessage struct {
	data        json.RawMessage
	ts          int64
	messageType string
	crossSeq    *int64
	receivedAt  time.Time
	epoch       int
	sessionID   string
}

func refreshReadDeadline(conn *websocket.Conn, timeout time.Duration) error {
	return conn.SetReadDeadline(time.Now().Add(timeout))
}

func configureReadLiveness(conn *websocket.Conn, timeout time.Duration) error {
	if err := refreshReadDeadline(conn, timeout); err != nil {
		return err
	}
	pingHandler := conn.PingHandler()
	conn.SetPingHandler(func(message string) error {
		if err := refreshReadDeadline(conn, timeout); err != nil {
			return err
		}
		return pingHandler(message)
	})
	pongHandler := conn.PongHandler()
	conn.SetPongHandler(func(message string) error {
		if err := refreshReadDeadline(conn, timeout); err != nil {
			return err
		}
		return pongHandler(message)
	})
	return nil
}

func isReadTimeout(err error) bool {
	return errors.Is(err, errReadTimeout)
}

func classifyReadError(err error) error {
	var netErr net.Error
	if errors.As(err, &netErr) && netErr.Timeout() {
		return fmt.Errorf("read frame: %w: %w", errReadTimeout, err)
	}
	return fmt.Errorf("read frame: %w", err)
}

func handleTicker(msg tickerMessage, state map[string]tickerState, publish PublishFn, ctx context.Context) {
	var fields tickerFields
	if err := json.Unmarshal(msg.data, &fields); err != nil || fields.Symbol == "" {
		return
	}
	receivedAtMs := msg.receivedAt.UnixMilli()
	cur := state[fields.Symbol]
	cur.merge(fields, msg.ts, receivedAtMs)
	state[fields.Symbol] = cur

	event := cur.toEvent(msg.ts, receivedAtMs, msg.messageType, msg.crossSeq, msg.epoch, msg.sessionID)
	if err := publish(ctx, event); err != nil {
		slog.Warn("bybit.publish_failed", "symbol", fields.Symbol, "err", err)
	}
}

// tickerFields maps the raw Bybit WebSocket data fields. OpenInterest is the
// contract quantity; OpenInterestValue is its USD notional. Both are needed
// by the momentum-capture line (ROADMAP "Active course" item 5): OI alone
// does not say whether growth is cheap-contract noise or real notional size.
type tickerFields struct {
	Symbol            string `json:"symbol"`
	LastPrice         string `json:"lastPrice"`
	Price24hPct       string `json:"price24hPcnt"`
	High24h           string `json:"highPrice24h"`
	Low24h            string `json:"lowPrice24h"`
	Volume24h         string `json:"volume24h"`
	Turnover24h       string `json:"turnover24h"`
	Bid               string `json:"bid1Price"`
	Ask               string `json:"ask1Price"`
	OpenInterest      string `json:"openInterest"`
	OpenInterestValue string `json:"openInterestValue"`
}

// tickerState holds the merged snapshot+delta state for one symbol.
//
// Each OI field carries two timestamps, and they mean different things:
// EventAtMs is Bybit's own message ts for the last message that actually
// carried a fresh value for that field (exchange time); ObservedAtMs is
// this collector's own wall-clock receive time for that same message. A
// delta republishes the last known OI even when only price changed, so
// without tracking these separately from the current message's own ts/
// receive time, a consumer would misattribute an old OI value to whatever
// message happens to arrive next.
type tickerState struct {
	Symbol                        string
	LastPrice                     string
	Price24hPct                   string
	High24h                       string
	Low24h                        string
	Volume24h                     string
	Turnover24h                   string
	Bid                           string
	Ask                           string
	OpenInterest                  string
	OpenInterestEventAtMs         int64
	OpenInterestObservedAtMs      int64
	OpenInterestValue             string
	OpenInterestValueEventAtMs    int64
	OpenInterestValueObservedAtMs int64
}

func (st *tickerState) merge(f tickerFields, eventAtMs int64, receivedAtMs int64) {
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
	if f.OpenInterest != "" {
		st.OpenInterest = f.OpenInterest
		st.OpenInterestEventAtMs = eventAtMs
		st.OpenInterestObservedAtMs = receivedAtMs
	}
	if f.OpenInterestValue != "" {
		st.OpenInterestValue = f.OpenInterestValue
		st.OpenInterestValueEventAtMs = eventAtMs
		st.OpenInterestValueObservedAtMs = receivedAtMs
	}
}

func (st tickerState) toEvent(
	ts int64,
	receivedAtMs int64,
	messageType string,
	crossSeq *int64,
	epoch int,
	sessionID string,
) TickerEvent {
	return TickerEvent{
		SchemaVersion:                 1,
		Source:                        "bybit",
		Symbol:                        st.Symbol,
		TS:                            ts,
		LastPrice:                     nonEmpty(st.LastPrice),
		Price24hPct:                   nonEmpty(st.Price24hPct),
		High24h:                       nonEmpty(st.High24h),
		Low24h:                        nonEmpty(st.Low24h),
		Volume24h:                     nonEmpty(st.Volume24h),
		Turnover24h:                   nonEmpty(st.Turnover24h),
		Bid:                           nonEmpty(st.Bid),
		Ask:                           nonEmpty(st.Ask),
		OpenInterest:                  nonEmpty(st.OpenInterest),
		OpenInterestEventAtMs:         nonZero(st.OpenInterestEventAtMs),
		OpenInterestObservedAtMs:      nonZero(st.OpenInterestObservedAtMs),
		OpenInterestValue:             nonEmpty(st.OpenInterestValue),
		OpenInterestValueEventAtMs:    nonZero(st.OpenInterestValueEventAtMs),
		OpenInterestValueObservedAtMs: nonZero(st.OpenInterestValueObservedAtMs),
		ReceivedAtMs:                  receivedAtMs,
		MessageType:                   messageType,
		CrossSequence:                 crossSeq,
		ReconnectEpoch:                epoch,
		StreamSessionID:               sessionID,
	}
}

func nonZero(v int64) *int64 {
	if v == 0 {
		return nil
	}
	return &v
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
