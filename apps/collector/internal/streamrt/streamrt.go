// Package streamrt is the shared streaming runtime of the realtime capture design
// (docs/engineering/realtime-market-capture-design-v1.md): the runtime owns every
// connection and a venue is only a codec.
//
// The runtime dials, shards instruments within the codec's declared limit, sends the
// codec's subscribe and ping frames, enforces read liveness, reconnects with jittered
// backoff, and assigns a fresh session id per dial. A codec builds frames and parses
// them; it never dials.
//
// Lifecycle events travel in-band on the same ordered stream as the data, so a
// consumer learns of a break at once, not at the next trade:
//
//   - Connected: a dial succeeded and every subscribe frame was written;
//   - Subscribed: the venue acknowledged as many subscriptions as were requested.
//     Data is complete only from here. Acknowledgements name no instrument on the
//     supported venues, so this is a count match. Without it within SubscribeTimeout
//     the session ends;
//   - Disconnected: the session ended (error, liveness timeout or a codec error).
//     LastFrameAt is the last frame received: the gap starts there, not at the
//     detection, which can come a whole read timeout later;
//   - Heartbeat: the connection is alive; LastFrameAt is its latest received frame. At
//     most one per HeartbeatEvery per shard, carried by received frames, so a dead
//     connection sends none. A consumer may treat the stream as delivered through
//     LastFrameAt (frames of one connection arrive in order);
//   - Overflow: events of this shard were dropped because the consumer queue was full,
//     from Since (the first drop) to At by receive time. DroppedFrom and DroppedTo
//     bound the dropped trades by exchange time, and DroppedLifecycle says whether a
//     lifecycle event was among them (a consumer can then no longer trust its
//     connection state until the next Subscribed).
//
// The queue is bounded and the reader never blocks on it. A dropped event is counted
// and the shard owes an Overflow event, delivered before its next delivered event, so
// a consumer always learns that a gap happened before the data that follows it.
package streamrt

import (
	"context"
	"crypto/rand"
	"errors"
	"fmt"
	"log/slog"
	mathrand "math/rand/v2"
	"net/http"
	"sync"
	"sync/atomic"
	"time"

	"github.com/gorilla/websocket"

	"github.com/mavlevich/schurfer/collector/internal/wsstream"
)

// Trade is one public trade in canonical form. Size is in base units (the codec
// converts contracts); Notional is in the quote currency (USDT).
type Trade struct {
	Exchange   string
	Symbol     string // the venue's native instrument id, verbatim
	TradeID    string
	Side       string // "buy" or "sell": the taker side
	Price      float64
	Size       float64
	Notional   float64
	EventAt    time.Time // exchange time
	ReceivedAt time.Time // local receive time
	Seq        int64     // venue sequence when the venue has one, else 0
}

// LifecycleKind names an in-band lifecycle event.
type LifecycleKind string

const (
	Connected    LifecycleKind = "connected"
	Subscribed   LifecycleKind = "subscribed"
	Heartbeat    LifecycleKind = "heartbeat"
	Disconnected LifecycleKind = "disconnected"
	Overflow     LifecycleKind = "overflow"
)

// Lifecycle is an in-band connection event for exactly the shard's own symbols.
type Lifecycle struct {
	Kind             LifecycleKind
	Shard            int
	SessionID        string
	Symbols          []string
	At               time.Time
	Dropped          int64     // Overflow: events dropped since the last delivered event
	Since            time.Time // Overflow: when the first of them was dropped (receive time)
	DroppedFrom      time.Time // Overflow: earliest exchange time among dropped trades
	DroppedTo        time.Time // Overflow: latest exchange time among dropped trades
	DroppedLifecycle bool      // Overflow: a lifecycle event was dropped too
	LastFrameAt      time.Time // Disconnected: the session's last received frame (or its dial)
	Reason           string    // Disconnected: why
}

// Event is either a trade or a lifecycle event; exactly one pointer is set.
type Event struct {
	Trade     *Trade
	Lifecycle *Lifecycle
}

// Codec is everything venue-specific. It never dials.
type Codec interface {
	Exchange() string
	URL() string
	// MaxSymbolsPerConnection bounds a shard.
	MaxSymbolsPerConnection() int
	// SubscribeFrames are written in order after a dial.
	SubscribeFrames(symbols []string) ([][]byte, error)
	PingFrame() []byte
	PingInterval() time.Duration
	// Parse turns one frame into trades and counts subscription acknowledgements.
	// Other control frames (pong) give neither. An error ends the session (for example
	// a refused subscription).
	Parse(frame []byte, receivedAt time.Time) (Parsed, error)
}

// Parsed is one frame's content.
type Parsed struct {
	Trades []Trade
	Acks   int
}

// Config holds the runtime's own limits.
type Config struct {
	QueueSize        int
	ReadTimeout      time.Duration // no frame for this long ends the session
	SubscribeTimeout time.Duration // all acknowledgements must arrive within this
	HeartbeatEvery   time.Duration // at most one Heartbeat per shard per this
	BackoffInitial   time.Duration
	BackoffMax       time.Duration
	Dialer           *websocket.Dialer
}

