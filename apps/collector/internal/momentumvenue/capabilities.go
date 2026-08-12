// Package momentumvenue defines the fail-closed venue capability contract for
// the early-momentum capture line. It describes what a venue can provide; it
// does not connect to a venue or authorize a production feed.
package momentumvenue

import (
	"errors"
	"fmt"
	"sort"
	"strings"
	"time"
)

const MatrixVersion = "momentum_venue_capability_matrix_v1"

type SupportStatus string

const (
	StatusImplemented   SupportStatus = "implemented"
	StatusDocumented    SupportStatus = "officially_documented"
	StatusProbeRequired SupportStatus = "probe_required"
	StatusUnsupported   SupportStatus = "unsupported"
	StatusNotAudited    SupportStatus = "not_audited"
)

type Transport string

const (
	TransportWebSocket Transport = "websocket"
	TransportRESTPoll  Transport = "rest_poll"
	TransportNone      Transport = "none"
	TransportUnknown   Transport = "unknown"
)

type Capability struct {
	Status             SupportStatus
	Transport          Transport
	Semantics          string
	ExchangeTimestamp  string
	Sequence           string
	EvidenceURLs       []string
	ImplementationRefs []string
	Constraints        []string
}

type Venue struct {
	Exchange      string
	MarketType    string
	IntendedRoles []string
	Universe      Capability
	Trades        Capability
	OIAmount      Capability
	OIValue       Capability
	Liquidations  Capability
	Lifecycle     Capability
}

type Matrix struct {
	SchemaVersion string
	AsOf          time.Time
	Venues        []Venue
}

