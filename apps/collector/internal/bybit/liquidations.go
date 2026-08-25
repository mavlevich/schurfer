package bybit

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
	"github.com/mavlevich/schurfer/collector/internal/liquidationcapture"
	"github.com/mavlevich/schurfer/collector/internal/wsstream"
)

const liquidationTopicsPerConnection = 200

type liquidationSubscriptionAcks struct {
	pending map[string]struct{}
}

func newLiquidationSubscriptionAcks() *liquidationSubscriptionAcks {
	return &liquidationSubscriptionAcks{pending: make(map[string]struct{})}
}

func (acks *liquidationSubscriptionAcks) expect(requestID string) {
	acks.pending[requestID] = struct{}{}
}

func (acks *liquidationSubscriptionAcks) observe(payload []byte) (handled bool, ready bool, err error) {
	var response struct {
		Op      string `json:"op"`
		Request string `json:"req_id"`
		Success *bool  `json:"success"`
	}
	if err := json.Unmarshal(payload, &response); err != nil {
		return false, false, fmt.Errorf("decode subscription response: %w", err)
	}
	if response.Op != "subscribe" {
		return false, false, nil
	}
	if response.Success == nil || !*response.Success {
		return true, false, fmt.Errorf("subscribe nack: %s", string(payload))
	}
	if _, ok := acks.pending[response.Request]; !ok {
		return true, false, fmt.Errorf("unexpected subscription acknowledgement %q", response.Request)
	}
	delete(acks.pending, response.Request)
	return true, len(acks.pending) == 0, nil
}

func (s *Source) CoverageKind() liquidationcapture.CoverageKind {
	return liquidationcapture.CoverageCompleteStream
}

func (s *Source) ExpectedConnections(symbolCount int) int {
	if symbolCount <= 0 {
		return 0
	}
	return (symbolCount + liquidationTopicsPerConnection - 1) / liquidationTopicsPerConnection
}

func (s *Source) Stats() liquidationcapture.SourceStats {
	last := s.lastLiquidationAtUnixMilli.Load()
	stats := liquidationcapture.SourceStats{
		EventsAcceptedTotal: s.liquidationAcceptedTotal.Load(),
		EventsInvalidTotal:  s.liquidationInvalidTotal.Load(),
		ReconnectTotal:      s.liquidationReconnectTotal.Load(),
		ReadTimeoutTotal:    s.liquidationReadTimeoutTotal.Load(),
	}
	if last > 0 {
		stats.LastEventAt = time.UnixMilli(last)
	}
	return stats
}

func (s *Source) RunLiquidations(
	ctx context.Context,
	symbols []string,
	universeVersion string,
	consume liquidationcapture.EventFn,
	onLifecycle liquidationcapture.LifecycleFn,
) error {
	if len(symbols) == 0 {
		return fmt.Errorf("bybit liquidation capture requires at least one symbol")
	}
	chunks := wsstream.ChunkSlice(symbols, liquidationTopicsPerConnection)
	var wg sync.WaitGroup
	for _, chunk := range chunks {
		connectionSymbols := append([]string(nil), chunk...)
		wg.Add(1)
		go func() {
			defer wg.Done()
			s.liquidationLoop(ctx, connectionSymbols, universeVersion, consume, onLifecycle)
		}()
	}
	wg.Wait()
	return nil
}

func (s *Source) liquidationLoop(
	ctx context.Context,
	symbols []string,
	universeVersion string,
	consume liquidationcapture.EventFn,
	onLifecycle liquidationcapture.LifecycleFn,
) {
	for ctx.Err() == nil {
		err := s.liquidationStream(ctx, symbols, universeVersion, consume, onLifecycle)
		if ctx.Err() != nil {
			return
		}
		if err != nil {
			s.liquidationReconnectTotal.Add(1)
			if wsstream.IsReadTimeout(err) {
				s.liquidationReadTimeoutTotal.Add(1)
			}
			slog.Warn("bybit.liquidations.reconnecting", "err", err, "symbols", len(symbols))
		}
		select {
		case <-ctx.Done():
			return
		case <-time.After(s.streamConfig.ReconnectDelay):
		}
	}
}

