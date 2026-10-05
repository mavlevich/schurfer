package streamrt

import (
	"encoding/json"
	"fmt"
	"strconv"
	"strings"
	"time"

	"github.com/mavlevich/schurfer/collector/internal/wsstream"
)

// BybitCodec is the public linear trade stream of Bybit v5. Sizes are already base
// units on linear contracts; the notional is price times size in USDT. Validation
// follows the existing bybit adapter (apps/collector/internal/bybit/trades.go): finite
// positive price and size, a known taker side, an exchange time no more than
// maxFutureSkew ahead of the receive time.
type BybitCodec struct {
	Endpoint string // empty means the production public linear endpoint
}

const (
	bybitLinearURL          = "wss://stream.bybit.com/v5/public/linear"
	bybitTopicsPerSubscribe = 10
	bybitTopicsPerShard     = 200
	bybitPingInterval       = 20 * time.Second
	maxFutureSkew           = 5 * time.Second
)

func (BybitCodec) Exchange() string { return "bybit" }

func (c BybitCodec) URL() string {
	if c.Endpoint != "" {
		return c.Endpoint
	}
	return bybitLinearURL
}

func (BybitCodec) MaxSymbolsPerConnection() int { return bybitTopicsPerShard }

func (BybitCodec) SubscribeFrames(symbols []string) ([][]byte, error) {
	topics := make([]string, len(symbols))
	for i, symbol := range symbols {
		topics[i] = "publicTrade." + symbol
	}
	chunks := wsstream.ChunkSlice(topics, bybitTopicsPerSubscribe)
	frames := make([][]byte, 0, len(chunks))
	for _, chunk := range chunks {
		frame, err := json.Marshal(map[string]any{"op": "subscribe", "args": chunk})
		if err != nil {
			return nil, err
		}
		frames = append(frames, frame)
	}
	return frames, nil
}

func (BybitCodec) PingFrame() []byte { return []byte(`{"op":"ping"}`) }

func (BybitCodec) PingInterval() time.Duration { return bybitPingInterval }

type bybitMessage struct {
	Op      string `json:"op"`
	Success *bool  `json:"success"`
	RetMsg  string `json:"ret_msg"`
	Topic   string `json:"topic"`
	Data    []struct {
		EventAt int64  `json:"T"`
		Symbol  string `json:"s"`
		Side    string `json:"S"`
		Size    string `json:"v"`
		Price   string `json:"p"`
		TradeID string `json:"i"`
		Seq     int64  `json:"seq"`
	} `json:"data"`
}

// Parse returns the valid trades of a publicTrade frame, or one acknowledgement for a
// successful subscribe response (one per subscribe frame). A refused subscription is
// an error; pongs give nothing. Invalid items, and trades without an id (which could
// not be deduplicated), are skipped, as in the existing adapter.
func (BybitCodec) Parse(frame []byte, receivedAt time.Time) (Parsed, error) {
	var message bybitMessage
	if err := json.Unmarshal(frame, &message); err != nil {
		return Parsed{}, fmt.Errorf("decode: %w", err)
	}
	if message.Op == "subscribe" {
		if message.Success == nil || !*message.Success {
			return Parsed{}, fmt.Errorf("subscribe refused: %s", message.RetMsg)
		}
		return Parsed{Acks: 1}, nil
	}
	if !strings.HasPrefix(message.Topic, "publicTrade.") {
		return Parsed{}, nil
	}
	trades := make([]Trade, 0, len(message.Data))
	for _, item := range message.Data {
		price, priceErr := strconv.ParseFloat(item.Price, 64)
		size, sizeErr := strconv.ParseFloat(item.Size, 64)
		side := strings.ToLower(strings.TrimSpace(item.Side))
		eventAt := time.UnixMilli(item.EventAt)
		symbol := wsstream.NormalizeSymbol(item.Symbol)
		if priceErr != nil || sizeErr != nil || !wsstream.FinitePositiveNumber(price) ||
			!wsstream.FinitePositiveNumber(size) || item.EventAt <= 0 ||
			eventAt.After(receivedAt.Add(maxFutureSkew)) || symbol == "" ||
			strings.TrimSpace(item.TradeID) == "" || (side != "buy" && side != "sell") {
			continue
		}
		trades = append(trades, Trade{
			Exchange: "bybit", Symbol: symbol, TradeID: strings.TrimSpace(item.TradeID),
			Side: side, Price: price, Size: size, Notional: price * size,
			EventAt: eventAt, ReceivedAt: receivedAt, Seq: item.Seq,
		})
	}
	return Parsed{Trades: trades}, nil
}
