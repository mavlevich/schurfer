// Package momentumsource defines the narrow, venue-agnostic capability
// interfaces the momentum-capture line is built on: UniverseSource,
// TradeSource, TickerSource, and OpenInterestSource. This package never
// dials a venue itself -- it is the shared contract an adapter (see
// apps/collector/internal/bybit's Adapter, and a future Binance adapter)
// implements, and that a canonical consumer depends on instead of any one
// venue's own concrete types.
//
// Per docs/research/momentum-venue-capability-matrix-v1.md's own
// architectural decision: this is deliberately not one large Exchange
// interface every adapter half-implements. A venue implements only the
// capabilities it actually has; a missing capability is never zero,
// neutral, or silently substituted from another venue -- see
// apps/collector/internal/momentumvenue for the fail-closed capability
// matrix these interfaces are meant to be checked against before an
// adapter is wired into any consumer.
package momentumsource

import (
	"context"
	"fmt"
	"time"
)

// Envelope is the identity/provenance header every canonical event carries,
// independent of which venue or capability produced it. Exchange and
// MarketType match momentumvenue.Venue's own fields (e.g. "bybit" /
// "linear_usdt_perpetual"), so a consumer can look up the matrix entry for
// any event it receives. NativeMarketID is the venue's own instrument
// identifier verbatim (e.g. Bybit's "BTCUSDT") -- never a normalized or
// guessed symbol, matching the project's existing "exact instrument
// identity" convention (see schurfer_analytics.momentum_flow_event_
// repository's own identity contract on the Python analytics side).
type Envelope struct {
	Exchange       string
	MarketType     string
	NativeMarketID string
	// EventAt is the venue's own exchange-reported time for this event.
	// ReceivedAt is this collector's own wall-clock receive time. Neither
	// substitutes for the other: EventAt can lag ReceivedAt by real network
	// and venue-side latency, and that gap is itself diagnostic information,
	// not noise to discard.
	EventAt    time.Time
	ReceivedAt time.Time
	// SessionID identifies one physical connection/dial. A change in
	// SessionID for the same NativeMarketID is the authoritative signal
	// that this is a different physical connection -- see bybit.Source's
	// own StreamSessionID field, whose semantics this preserves verbatim
	// for the Bybit adapter.
	SessionID string
}

// Trade is one normalized public trade.
type Trade struct {
	Envelope
	TradeID string
	// Side is the exchange-reported taker side: "buy" or "sell", lowercase.
	Side  string
	Price float64
	Size  float64
}

type TradeConsumer func(context.Context, Trade) error

// TradeSource streams public trades for the given native market ids. Blocks
// until ctx is cancelled or an unrecoverable error occurs; reconnect
// behavior is the implementation's own responsibility, matching
// bybit.Source.RunTrades's existing contract.
type TradeSource interface {
	StreamTrades(ctx context.Context, symbols []string, consume TradeConsumer) error
}

// TickerUpdate is one normalized ticker snapshot: price and best bid/ask,
// each optional since a venue's push protocol may omit an unchanged field
// on a given message (see bybit.TickerEvent's own delta semantics, which
// this type preserves). OpenInterest is populated only when this same
// ticker message is also this venue's OWN source of OI (true for Bybit;
// false for a venue where OI is a separate poll -- see OpenInterestSource).
// A nil OpenInterest here is never "OI is zero"; it is one of two distinct
// conditions an implementation must document which of it means:
//  1. this venue's ticker transport does not carry OI at all, and any real
//     reading must come from a distinct OpenInterestSource implementation;
//  2. this venue's transport DOES carry OI, but nothing has been observed
//     yet in the current connection episode (e.g. bybit.Adapter: OI state
//     resets on every reconnect, so nil here is transient, not permanent --
//     see bybit.OpenInterestFromTicker's own doc comment).
//
// Do not treat nil as case 1 unconditionally; check the adapter's own
// documented behavior.
type TickerUpdate struct {
	Envelope
	LastPrice     *string
	Price24hPct   *string
	High24h       *string
	Low24h        *string
	Volume24h     *string
	Turnover24h   *string
	Bid           *string
	Ask           *string
	OpenInterest  *OpenInterestReading
	MessageType   string
	CrossSequence *int64
}

// TickerConsumer receives each TickerUpdate. Unlike TradeConsumer, whether a
// returned error causes the underlying implementation to stop/reconnect the
// stream is NOT guaranteed the same way across adapters -- see bybit.
// Adapter.StreamTicker's own doc comment for a concrete case where a
// returned error is only logged and the stream keeps running.
type TickerConsumer func(context.Context, TickerUpdate) error