// V1 returns the reviewed matrix. A documented capability is deliberately not
// treated as implemented: every new adapter still needs fixtures, a bounded
// live probe, gap semantics, and its own canary before it can publish canonical
// observations.
func V1() Matrix {
	bybitTicker := "https://bybit-exchange.github.io/docs/v5/websocket/public/ticker"
	bybitTrades := "https://bybit-exchange.github.io/docs/v5/websocket/public/trade"
	bybitLiquidations := "https://bybit-exchange.github.io/docs/v5/websocket/public/all-liquidation"
	bybitInstruments := "https://bybit-exchange.github.io/docs/v5/market/instrument"
	bybitConnect := "https://bybit-exchange.github.io/docs/v5/ws/connect"
	binanceMarketStreams := "https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/ws-streams/market"
	binanceMarketREST := "https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data"

	return Matrix{
		SchemaVersion: MatrixVersion,
		AsOf:          time.Date(2026, time.August, 12, 0, 0, 0, 0, time.UTC),
		Venues: []Venue{
			{
				Exchange:      "bybit",
				MarketType:    "linear_usdt_perpetual",
				IntendedRoles: []string{"confirmation", "execution"},
				Universe: Capability{
					Status:             StatusImplemented,
					Transport:          TransportRESTPoll,
					Semantics:          "Trading category=linear symbols with quoteCoin=settleCoin=USDT, frozen at process start",
					EvidenceURLs:       []string{bybitInstruments},
					ImplementationRefs: []string{"apps/collector/internal/bybit/bybit.go:FetchSymbols"},
					Constraints: []string{
						"universe changes require a controlled restart in v1",
						"current decoder does not retain/filter contractType; perpetual-only scope must be proved before the canonical adapter",
					},
				},
				Trades: Capability{
					Status:             StatusImplemented,
					Transport:          TransportWebSocket,
					Semantics:          "individual public trades with exchange-reported taker side; block/RPI flags retained",
					ExchangeTimestamp:  "data.T fill timestamp in milliseconds",
					Sequence:           "data.seq retained for diagnostics only; not assumed contiguous",
					EvidenceURLs:       []string{bybitTrades},
					ImplementationRefs: []string{"apps/collector/internal/bybit/trades.go:RunTradesWithLifecycle"},
				},
				OIAmount: Capability{
					Status:             StatusImplemented,
					Transport:          TransportWebSocket,
					Semantics:          "native both-sides openInterest contract quantity; delta carry-forward retains the last field-specific source/receive timestamps",
					ExchangeTimestamp:  "ticker envelope ts for the message that actually carried openInterest",
					Sequence:           "cross sequence retained as diagnostic context only",
					EvidenceURLs:       []string{bybitTicker},
					ImplementationRefs: []string{"apps/collector/internal/bybit/ws.go", "apps/collector/internal/bybit/bybit.go:TickerEvent"},
				},
				OIValue: Capability{
					Status:             StatusImplemented,
					Transport:          TransportWebSocket,
					Semantics:          "native both-sides openInterestValue supplied by Bybit",
					ExchangeTimestamp:  "ticker envelope ts for the message that actually carried openInterestValue",
					Sequence:           "cross sequence retained as diagnostic context only",
					EvidenceURLs:       []string{bybitTicker},
					ImplementationRefs: []string{"apps/collector/internal/bybit/ws.go", "apps/collector/internal/bybit/bybit.go:TickerEvent"},
				},
				Liquidations: Capability{
					Status:            StatusDocumented,
					Transport:         TransportWebSocket,
					Semantics:         "allLiquidation per symbol, position side, executed size, and bankruptcy price",
					ExchangeTimestamp: "data.T update timestamp in milliseconds",
					EvidenceURLs:      []string{bybitLiquidations},
					Constraints:       []string{"not implemented or live-probed in Schurfer"},
				},
				Lifecycle: Capability{
					Status:             StatusImplemented,
					Transport:          TransportWebSocket,
					Semantics:          "fresh local session id per dial plus per-trade-shard connect/disconnect events and read deadlines",
					EvidenceURLs:       []string{bybitConnect},
					ImplementationRefs: []string{"apps/collector/internal/bybit/ws.go:newStreamSessionID", "apps/collector/internal/bybit/trades.go:TradeLifecycleEvent"},
				},
			},
			{
				Exchange:      "binance",
				MarketType:    "linear_usdt_perpetual",
				IntendedRoles: []string{"confirmation", "execution"},
				Universe: Capability{
					Status:       StatusDocumented,
					Transport:    TransportRESTPoll,
					Semantics:    "USD-M symbol catalog and trading rules from exchangeInfo",
					EvidenceURLs: []string{binanceMarketREST},
					Constraints:  []string{"adapter and point-in-time universe drift handling not implemented"},
				},
				Trades: Capability{
					Status:            StatusDocumented,
					Transport:         TransportWebSocket,
					Semantics:         "aggTrade groups market fills with the same price and taking side in 100ms; buyer-maker flag permits taker-side derivation",
					ExchangeTimestamp: "T trade time and E event time in milliseconds",
					Sequence:          "aggregate trade id plus first/last underlying trade ids",
					EvidenceURLs:      []string{binanceMarketStreams},
					Constraints: []string{
						"not implemented or live-probed in Schurfer",
						"100ms aggregation is not semantically identical to Bybit individual trades",
						"do not compare top-K or large-trade histograms until aggregation compatibility is explicitly contracted",
					},
				},
				OIAmount: Capability{
					Status:            StatusDocumented,
					Transport:         TransportRESTPoll,
					Semantics:         "native current openInterest contract quantity per symbol",
					ExchangeTimestamp: "response time field in milliseconds",
					EvidenceURLs:      []string{binanceMarketREST},
					Constraints:       []string{"poll cadence, rate-limit budget, and stale-read behavior require a bounded probe"},
				},
				OIValue: Capability{
					Status:       StatusProbeRequired,
					Transport:    TransportUnknown,
					Semantics:    "no native current OI-value field is established by the reviewed openInterest endpoint",
					EvidenceURLs: []string{binanceMarketREST},
					Constraints: []string{
						"do not silently substitute openInterest multiplied by an unmatched current price",
						"a derived value requires an explicit price source, timestamp-alignment rule, and provenance",
					},
				},
				Liquidations: Capability{
					Status:            StatusDocumented,
					Transport:         TransportWebSocket,
					Semantics:         "force-order snapshots; at most the latest liquidation per symbol in each 1000ms interval",
					ExchangeTimestamp: "T order trade time and E event time in milliseconds",
					EvidenceURLs:      []string{binanceMarketStreams},
					Constraints: []string{
						"documented stream is censored and must not be labelled a complete liquidation tape",
						"not implemented or live-probed in Schurfer",
					},
				},
				Lifecycle: Capability{
					Status:       StatusProbeRequired,
					Transport:    TransportWebSocket,
					Semantics:    "adapter must expose connection epochs, planned server disconnects, per-feed gaps, and local receive timestamps",
					EvidenceURLs: []string{binanceMarketStreams},
					Constraints:  []string{"no Schurfer adapter or bounded reconnect probe yet"},
				},
			},
			unauditedVenue("okx", []string{"confirmation", "execution"}),
			unauditedVenue("bitget", []string{"confirmation", "execution"}),
			unauditedVenue("gate", []string{"discovery_source"}),
			unauditedVenue("mexc", []string{"discovery_source"}),
			unauditedVenue("xt", []string{"discovery_source"}),
		},
	}
}

