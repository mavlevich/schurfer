package binance

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
	"github.com/mavlevich/schurfer/collector/internal/wsstream"
)

const (
	markPriceStreamsPerConnection = 200
	maxMarkPriceFutureSkew        = 5 * time.Second
)

// PublicMarkPrice is one normalized USD-M futures mark-price update.
// FundingRate is the venue's current/predicted funding rate, not a settled
// payment. NextFundingAt is retained so research can align the state to the
// relevant settlement boundary without guessing a universal 8h cadence.
type PublicMarkPrice struct {
	Symbol        string
	EventAt       time.Time
	ReceivedAt    time.Time
	MarkPrice     float64
	IndexPrice    float64
	FundingRate   float64
	NextFundingAt time.Time
}

// InvalidMarkPrice preserves the routing and timing evidence available from
// a semantically invalid markPriceUpdate. It is deliberately delivered over
// the same bounded FIFO as valid updates: silently dropping it here would
// leave the application unable to count the failure or mark the affected bar
// incomplete before its slower per-symbol silence detector fires.
type InvalidMarkPrice struct {
	Symbol     string
	EventAt    time.Time
	ReceivedAt time.Time
	Reason     string
}

// MarkPriceMessage is exactly one valid update or one invalid observation.
// Non-mark-price websocket messages produce neither and are ignored before
// this boundary.
type MarkPriceMessage struct {
	Update  *PublicMarkPrice
	Invalid *InvalidMarkPrice
}

type MarkPriceFn func(context.Context, MarkPriceMessage) error

type MarkPriceLifecycleEvent struct {
	ShardSessionID string
	Symbols        []string
	ConnectedAt    time.Time
	DisconnectedAt time.Time
	Reason         string
	ReadTimeout    bool
}

type MarkPriceLifecycleFn func(MarkPriceLifecycleEvent)

func (s *Source) RunMarkPrice(ctx context.Context, symbols []string, consume MarkPriceFn) error {
	return s.RunMarkPriceWithLifecycle(ctx, symbols, consume, func(MarkPriceLifecycleEvent) {})
}

// RunMarkPriceWithLifecycle uses Binance's market-category combined stream
// (the same routed endpoint as aggTrade) at the documented default
// three-second cadence. One-second updates would add no information to a
// one-minute persisted row while tripling decode/queue load.
// Connections are sharded and bounded exactly like the existing
// trade/bookTicker feeds; one venue outage cannot create an unbounded
// goroutine or queue fan-out.
func (s *Source) RunMarkPriceWithLifecycle(
	ctx context.Context,
	symbols []string,
	consume MarkPriceFn,
	onLifecycle MarkPriceLifecycleFn,
) error {
	slog.Info("binance.markprice.subscribing", "count", len(symbols))
	chunks := wsstream.ChunkSlice(symbols, markPriceStreamsPerConnection)
	var wg sync.WaitGroup
	for _, chunk := range chunks {
		wg.Add(1)
		go func(connectionSymbols []string) {
			defer wg.Done()
			s.markPriceStreamLoop(ctx, connectionSymbols, consume, onLifecycle)
		}(chunk)
	}
	wg.Wait()
	return nil
}

func (s *Source) markPriceStreamLoop(
	ctx context.Context,
	symbols []string,
	consume MarkPriceFn,
	onLifecycle MarkPriceLifecycleFn,
) {
	for {
		if err := s.markPriceStream(ctx, symbols, consume, onLifecycle); err != nil {
			if ctx.Err() != nil {
				return
			}
			slog.Warn("binance.markprice.reconnecting", "err", err, "read_timeout", wsstream.IsReadTimeout(err), "delay", reconnectDelay)
		}
		select {
		case <-ctx.Done():
			return
		case <-time.After(reconnectDelay):
		}
	}
}