func (c Config) withDefaults(codec Codec) Config {
	if c.QueueSize <= 0 {
		c.QueueSize = 65_536
	}
	if c.ReadTimeout <= 0 {
		c.ReadTimeout = 3 * codec.PingInterval()
	}
	if c.SubscribeTimeout <= 0 {
		c.SubscribeTimeout = 10 * time.Second
	}
	if c.HeartbeatEvery <= 0 {
		c.HeartbeatEvery = 200 * time.Millisecond
	}
	if c.BackoffInitial <= 0 {
		c.BackoffInitial = time.Second
	}
	if c.BackoffMax <= 0 {
		c.BackoffMax = 30 * time.Second
	}
	if c.Dialer == nil {
		c.Dialer = &websocket.Dialer{HandshakeTimeout: 10 * time.Second}
	}
	return c
}

// Stats are counters only.
type Stats struct {
	Frames      atomic.Int64
	Bytes       atomic.Int64
	Trades      atomic.Int64
	Acks        atomic.Int64
	Dropped     atomic.Int64
	Sessions    atomic.Int64
	Disconnects atomic.Int64
	ParseErrors atomic.Int64
}

// Runtime runs one codec over a fixed symbol list.
type Runtime struct {
	codec  Codec
	config Config
	events chan Event
	Stats  Stats
}

// New makes a runtime; Events() is its single ordered output stream.
func New(codec Codec, config Config) *Runtime {
	config = config.withDefaults(codec)
	return &Runtime{codec: codec, config: config, events: make(chan Event, config.QueueSize)}
}

// Events is the bounded output queue. It is closed when Run returns.
func (r *Runtime) Events() <-chan Event { return r.events }

// Run shards the symbols and keeps every shard connected until ctx ends.
func (r *Runtime) Run(ctx context.Context, symbols []string) error {
	if len(symbols) == 0 {
		return errors.New("streamrt: no symbols")
	}
	defer close(r.events)
	var wg sync.WaitGroup
	for index, shard := range wsstream.ChunkSlice(symbols, r.codec.MaxSymbolsPerConnection()) {
		wg.Add(1)
		go func() {
			defer wg.Done()
			r.runShard(ctx, index, shard)
		}()
	}
	wg.Wait()
	return nil
}

// shard is one connection's state; owed counts events dropped and not yet reported,
// owedSince is when the first of them was dropped.
type shard struct {
	index         int
	symbols       []string
	owed          int64
	owedSince     time.Time
	owedFrom      time.Time
	owedTo        time.Time
	owedLifecycle bool
}

func (s *shard) drop(r *Runtime, event Event) {
	if s.owed == 0 {
		s.owedSince = time.Now()
		s.owedFrom, s.owedTo, s.owedLifecycle = time.Time{}, time.Time{}, false
	}
	switch {
	case event.Trade != nil:
		at := event.Trade.EventAt
		if s.owedFrom.IsZero() || at.Before(s.owedFrom) {
			s.owedFrom = at
		}
		if at.After(s.owedTo) {
			s.owedTo = at
		}
	case event.Lifecycle != nil:
		s.owedLifecycle = true
	}
	s.owed++
	r.Stats.Dropped.Add(1)
}

// emit delivers without blocking. A drop is counted and reported by an Overflow event
// placed before the shard's next delivered event.
func (r *Runtime) emit(s *shard, event Event) {
	if s.owed > 0 {
		overflow := Event{Lifecycle: &Lifecycle{
			Kind: Overflow, Shard: s.index, Symbols: s.symbols, At: time.Now(),
			Dropped: s.owed, Since: s.owedSince, DroppedFrom: s.owedFrom, DroppedTo: s.owedTo,
			DroppedLifecycle: s.owedLifecycle,
		}}
		select {
		case r.events <- overflow:
			s.owed = 0
		default:
			s.drop(r, event)
			return
		}
	}
	select {
	case r.events <- event:
	default:
		s.drop(r, event)
	}
}

func (r *Runtime) runShard(ctx context.Context, index int, symbols []string) {
	s := &shard{index: index, symbols: symbols}
	backoff := r.config.BackoffInitial
	for ctx.Err() == nil {
		connected, err := r.session(ctx, s)
		if ctx.Err() != nil {
			return
		}
		if connected {
			backoff = r.config.BackoffInitial
		}
		slog.Warn("streamrt.session_ended", "exchange", r.codec.Exchange(), "shard", index, "err", err)
		jitter := time.Duration(mathrand.Int64N(int64(backoff)/2 + 1)) //nolint:gosec // backoff jitter
		select {
		case <-ctx.Done():
			return
		case <-time.After(backoff + jitter):
		}
		backoff = min(backoff*2, r.config.BackoffMax)
	}
}

