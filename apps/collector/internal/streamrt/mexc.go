package streamrt

import (
	"encoding/json"
	"errors"
	"fmt"
	"time"
)

// MexcCodec is MEXC's contract deal stream (docs/research/mexc-sealed-probe-v1.md).
// Deals are subscribed with compress=false: one trade per push, never an aggregate.
// Volume arrives in contracts; base size = contracts x contractSize from the contract
// snapshot the codec is built with. A symbol without a contract size is refused when
// the subscribe frames are built, never guessed.
type MexcCodec struct {
	Endpoint      string             // empty means the production contract endpoint
	ContractSizes map[string]float64 // native id -> contract size, from the versioned snapshot
}

const (
	mexcContractURL     = "wss://contract.mexc.com/edge"
	mexcSymbolsPerShard = 50
	mexcPingInterval    = 15 * time.Second
	mexcTakerBuy        = 1
	mexcTakerSell       = 2
)

func (MexcCodec) Exchange() string { return "mexc" }

func (c MexcCodec) URL() string {
	if c.Endpoint != "" {
		return c.Endpoint
	}
	return mexcContractURL
}

func (MexcCodec) MaxSymbolsPerConnection() int { return mexcSymbolsPerShard }

func (c MexcCodec) SubscribeFrames(symbols []string) ([][]byte, error) {
	frames := make([][]byte, 0, len(symbols))
	for _, symbol := range symbols {
		if size, ok := c.ContractSizes[symbol]; !ok || size <= 0 {
			return nil, fmt.Errorf("no contract size for %s", symbol)
		}
		frame, err := json.Marshal(map[string]any{
			"method": "sub.deal",
			"param":  map[string]any{"symbol": symbol, "compress": false},
		})
		if err != nil {
			return nil, err
		}
		frames = append(frames, frame)
	}
	return frames, nil
}

func (MexcCodec) PingFrame() []byte { return []byte(`{"method":"ping"}`) }

func (MexcCodec) PingInterval() time.Duration { return mexcPingInterval }

type mexcDeal struct {
	Price   *float64 `json:"p"`
	Volume  *float64 `json:"v"`
	Taker   *int     `json:"T"`
	Time    *int64   `json:"t"`
	TradeID *int64   `json:"i"`
}

// Parse returns the trades of a push.deal frame, or one acknowledgement for an
// rs.sub.deal success (one per subscription). rs.error or a failed subscription ends
// the session; pongs give nothing. The payload may be one deal or a list of deals.
// Deals without an id are skipped: they could not be deduplicated.
func (c MexcCodec) Parse(frame []byte, receivedAt time.Time) (Parsed, error) {
	var message struct {
		Channel string          `json:"channel"`
		Symbol  string          `json:"symbol"`
		Data    json.RawMessage `json:"data"`
	}
	if err := json.Unmarshal(frame, &message); err != nil {
		return Parsed{}, fmt.Errorf("decode: %w", err)
	}
	switch message.Channel {
	case "rs.error":
		return Parsed{}, errors.New("mexc rs.error")
	case "rs.sub.deal":
		if string(message.Data) != `"success"` {
			return Parsed{}, fmt.Errorf("mexc subscription refused: %s", message.Data)
		}
		return Parsed{Acks: 1}, nil
	case "push.deal":
	default:
		return Parsed{}, nil
	}
	size, ok := c.ContractSizes[message.Symbol]
	if !ok || size <= 0 {
		return Parsed{}, nil // not ours: never converted with a guessed size
	}
	var deals []mexcDeal
	if len(message.Data) > 0 && message.Data[0] == '[' {
		if err := json.Unmarshal(message.Data, &deals); err != nil {
			return Parsed{}, fmt.Errorf("decode deals: %w", err)
		}
	} else {
		var one mexcDeal
		if err := json.Unmarshal(message.Data, &one); err != nil {
			return Parsed{}, fmt.Errorf("decode deal: %w", err)
		}
		deals = []mexcDeal{one}
	}
	trades := make([]Trade, 0, len(deals))
	for _, d := range deals {
		if d.Price == nil || d.Volume == nil || d.Taker == nil || d.Time == nil ||
			d.TradeID == nil || *d.Price <= 0 || *d.Volume <= 0 || *d.Time <= 0 {
			continue
		}
		var side string
		switch *d.Taker {
		case mexcTakerBuy:
			side = "buy"
		case mexcTakerSell:
			side = "sell"
		default:
			continue
		}
		eventAt := time.UnixMilli(*d.Time)
		if eventAt.After(receivedAt.Add(maxFutureSkew)) {
			continue
		}
		base := *d.Volume * size
		trades = append(trades, Trade{
			Exchange: "mexc", Symbol: message.Symbol, TradeID: fmt.Sprint(*d.TradeID), Side: side,
			Price: *d.Price, Size: base, Notional: *d.Price * base, EventAt: eventAt,
			ReceivedAt: receivedAt,
		})
	}
	return Parsed{Trades: trades}, nil
}