func (s *Source) markPriceStream(
	ctx context.Context,
	symbols []string,
	consume MarkPriceFn,
	onLifecycle MarkPriceLifecycleFn,
) (err error) {
	sessionID, sessionErr := wsstream.NewSessionID(rand.Reader)
	if sessionErr != nil {
		return fmt.Errorf("mark price stream session id: %w", sessionErr)
	}
	connectedAt := time.Now()
	var connected bool
	defer func() {
		if err != nil && connected {
			onLifecycle(MarkPriceLifecycleEvent{
				ShardSessionID: sessionID, Symbols: symbols, ConnectedAt: connectedAt,
				DisconnectedAt: time.Now(), Reason: err.Error(), ReadTimeout: wsstream.IsReadTimeout(err),
			})
		}
	}()

	dialer := websocket.Dialer{HandshakeTimeout: 10 * time.Second}
	conn, response, dialErr := dialer.DialContext(ctx, s.markPriceStreamURL(symbols), http.Header{})
	if dialErr != nil {
		if response != nil {
			_ = response.Body.Close()
		}
		return fmt.Errorf("dial: %w", dialErr)
	}
	defer func() { _ = conn.Close() }()
	connected = true
	onLifecycle(MarkPriceLifecycleEvent{ShardSessionID: sessionID, Symbols: symbols, ConnectedAt: connectedAt})

	stop := context.AfterFunc(ctx, func() { _ = conn.SetReadDeadline(time.Now()) })
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
		if err := handleMarkPricePayload(ctx, payload, time.Now(), consume); err != nil {
			return err
		}
	}
}

func (s *Source) markPriceStreamURL(symbols []string) string {
	base := s.wsMarketBaseURL
	if base == "" {
		base = wsMarketBaseURL
	}
	streams := make([]string, len(symbols))
	for i, symbol := range symbols {
		streams[i] = strings.ToLower(symbol) + "@markPrice"
	}
	return base + "?streams=" + strings.Join(streams, "/")
}

func handleMarkPricePayload(
	ctx context.Context,
	payload []byte,
	receivedAt time.Time,
	consume MarkPriceFn,
) error {
	var envelope struct {
		Data struct {
			EventType  string `json:"e"`
			EventAt    int64  `json:"E"`
			Symbol     string `json:"s"`
			MarkPrice  string `json:"p"`
			IndexPrice string `json:"i"`
			// Keep the exact upper-case field so encoding/json's
			// case-insensitive fallback cannot decode "P" (estimated
			// settlement price) into the lower-case "p" mark price.
			EstimatedSettlePrice string `json:"P"`
			FundingRate          string `json:"r"`
			NextFundingAt        int64  `json:"T"`
		} `json:"data"`
	}
	if err := json.Unmarshal(payload, &envelope); err != nil {
		return fmt.Errorf("decode mark price: %w", err)
	}
	d := envelope.Data
	if d.EventType != "markPriceUpdate" {
		return nil
	}
	mark, markErr := strconv.ParseFloat(d.MarkPrice, 64)
	index, indexErr := strconv.ParseFloat(d.IndexPrice, 64)
	funding, fundingErr := strconv.ParseFloat(d.FundingRate, 64)
	eventAt := time.UnixMilli(d.EventAt)
	eventAtUsable := d.EventAt > 0 && !eventAt.After(receivedAt.Add(maxMarkPriceFutureSkew))
	if markErr != nil || indexErr != nil || fundingErr != nil ||
		!finitePositive(mark) || !finitePositive(index) ||
		math.IsNaN(funding) || math.IsInf(funding, 0) ||
		!eventAtUsable ||
		d.NextFundingAt <= 0 || strings.TrimSpace(d.Symbol) == "" {
		invalid := InvalidMarkPrice{
			Symbol:     wsstream.NormalizeSymbol(d.Symbol),
			ReceivedAt: receivedAt,
			Reason:     "invalid_market_values_or_provenance",
		}
		if eventAtUsable {
			invalid.EventAt = eventAt
		}
		return consume(ctx, MarkPriceMessage{Invalid: &invalid})
	}
	update := PublicMarkPrice{
		Symbol: wsstream.NormalizeSymbol(d.Symbol), EventAt: eventAt,
		ReceivedAt: receivedAt, MarkPrice: mark, IndexPrice: index,
		FundingRate: funding, NextFundingAt: time.UnixMilli(d.NextFundingAt),
	}
	return consume(ctx, MarkPriceMessage{Update: &update})
}

func finitePositive(value float64) bool {
	return !math.IsNaN(value) && !math.IsInf(value, 0) && value > 0
}