// session runs one dial. It reports whether it got as far as Connected.
func (r *Runtime) session(ctx context.Context, s *shard) (connected bool, err error) {
	sessionID, err := wsstream.NewSessionID(rand.Reader)
	if err != nil {
		return false, err
	}
	conn, resp, err := r.config.Dialer.DialContext(ctx, r.codec.URL(), http.Header{})
	if resp != nil && resp.Body != nil {
		_ = resp.Body.Close()
	}
	if err != nil {
		return false, fmt.Errorf("dial: %w", err)
	}
	defer func() { _ = conn.Close() }()
	frames, err := r.codec.SubscribeFrames(s.symbols)
	if err != nil {
		return false, fmt.Errorf("subscribe frames: %w", err)
	}
	var writeMu sync.Mutex
	write := func(frame []byte) error {
		writeMu.Lock()
		defer writeMu.Unlock()
		_ = conn.SetWriteDeadline(time.Now().Add(5 * time.Second))
		return conn.WriteMessage(websocket.TextMessage, frame)
	}
	for _, frame := range frames {
		if err := write(frame); err != nil {
			return false, fmt.Errorf("subscribe: %w", err)
		}
	}
	r.Stats.Sessions.Add(1)
	dialedAt := time.Now()
	lastFrameAt := dialedAt
	r.emit(s, Event{Lifecycle: &Lifecycle{
		Kind: Connected, Shard: s.index, SessionID: sessionID, Symbols: s.symbols, At: dialedAt,
	}})
	defer func() {
		if ctx.Err() != nil {
			return // a clean shutdown is not a feed interruption
		}
		r.Stats.Disconnects.Add(1)
		reason := "closed"
		if err != nil {
			reason = err.Error()
		}
		r.emit(s, Event{Lifecycle: &Lifecycle{
			Kind: Disconnected, Shard: s.index, SessionID: sessionID, Symbols: s.symbols,
			At: time.Now(), LastFrameAt: lastFrameAt, Reason: reason,
		}})
	}()

	done := make(chan struct{})
	defer close(done)
	go func() {
		ticker := time.NewTicker(r.codec.PingInterval())
		defer ticker.Stop()
		for {
			select {
			case <-done:
				return
			case <-ctx.Done():
				_ = conn.Close()
				return
			case <-ticker.C:
				if write(r.codec.PingFrame()) != nil {
					_ = conn.Close()
					return
				}
			}
		}
	}()
	return true, r.readLoop(conn, s, sessionID, len(frames), dialedAt, &lastFrameAt)

}

// readLoop reads frames until the session ends: it enforces the subscription deadline
// and liveness, counts acknowledgements into Subscribed, forwards trades, and emits
// heartbeats. lastFrameAt is updated for the caller's Disconnected event.
func (r *Runtime) readLoop(
	conn *websocket.Conn, s *shard, sessionID string, requested int, dialedAt time.Time,
	lastFrameAt *time.Time,
) error {
	if err := wsstream.ConfigureReadLiveness(conn, r.config.ReadTimeout); err != nil {
		return err
	}
	acked, subscribed := 0, false
	var lastBeat time.Time
	subscribeBy := dialedAt.Add(r.config.SubscribeTimeout)
	for {
		if !subscribed {
			// the earlier of liveness and the subscription deadline
			deadline := time.Now().Add(r.config.ReadTimeout)
			if subscribeBy.Before(deadline) {
				deadline = subscribeBy
			}
			if subscribeBy.Before(time.Now()) {
				return fmt.Errorf("subscribe timeout: %d of %d acknowledged", acked, requested)
			}
			if err := conn.SetReadDeadline(deadline); err != nil {
				return err
			}
		}
		_, frame, readErr := conn.ReadMessage()
		if readErr != nil {
			if !subscribed && !time.Now().Before(subscribeBy) {
				return fmt.Errorf("subscribe timeout: %d of %d acknowledged", acked, requested)
			}
			return wsstream.ClassifyReadError(readErr)
		}
		received := time.Now()
		*lastFrameAt = received
		if err := wsstream.RefreshReadDeadline(conn, r.config.ReadTimeout); err != nil {
			return err
		}
		r.Stats.Frames.Add(1)
		r.Stats.Bytes.Add(int64(len(frame)))
		parsed, parseErr := r.codec.Parse(frame, received)
		if parseErr != nil {
			r.Stats.ParseErrors.Add(1)
			return fmt.Errorf("parse: %w", parseErr)
		}
		if parsed.Acks > 0 {
			r.Stats.Acks.Add(int64(parsed.Acks))
			acked += parsed.Acks
			if !subscribed && acked >= requested {
				subscribed = true
				r.emit(s, Event{Lifecycle: &Lifecycle{
					Kind: Subscribed, Shard: s.index, SessionID: sessionID, Symbols: s.symbols,
					At: received,
				}})
			}
		}
		for i := range parsed.Trades {
			r.Stats.Trades.Add(1)
			r.emit(s, Event{Trade: &parsed.Trades[i]})
		}
		if subscribed && received.Sub(lastBeat) >= r.config.HeartbeatEvery {
			lastBeat = received
			r.emit(s, Event{Lifecycle: &Lifecycle{
				Kind: Heartbeat, Shard: s.index, SessionID: sessionID, Symbols: s.symbols,
				At: received, LastFrameAt: received,
			}})
		}
	}
}
