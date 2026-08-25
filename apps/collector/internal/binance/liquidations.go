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
	"time"

	"github.com/gorilla/websocket"
	"github.com/mavlevich/schurfer/collector/internal/liquidationcapture"
	"github.com/mavlevich/schurfer/collector/internal/wsstream"
)

func (s *Source) CoverageKind() liquidationcapture.CoverageKind {
	return liquidationcapture.CoverageLatestPerSymbol1000ms
}

func (s *Source) ExpectedConnections(symbolCount int) int {
	if symbolCount <= 0 {
		return 0
	}
	return 1
}

func (s *Source) Stats() liquidationcapture.SourceStats {
	last := s.lastLiquidationAtUnixMilli.Load()
	stats := liquidationcapture.SourceStats{
		EventsAcceptedTotal:          s.liquidationAcceptedTotal.Load(),
		EventsInvalidTotal:           s.liquidationInvalidTotal.Load(),
		EventsOutOfScopeTotal:        s.liquidationOutOfScopeTotal.Load(),
		ScopeTagMissingAcceptedTotal: s.liquidationScopeTagMissingAcceptedTotal.Load(),
		ReconnectTotal:               s.liquidationReconnectTotal.Load(),
		ReadTimeoutTotal:             s.liquidationReadTimeoutTotal.Load(),
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
		return fmt.Errorf("binance liquidation capture requires at least one symbol")
	}
	allowed := make(map[string]struct{}, len(symbols))
	for _, symbol := range symbols {
		allowed[wsstream.NormalizeSymbol(symbol)] = struct{}{}
	}
	for ctx.Err() == nil {
		err := s.liquidationStream(ctx, allowed, universeVersion, consume, onLifecycle)
		if ctx.Err() != nil {
			return nil
		}
		if err != nil {
			s.liquidationReconnectTotal.Add(1)
			if wsstream.IsReadTimeout(err) {
				s.liquidationReadTimeoutTotal.Add(1)
			}
			slog.Warn("binance.liquidations.reconnecting", "err", err)
		}
		select {
		case <-ctx.Done():
			return nil
		case <-time.After(reconnectDelay):
		}
	}
	return nil
}