// TickerSource streams ticker updates for the given native market ids.
type TickerSource interface {
	StreamTicker(ctx context.Context, symbols []string, consume TickerConsumer) error
}

// ValueProvenance distinguishes a value the venue itself returned from one
// this codebase computed from other inputs. Per momentumvenue's own
// architectural note, a derived value must never be silently treated as
// equivalent to a native one -- provenance travels with the value, not just
// in a comment. The zero value ("") means neither: no value is present at
// all, so provenance does not apply -- an implementation must never stamp
// ProvenanceNative or ProvenanceDerived on a field whose own value pointer
// is nil (see bybit.OpenInterestFromTicker's own provenanceIfPresent
// helper, added after a code-review finding on exactly this).
type ValueProvenance string

const (
	ProvenanceNative  ValueProvenance = "native"
	ProvenanceDerived ValueProvenance = "derived"
)

// OpenInterestReading is one open-interest observation: contract quantity
// (Amount) and, where available, its USD notional (Value) -- see
// docs/research/binance-momentum-capability-preflight-v1.md on why these
// are not interchangeable and not always both available at the same
// freshness. Each has its own event/observed timestamp pair, independent of
// Envelope.EventAt/ReceivedAt: a delta-style push (Bybit) can carry this
// same reading across several messages without a fresh update, and a
// poll-style source (Binance) may only refresh the two fields on different
// cadences entirely (see the preflight's own finding that openInterestHist
// is 5-minute-or-coarser while openInterest itself is near-real-time).
type OpenInterestReading struct {
	AmountProvenance ValueProvenance
	Amount           *string
	AmountEventAt    *time.Time
	AmountObservedAt *time.Time
	ValueProvenance  ValueProvenance
	Value            *string
	ValueEventAt     *time.Time
	ValueObservedAt  *time.Time
}

type OpenInterestConsumer func(context.Context, Envelope, OpenInterestReading) error

// OpenInterestSource streams (or, for a poll-based venue, periodically
// emits) open-interest readings for the given native market ids,
// independent of TickerSource. Deliberately a separate interface, not a
// method on TickerSource: a venue whose OI arrives embedded in its ticker
// push (Bybit) does not need a second, redundant subscription just to
// satisfy this interface too -- see bybit.Adapter's own doc comment on why
// it does NOT implement this interface, and OpenInterestFromTicker instead.
type OpenInterestSource interface {
	StreamOpenInterest(ctx context.Context, symbols []string, consume OpenInterestConsumer) error
}

// UniverseSnapshot is one point-in-time classification of a venue's
// instrument catalog, restricted to whatever this capture line's own scope
// requires (e.g. Bybit's crypto-linear-perpetual filter). ExclusionCounts
// is a reason -> count map, deliberately not a single number: dropping the
// per-reason breakdown was the original Bybit universe bug this project
// already fixed once (see docs/research/momentum-venue-capability-matrix-
// v1.md's "Bybit universe remediation" section) -- every future venue must
// keep the same visibility, not silently regress to a single opaque count.
type UniverseSnapshot struct {
	Exchange          string
	MarketType        string
	IncludedSymbols   []string
	TotalCatalogItems int
	ExclusionCounts   map[string]int
}

// Validate enforces the same fail-closed accounting bybit.validateCatalog
// already checks privately: every catalog item is accounted for exactly
// once, either included or excluded under a named reason. An unclassified
// remainder is a bug in the adapter's own classification, not a acceptable
// gap -- see momentumvenue's "missing capability is never... neutral"
// principle applied to universe accounting specifically.
func (s UniverseSnapshot) Validate() error {
	if s.Exchange == "" || s.MarketType == "" {
		return fmt.Errorf("universe snapshot: exchange and market type are required")
	}
	classified := len(s.IncludedSymbols)
	for _, count := range s.ExclusionCounts {
		classified += count
	}
	if classified != s.TotalCatalogItems {
		return fmt.Errorf(
			"universe snapshot classification mismatch: total=%d classified=%d",
			s.TotalCatalogItems, classified,
		)
	}
	seen := make(map[string]struct{}, len(s.IncludedSymbols))
	for _, symbol := range s.IncludedSymbols {
		if symbol == "" {
			return fmt.Errorf("universe snapshot: included symbol must not be empty")
		}
		if _, exists := seen[symbol]; exists {
			return fmt.Errorf("universe snapshot: duplicate included symbol %q", symbol)
		}
		seen[symbol] = struct{}{}
	}
	return nil
}

type UniverseSource interface {
	FetchUniverse(ctx context.Context) (UniverseSnapshot, error)
}
