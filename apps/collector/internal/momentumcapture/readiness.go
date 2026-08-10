package momentumcapture

// ReadinessTracker tracks, for each symbol in a frozen Universe, whether at
// least one trade and one ticker/OI observation has ever been accepted for
// it. A symbol only becomes "ready" once both are true; until then, its
// bars are expected to show TradesComplete/TickerComplete=false from the
// momentum engine's own health tracking (see package momentum). This
// tracker exists purely to make that visible in the health snapshot as
// SymbolsMissingTicker/SymbolsMissingTrades without needing to reach into
// the engine's internal state.
//
// A symbol can end up permanently in SymbolsMissingTicker even on a
// perfectly healthy momentum-capture process: cmd/collector (which
// supplies ticker/OI over NATS) freezes its OWN universe independently, at
// its OWN process startup. If that universe is narrower than, or was
// captured before a listing that momentum-capture's own universe includes,
// some symbols will simply never appear on the ticker feed at all. This is
// a structural cross-process drift risk, not necessarily message loss, and
// SymbolsMissingTicker must be read with that in mind rather than assumed
// to always mean "something is broken right now".
//
// Not safe for concurrent use: like package momentum's Engine, this is
// meant to be owned by a single event-loop goroutine (see the bounded
// event loop step of this PR).
type ReadinessTracker struct {
	universe   Universe
	symbolSet  map[string]struct{}
	seenTicker map[string]struct{}
	seenTrades map[string]struct{}
}

// NewReadinessTracker returns a tracker scoped to exactly universe's
// symbols; observations for any other symbol are ignored (see
// ObserveTicker/ObserveTrade), matching the frozen-universe contract's
// "never silently absorb an out-of-scope symbol" rule.
func NewReadinessTracker(universe Universe) *ReadinessTracker {
	symbolSet := make(map[string]struct{}, universe.Count())
	for _, symbol := range universe.Symbols {
		symbolSet[symbol] = struct{}{}
	}
	return &ReadinessTracker{
		universe:   universe,
		symbolSet:  symbolSet,
		seenTicker: make(map[string]struct{}),
		seenTrades: make(map[string]struct{}),
	}
}

// ObserveTicker records that a real ticker/OI observation was accepted for
// symbol. A symbol outside the frozen universe is silently ignored.
func (r *ReadinessTracker) ObserveTicker(symbol string) {
	if _, ok := r.symbolSet[symbol]; !ok {
		return
	}
	r.seenTicker[symbol] = struct{}{}
}

// ObserveTrade records that a real trade was accepted for symbol. A symbol
// outside the frozen universe is silently ignored.
func (r *ReadinessTracker) ObserveTrade(symbol string) {
	if _, ok := r.symbolSet[symbol]; !ok {
		return
	}
	r.seenTrades[symbol] = struct{}{}
}

// Ready reports whether symbol has ever had both a ticker/OI observation
// and a trade accepted. A symbol outside the frozen universe is never
// ready.
func (r *ReadinessTracker) Ready(symbol string) bool {
	if _, ok := r.symbolSet[symbol]; !ok {
		return false
	}
	_, ticker := r.seenTicker[symbol]
	_, trades := r.seenTrades[symbol]
	return ticker && trades
}

// MissingTicker returns every frozen-universe symbol that has never had a
// ticker/OI observation accepted, sorted (matching Universe.Symbols' own
// deterministic order).
func (r *ReadinessTracker) MissingTicker() []string {
	return r.missing(r.seenTicker)
}

// MissingTrades returns every frozen-universe symbol that has never had a
// trade accepted, sorted.
func (r *ReadinessTracker) MissingTrades() []string {
	return r.missing(r.seenTrades)
}

func (r *ReadinessTracker) missing(seen map[string]struct{}) []string {
	var out []string
	for _, symbol := range r.universe.Symbols {
		if _, ok := seen[symbol]; !ok {
			out = append(out, symbol)
		}
	}
	return out
}