func (s *Source) liquidationStream(
	ctx context.Context,
	allowed map[string]struct{},
	universeVersion string,
	consume liquidationcapture.EventFn,
	onLifecycle liquidationcapture.LifecycleFn,
) (err error) {
	sessionID, err := wsstream.NewSessionID(rand.Reader)
	if err != nil {
		return fmt.Errorf("liquidation session id: %w", err)
	}
	connectedAt := time.Now()
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

	base := s.wsMarketBaseURL
	if base == "" {
		base = wsMarketBaseURL
	}
	streamURL := base + "?streams=!forceOrder@arr"
	dialer := websocket.Dialer{HandshakeTimeout: 10 * time.Second}
	conn, response, dialErr := dialer.DialContext(ctx, streamURL, http.Header{})
	if dialErr != nil {
		if response != nil {
			_ = response.Body.Close()
		}
		return fmt.Errorf("dial: %w", dialErr)
	}
	defer func() { _ = conn.Close() }()
	connected = true
	onLifecycle(liquidationcapture.LifecycleEvent{SessionID: sessionID, ConnectedAt: connectedAt})
	slog.Info("binance.liquidations.connected", "session_id", sessionID)

	pingCtx, pingCancel := context.WithCancel(ctx)
	pingDone := make(chan struct{})
	go func() {
		defer close(pingDone)
		ticker := time.NewTicker(pingInterval)
		defer ticker.Stop()
		for {
			select {
			case <-ticker.C:
				if conn.WriteControl(websocket.PingMessage, nil, time.Now().Add(5*time.Second)) != nil {
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
		if err := s.handleLiquidationPayload(ctx, payload, time.Now(), sessionID, universeVersion, allowed, consume); err != nil {
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
	allowed map[string]struct{},
	consume liquidationcapture.EventFn,
) error {
	var envelope struct {
		Stream string `json:"stream"`
		Data   struct {
			EventType  string `json:"e"`
			EventAt    int64  `json:"E"`
			Pair       string `json:"ps"`
			SymbolType int    `json:"st"`
			Order      struct {
				Symbol                    string `json:"s"`
				Side                      string `json:"S"`
				OrderType                 string `json:"o"`
				TimeInForce               string `json:"f"`
				Quantity                  string `json:"q"`
				Price                     string `json:"p"`
				AveragePrice              string `json:"ap"`
				Status                    string `json:"X"`
				LastFilledQuantity        string `json:"l"`
				AccumulatedFilledQuantity string `json:"z"`
				TradeAt                   int64  `json:"T"`
			} `json:"o"`
		} `json:"data"`
	}
	if err := json.Unmarshal(payload, &envelope); err != nil {
		return fmt.Errorf("decode forceOrder: %w", err)
	}
	data := envelope.Data
	if data.EventType != "forceOrder" {
		return nil
	}
	// Binance's post-CM documentation includes st=1 (USD-M) and st=2
	// (COIN-M), but the production fstream endpoint still emits legacy
	// frames with no st field. A missing tag is accepted only after the
	// strict frozen USD-M symbol allowlist check below and is labelled as a
	// different source contract so research can exclude or stratify it.
	if data.SymbolType == 2 {
		s.liquidationOutOfScopeTotal.Add(1)
		return nil
	}
	if data.SymbolType != 0 && data.SymbolType != 1 {
		s.liquidationInvalidTotal.Add(1)
		return nil
	}
	symbol := wsstream.NormalizeSymbol(data.Order.Symbol)
	if _, ok := allowed[symbol]; !ok {
		s.liquidationOutOfScopeTotal.Add(1)
		return nil
	}
	sourceContractVariant := "binance_merged_um_v1"
	if data.SymbolType == 0 {
		sourceContractVariant = "binance_usdm_no_scope_tag_v1"
	}
	quantity, quantityErr := strconv.ParseFloat(data.Order.Quantity, 64)
	orderPrice, orderPriceErr := strconv.ParseFloat(data.Order.Price, 64)
	averagePrice, averagePriceErr := strconv.ParseFloat(data.Order.AveragePrice, 64)
	lastFilled, lastFilledErr := strconv.ParseFloat(data.Order.LastFilledQuantity, 64)
	accumulatedFilled, accumulatedErr := strconv.ParseFloat(data.Order.AccumulatedFilledQuantity, 64)
	positionSide := liquidationcapture.PositionSide("")
	switch strings.ToUpper(strings.TrimSpace(data.Order.Side)) {
	case "SELL":
		positionSide = liquidationcapture.PositionLong
	case "BUY":
		positionSide = liquidationcapture.PositionShort
	}
	raw, rawErr := json.Marshal(data)
	notional := averagePrice * accumulatedFilled
	event, eventErr := liquidationcapture.NewEvent(liquidationcapture.Event{
		Exchange: "binance", MarketType: "linear", NativeMarketID: symbol, UniverseVersion: universeVersion,
		SourceContractVariant: sourceContractVariant,
		CoverageKind:          liquidationcapture.CoverageLatestPerSymbol1000ms,
		PositionSide:          positionSide, EventAt: time.UnixMilli(data.Order.TradeAt),
		ExchangePublishedAt: time.UnixMilli(data.EventAt), ReceivedAt: receivedAt,
		SourceSessionID: sessionID, Quantity: quantity, QuantityUnit: "base_asset",
		OrderPrice: &orderPrice, AveragePrice: &averagePrice,
		LastFilledQuantity: &lastFilled, AccumulatedFilledQuantity: &accumulatedFilled,
		EstimatedLiquidationNotional: &notional, RawPayload: raw,
	}, fmt.Sprintf("%d:%d:%s:%d:%s:%s:%s:%s:%s:%s:%s:%s:%s:%s", data.EventAt, data.Order.TradeAt,
		data.Pair, data.SymbolType,
		symbol, data.Order.Side, data.Order.OrderType, data.Order.TimeInForce,
		data.Order.Quantity, data.Order.Price, data.Order.AveragePrice, data.Order.Status,
		data.Order.LastFilledQuantity, data.Order.AccumulatedFilledQuantity))
	if quantityErr != nil || orderPriceErr != nil || averagePriceErr != nil ||
		lastFilledErr != nil || accumulatedErr != nil || rawErr != nil || eventErr != nil {
		s.liquidationInvalidTotal.Add(1)
		return nil
	}
	if err := consume(ctx, event); err != nil {
		return err
	}
	if data.SymbolType == 0 {
		s.liquidationScopeTagMissingAcceptedTotal.Add(1)
	}
	s.liquidationAcceptedTotal.Add(1)
	s.lastLiquidationAtUnixMilli.Store(event.EventAt.UnixMilli())
	return nil
}

var _ liquidationcapture.Source = (*Source)(nil)