func unauditedVenue(exchange string, roles []string) Venue {
	unknown := Capability{
		Status:    StatusNotAudited,
		Transport: TransportUnknown,
		Semantics: "requires official-contract audit and bounded live probe",
	}
	return Venue{
		Exchange: exchange, MarketType: "linear_usdt_perpetual", IntendedRoles: roles,
		Universe: unknown, Trades: unknown, OIAmount: unknown, OIValue: unknown,
		Liquidations: unknown, Lifecycle: unknown,
	}
}

func (matrix Matrix) Validate() error {
	if matrix.SchemaVersion != MatrixVersion {
		return fmt.Errorf("schema version %q, want %q", matrix.SchemaVersion, MatrixVersion)
	}
	if matrix.AsOf.IsZero() {
		return errors.New("as-of timestamp is required")
	}
	if len(matrix.Venues) == 0 {
		return errors.New("at least one venue is required")
	}

	seen := make(map[string]struct{}, len(matrix.Venues))
	for index, venue := range matrix.Venues {
		key := venue.Exchange + ":" + venue.MarketType
		if strings.TrimSpace(venue.Exchange) == "" || strings.TrimSpace(venue.MarketType) == "" {
			return fmt.Errorf("venue %d: exchange and market type are required", index)
		}
		if _, ok := seen[key]; ok {
			return fmt.Errorf("duplicate venue %q", key)
		}
		seen[key] = struct{}{}
		if len(venue.IntendedRoles) == 0 {
			return fmt.Errorf("venue %q: at least one intended role is required", key)
		}
		capabilities := map[string]Capability{
			"universe": venue.Universe, "trades": venue.Trades,
			"oi_amount": venue.OIAmount, "oi_value": venue.OIValue,
			"liquidations": venue.Liquidations, "lifecycle": venue.Lifecycle,
		}
		for name, capability := range capabilities {
			if err := validateCapability(capability); err != nil {
				return fmt.Errorf("venue %q capability %q: %w", key, name, err)
			}
		}
	}
	return nil
}

func validateCapability(capability Capability) error {
	validStatuses := map[SupportStatus]bool{
		StatusImplemented: true, StatusDocumented: true, StatusProbeRequired: true,
		StatusUnsupported: true, StatusNotAudited: true,
	}
	if !validStatuses[capability.Status] {
		return fmt.Errorf("invalid status %q", capability.Status)
	}
	validTransports := map[Transport]bool{
		TransportWebSocket: true, TransportRESTPoll: true,
		TransportNone: true, TransportUnknown: true,
	}
	if !validTransports[capability.Transport] {
		return fmt.Errorf("invalid transport %q", capability.Transport)
	}
	if strings.TrimSpace(capability.Semantics) == "" {
		return errors.New("semantics are required")
	}
	if capability.Status == StatusImplemented && len(capability.ImplementationRefs) == 0 {
		return errors.New("implemented capability requires an implementation reference")
	}
	if capability.Status == StatusImplemented || capability.Status == StatusDocumented {
		if len(capability.EvidenceURLs) == 0 {
			return errors.New("implemented/documented capability requires official evidence")
		}
	}
	if capability.Status == StatusUnsupported && capability.Transport != TransportNone {
		return errors.New("unsupported capability must use transport none")
	}
	if capability.Status == StatusNotAudited {
		if capability.Transport != TransportUnknown {
			return errors.New("not-audited capability must use unknown transport")
		}
		if len(capability.EvidenceURLs) != 0 || len(capability.ImplementationRefs) != 0 {
			return errors.New("not-audited capability cannot claim evidence or implementation")
		}
	}
	for _, rawURL := range capability.EvidenceURLs {
		if !strings.HasPrefix(rawURL, "https://") {
			return fmt.Errorf("evidence URL %q must use https", rawURL)
		}
	}
	return nil
}

func (matrix Matrix) Venue(exchange, marketType string) (Venue, bool) {
	for _, venue := range matrix.Venues {
		if venue.Exchange == exchange && venue.MarketType == marketType {
			return venue, true
		}
	}
	return Venue{}, false
}

func (matrix Matrix) Keys() []string {
	keys := make([]string, 0, len(matrix.Venues))
	for _, venue := range matrix.Venues {
		keys = append(keys, venue.Exchange+":"+venue.MarketType)
	}
	sort.Strings(keys)
	return keys
}
