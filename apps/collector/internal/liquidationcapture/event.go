// Package liquidationcapture defines the venue-neutral, append-only public
// liquidation event contract. Venue adapters must preserve their native
// semantics instead of pretending every exchange exposes a complete tape.
package liquidationcapture

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"sort"
	"strings"
	"time"
)

const CaptureVersion = "liquidation_event_v1"

type CoverageKind string

const (
	// CoverageCompleteStream means the venue documents the feed as carrying
	// every liquidation in scope. Connectivity/loss still determines whether
	// any particular time interval is complete.
	CoverageCompleteStream CoverageKind = "complete_stream"
	// CoverageLatestPerSymbol1000ms is Binance's documented lossy snapshot:
	// at most the latest liquidation for a symbol in each 1000 ms interval.
	CoverageLatestPerSymbol1000ms CoverageKind = "latest_per_symbol_1000ms"
)

type PositionSide string

const (
	PositionLong  PositionSide = "long"
	PositionShort PositionSide = "short"
)

// Event is one normalized public liquidation observation. RawPayload keeps
// the venue-native item for future audit; normalized numeric fields never
// replace it as the source of truth.
type Event struct {
	Exchange              string
	MarketType            string
	NativeMarketID        string
	UniverseVersion       string
	SourceContractVariant string
	CoverageKind          CoverageKind
	PositionSide          PositionSide
	EventAt               time.Time
	ExchangePublishedAt   time.Time
	ReceivedAt            time.Time
	SourceSessionID       string
	SourceEventKey        [32]byte
	PayloadHash           [32]byte

	Quantity                     float64
	QuantityUnit                 string
	BankruptcyPrice              *float64
	OrderPrice                   *float64
	AveragePrice                 *float64
	LastFilledQuantity           *float64
	AccumulatedFilledQuantity    *float64
	EstimatedLiquidationNotional *float64
	RawPayload                   json.RawMessage
}

// NewEvent validates the normalized contract and derives stable hashes. The
// sourceIdentity must be deterministic for an identical delivery of the same
// venue event. Venues without a native event ID cannot promise deduplication
// if they re-batch or otherwise rewrite an event during replay.
func NewEvent(event Event, sourceIdentity string) (Event, error) {
	event.Exchange = strings.ToLower(strings.TrimSpace(event.Exchange))
	event.MarketType = strings.ToLower(strings.TrimSpace(event.MarketType))
	event.NativeMarketID = strings.ToUpper(strings.TrimSpace(event.NativeMarketID))
	event.UniverseVersion = strings.TrimSpace(event.UniverseVersion)
	event.SourceContractVariant = strings.TrimSpace(event.SourceContractVariant)
	event.QuantityUnit = strings.TrimSpace(event.QuantityUnit)
	event.SourceSessionID = strings.TrimSpace(event.SourceSessionID)

	if event.Exchange == "" || event.MarketType == "" || event.NativeMarketID == "" ||
		event.UniverseVersion == "" || event.SourceContractVariant == "" {
		return Event{}, errors.New("exchange, market_type, native_market_id, universe_version, and source_contract_variant are required")
	}
	if event.CoverageKind != CoverageCompleteStream && event.CoverageKind != CoverageLatestPerSymbol1000ms {
		return Event{}, fmt.Errorf("unsupported coverage kind %q", event.CoverageKind)
	}
	if event.PositionSide != PositionLong && event.PositionSide != PositionShort {
		return Event{}, fmt.Errorf("unsupported position side %q", event.PositionSide)
	}
	if event.EventAt.IsZero() || event.ExchangePublishedAt.IsZero() || event.ReceivedAt.IsZero() ||
		event.EventAt.UnixMilli() <= 0 || event.ExchangePublishedAt.UnixMilli() <= 0 || event.ReceivedAt.UnixMilli() <= 0 {
		return Event{}, errors.New("event, exchange-published, and received timestamps are required")
	}
	if event.ExchangePublishedAt.After(event.ReceivedAt.Add(5*time.Second)) || event.EventAt.After(event.ReceivedAt.Add(5*time.Second)) {
		return Event{}, errors.New("exchange timestamp is implausibly far in the future")
	}
	if event.SourceSessionID == "" || strings.TrimSpace(sourceIdentity) == "" {
		return Event{}, errors.New("source session and deterministic source identity are required")
	}
	if !finitePositive(event.Quantity) || event.QuantityUnit == "" {
		return Event{}, errors.New("quantity must be finite and positive and have a unit")
	}
	for name, value := range map[string]*float64{
		"bankruptcy_price":               event.BankruptcyPrice,
		"order_price":                    event.OrderPrice,
		"average_price":                  event.AveragePrice,
		"last_filled_quantity":           event.LastFilledQuantity,
		"accumulated_filled_quantity":    event.AccumulatedFilledQuantity,
		"estimated_liquidation_notional": event.EstimatedLiquidationNotional,
	} {
		if value != nil && !finitePositive(*value) {
			return Event{}, fmt.Errorf("%s must be finite and positive when present", name)
		}
	}
	if !json.Valid(event.RawPayload) {
		return Event{}, errors.New("raw_payload must be valid JSON")
	}

	event.SourceEventKey = sha256.Sum256([]byte(strings.Join([]string{
		CaptureVersion, event.Exchange, event.MarketType, event.NativeMarketID,
		string(event.CoverageKind), sourceIdentity,
	}, "\x1f")))
	event.PayloadHash = sha256.Sum256(event.RawPayload)
	return event, nil
}

func finitePositive(value float64) bool {
	return value > 0 && !math.IsNaN(value) && !math.IsInf(value, 0)
}

type LifecycleEvent struct {
	SessionID      string
	ConnectedAt    time.Time
	DisconnectedAt time.Time
	Reason         string
	ReadTimeout    bool
}

type EventFn func(context.Context, Event) error
type LifecycleFn func(LifecycleEvent)

type SourceStats struct {
	EventsAcceptedTotal          uint64
	EventsInvalidTotal           uint64
	EventsOutOfScopeTotal        uint64
	ScopeTagMissingAcceptedTotal uint64
	ReconnectTotal               uint64
	ReadTimeoutTotal             uint64
	LastEventAt                  time.Time
}

// Source is implemented by each venue adapter. One deployed process owns
// exactly one Source, so a venue outage cannot take down another venue.
type Source interface {
	RunLiquidations(context.Context, []string, string, EventFn, LifecycleFn) error
	CoverageKind() CoverageKind
	ExpectedConnections(symbolCount int) int
	Stats() SourceStats
}

// UniverseVersion pins the exact frozen native-symbol set for one process.
func UniverseVersion(symbols []string) string {
	normalized := make([]string, 0, len(symbols))
	for _, symbol := range symbols {
		normalized = append(normalized, strings.ToUpper(strings.TrimSpace(symbol)))
	}
	sort.Strings(normalized)
	digest := sha256.Sum256([]byte(strings.Join(normalized, "\n")))
	return fmt.Sprintf("%x", digest[:])
}