func (s *Source) liquidationStream(
	ctx context.Context,
	symbols []string,
	universeVersion string,
	consume liquidationcapture.EventFn,
	onLifecycle liquidationcapture.LifecycleFn,
) (err error) {
	sessionID, err := wsstream.NewSessionID(rand.Reader)
	if err != nil {
		return fmt.Errorf("liquidation session id: %w", err)
	}
	var connectedAt time.Time
	connected := false
	defer func() {
		if err != nil && connected {
			onLifecycle(liquidationcapture.LifecycleEvent{
				SessionID: sessionID, ConnectedAt: connectedAt,
				DisconnectedAt: time.Now(), Reason: err.Error(),
				ReadTimeout: wsstream.IsReadTimeout(err),
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
	for i, symbol := range symbols {
		topics[i] = "allLiquidation." + symbol
	}
	subscriptionAcks := newLiquidationSubscriptionAcks()
	for i := 0; i < len(topics); i += subChunk {
		end := min(i+subChunk, len(topics))
		requestID := fmt.Sprintf("%s-%d", sessionID, i/subChunk)
		subscriptionAcks.expect(requestID)
		if err := writeJSON(map[string]any{
			"req_id": requestID,
			"op":     "subscribe",
			"args":   topics[i:end],
		}); err != nil {
			return fmt.Errorf("subscribe: %w", err)
		}
	}

	pingCtx, pingCancel := context.WithCancel(ctx)
	pingDone := make(chan struct{})
	go func() {
		defer close(pingDone)
		ticker := time.NewTicker(s.streamConfig.PingInterval)
		defer ticker.Stop()
		for {
			select {
			case <-ticker.C:
				if writeJSON(map[string]string{"op": "ping"}) != nil {
					return
				}
			case <-pingCtx.Done():
				return
			}
		}
	}()
	defer func() { <-pingDone }()
	defer pingCancel()

	stop := context.AfterFunc(ctx, func() { _ = conn.SetReadDeadline(time.Now()) })
	defer stop()
	if err := wsstream.ConfigureReadLiveness(conn, s.streamConfig.ReadTimeout); err != nil {
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
		if err := wsstream.RefreshReadDeadline(conn, s.streamConfig.ReadTimeout); err != nil {
			return fmt.Errorf("refresh read deadline: %w", err)
		}
		handled, ready, ackErr := subscriptionAcks.observe(payload)
		if ackErr != nil {
			return ackErr
		}
		if handled {
			if ready && !connected {
				connectedAt = time.Now()
				connected = true
				onLifecycle(liquidationcapture.LifecycleEvent{
					SessionID:   sessionID,
					ConnectedAt: connectedAt,
				})
				slog.Info("bybit.liquidations.connected", "symbols", len(symbols), "session_id", sessionID)
			}
			continue
		}
		if err := s.handleLiquidationPayload(ctx, payload, time.Now(), sessionID, universeVersion, consume); err != nil {
			return err
		}
	}
}

func (s *Source) handleLiquidationPayload(
	ctx context.Context,
	payload []byte,
	receivedAt time.Time,
	sessionID string,
	universeVersion string,
	consume liquidationcapture.EventFn,
) error {
	var message struct {
		Op      string `json:"op"`
		Success *bool  `json:"success"`
		Topic   string `json:"topic"`
		TS      int64  `json:"ts"`
		Data    []struct {
			EventAt int64  `json:"T"`
			Symbol  string `json:"s"`
			Side    string `json:"S"`
			Size    string `json:"v"`
			Price   string `json:"p"`
		} `json:"data"`
	}
	if err := json.Unmarshal(payload, &message); err != nil {
		return fmt.Errorf("decode allLiquidation: %w", err)
	}
	if message.Op == "subscribe" {
		if message.Success != nil && !*message.Success {
			return fmt.Errorf("subscribe nack: %s", string(payload))
		}
		return nil
	}
	if !strings.HasPrefix(message.Topic, "allLiquidation.") {
		return nil
	}
	for index, item := range message.Data {
		size, sizeErr := strconv.ParseFloat(item.Size, 64)
		price, priceErr := strconv.ParseFloat(item.Price, 64)
		positionSide := liquidationcapture.PositionSide("")
		switch strings.ToLower(strings.TrimSpace(item.Side)) {
		case "buy":
			positionSide = liquidationcapture.PositionLong
		case "sell":
			positionSide = liquidationcapture.PositionShort
		}
		raw, rawErr := json.Marshal(item)
		notional := size * price
		event, eventErr := liquidationcapture.NewEvent(liquidationcapture.Event{
			Exchange: "bybit", MarketType: "linear", NativeMarketID: item.Symbol, UniverseVersion: universeVersion,
			SourceContractVariant: "bybit_all_liquidation_v1",
			CoverageKind:          liquidationcapture.CoverageCompleteStream,
			PositionSide:          positionSide, EventAt: time.UnixMilli(item.EventAt),
			ExchangePublishedAt: time.UnixMilli(message.TS), ReceivedAt: receivedAt,
			SourceSessionID: sessionID, Quantity: size, QuantityUnit: "base_asset",
			BankruptcyPrice: &price, EstimatedLiquidationNotional: &notional, RawPayload: raw,
		}, fmt.Sprintf("%d:%d:%d:%s:%s:%s:%s", message.TS, index, item.EventAt, item.Symbol, item.Side, item.Size, item.Price))
		if sizeErr != nil || priceErr != nil || rawErr != nil || eventErr != nil {
			s.liquidationInvalidTotal.Add(1)
			continue
		}
		if err := consume(ctx, event); err != nil {
			return err
		}
		s.liquidationAcceptedTotal.Add(1)
		s.lastLiquidationAtUnixMilli.Store(event.EventAt.UnixMilli())
	}
	return nil
}

var _ liquidationcapture.Source = (*Source)(nil)
