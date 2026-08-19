package momentumsource

import (
	"fmt"
	"time"
)

// IdentityStatus is a single venue's own read on whether one Instrument's
// identity metadata is usable at all -- never a cross-venue judgment.
// Foundation-stage code (this package, bybit.SymbolCatalog, binance.
// SymbolCatalog) only ever produces these five values; a value like
// "confirmed" or "conflict" belongs to a later cross-venue RESOLUTION
// step (not implemented yet) that compares two venues' own Instruments
// against each other -- something this package's own single-venue catalog
// fetch has no way to do and must not pretend to.
type IdentityStatus string

const (
	// IdentityStatusReady means every field IdentityKey needs is present
	// and passed its own validation. This is the only status IdentityKey
	// ever returns true for.
	IdentityStatusReady IdentityStatus = "ready"
	// IdentityStatusMissingOnboardedAt means the venue's own catalog
	// response had no onboarding/launch timestamp for this instrument at
	// all (a genuinely absent field, not a zero or malformed one).
	IdentityStatusMissingOnboardedAt IdentityStatus = "missing_onboarded_at"
	// IdentityStatusInvalidOnboardedAt means a timestamp was present but
	// failed validation (non-positive, or unreasonably far in the future
	// relative to when the catalog was fetched) -- kept distinct from
	// "missing" because a garbled timestamp is a different failure to
	// investigate than an absent one.
	IdentityStatusInvalidOnboardedAt IdentityStatus = "invalid_onboarded_at"
	// IdentityStatusInvalidAssets means Base, Quote, or Settle failed
	// validation (empty, or -- for Quote/Settle specifically -- not the
	// single asset this foundation stage supports; see NewInstrument).
	IdentityStatusInvalidAssets IdentityStatus = "invalid_assets"
	// IdentityStatusUnsupportedMarketType means CanonicalMarketType is not
	// one this foundation stage has validated end to end (today, only
	// "linear_usdt_perpetual" -- see momentumvenue's own capability
	// matrix). A future venue with a genuinely different market type needs
	// its own deliberate review before this status stops applying to it,
	// not a silent pass-through.
	IdentityStatusUnsupportedMarketType IdentityStatus = "unsupported_market_type"
)

// linearUSDTPerpetual is the only CanonicalMarketType this foundation
// stage validates: matches bybit.MarketType/binance.MarketType verbatim
// (both already "linear_usdt_perpetual", see momentumvenue's own capacity
// matrix naming) rather than inventing a second, parallel market-type
// vocabulary just for identity.
const linearUSDTPerpetual = "linear_usdt_perpetual"

// foundationSettlementAsset is the only Quote/Settle asset this foundation
// stage validates -- mirrors linearUSDTPerpetual's own USDT-only scope.
// bybit.go/binance.go already filter their own catalogs to USDT quote and
// settle before an Instrument is ever built, so this check is currently
// redundant in practice; it exists anyway (a code-review finding) because
// this type's own contract is fail-closed enforcement that does not trust
// a caller's own filtering discipline alone -- exactly the same reasoning
// that puts identity_key_only_when_ready at the DB layer too, not just in
// NewInstrument's switch.
const foundationSettlementAsset = "USDT"

// Instrument is one venue's own point-in-time record of one instrument's
// identity metadata: never a cross-venue claim. It is deliberately built
// only through NewInstrument, which classifies IdentityStatus itself from
// the raw inputs -- a caller cannot construct a "ready" Instrument with
// missing or invalid fields by skipping validation.
type Instrument struct {
	Exchange       string
	NativeMarketID string
	Base           string
	Quote          string
	Settle         string
	// NativeMarketType is the venue's own wire-format market type string
	// verbatim (e.g. Bybit's "LinearPerpetual" contractType, Binance's
	// "PERPETUAL" contractType) -- kept for provenance/debugging, never
	// used for cross-venue comparison (CanonicalMarketType is).
	NativeMarketType string
	// CanonicalMarketType is the momentumsource-level normalized market
	// type (see bybit.MarketType/binance.MarketType) -- what a future
	// cross-venue resolution step actually compares.
	CanonicalMarketType string
	// OnboardedAt is nil unless IdentityStatus is IdentityStatusReady.
	OnboardedAt    *time.Time
	IdentityStatus IdentityStatus
}

// NewInstrument builds one Instrument from a venue's own already-decoded
// catalog fields, classifying IdentityStatus itself rather than trusting
// a caller's own judgment. onboardedAt is nil when the venue's response
// had no value for it at all (IdentityStatusMissingOnboardedAt); pass a
// non-nil but invalid time (e.g. the zero time, or one far in observedAt's
// future) to distinguish a garbled value from an absent one
// (IdentityStatusInvalidOnboardedAt).
//
// Known limitation (foundation stage is deliberately stateless -- each
// call classifies purely from its own observedAt, with no memory of a
// previous fetch's own classification for the same native_market_id): a
// garbled onboardedAt that happens to land only a few days or weeks in
// observedAt's future is correctly IdentityStatusInvalidOnboardedAt on the
// fetch that first observes it, but silently reclassifies to
// IdentityStatusReady on a later routine re-fetch, once real time has
// simply caught up past it -- the same fabricated value, no longer
// distinguishable from a genuine one at that point. Catching this would
// need comparing a new fetch's own row against the previous one's for the
// same identity, which is a cross-fetch history concern for a future
// resolution-stage step, not something a single stateless catalog fetch
// can do on its own.
func NewInstrument(
	exchange string,
	nativeMarketID string,
	base string,
	quote string,
	settle string,
	nativeMarketType string,
	canonicalMarketType string,
	onboardedAt *time.Time,
	observedAt time.Time,
) Instrument {
	instrument := Instrument{
		Exchange:            exchange,
		NativeMarketID:      nativeMarketID,
		Base:                base,
		Quote:               quote,
		Settle:              settle,
		NativeMarketType:    nativeMarketType,
		CanonicalMarketType: canonicalMarketType,
	}
	switch {
	case base == "" || quote != foundationSettlementAsset || settle != foundationSettlementAsset:
		instrument.IdentityStatus = IdentityStatusInvalidAssets
	case canonicalMarketType != linearUSDTPerpetual:
		instrument.IdentityStatus = IdentityStatusUnsupportedMarketType
	case onboardedAt == nil:
		instrument.IdentityStatus = IdentityStatusMissingOnboardedAt
	case onboardedAt.IsZero() || onboardedAt.After(observedAt.Add(24*time.Hour)):
		// The 24h tolerance is not a clock-drift allowance (this
		// collector's own network/clock-skew tolerances elsewhere are a
		// few seconds, a different concern entirely -- comparing this
		// process's clock to a venue's trade timestamps in-flight). It is
		// a deliberately loose bound against a venue listing announcement
		// landing in this catalog fetch slightly ahead of the instrument
		// actually being tradeable -- a launch date at or shortly after
		// observedAt is a real, valid case (a symbol that just listed),
		// not a sign of a garbled value; a value far enough in the future
		// to exceed a full day is instead treated as garbled.
		instrument.IdentityStatus = IdentityStatusInvalidOnboardedAt
	default:
		value := *onboardedAt
		instrument.OnboardedAt = &value
		instrument.IdentityStatus = IdentityStatusReady
	}
	return instrument
}

// ClassifyOnboardedAtMs turns an already-decoded Unix-milliseconds
// onboarding/launch timestamp into the *time.Time NewInstrument expects
// for its own onboardedAt parameter -- the one classification rule shared
// by every venue's own onboarding-timestamp field (a code-review finding:
// bybit.parseLaunchTimeMs and binance.parseOnboardDateMs originally
// reimplemented this identically, and the negative-value fix below had to
// be applied to both copies separately before this was factored out).
//
// present distinguishes "the venue's response had no value for this field
// at all" from "the venue's response had exactly 0" -- both classify
// identically here (NewInstrument's own IdentityStatusMissingOnboardedAt),
// but callers decode that distinction differently per venue wire format
// (Bybit's launchTime is a string that can be empty; Binance's
// onboardDate is a JSON number represented as a *int64, nil when absent).
// A negative ms is real data the venue actually sent, just semantically
// impossible as an onboard date, so it is kept distinct from both: a
// non-nil pointer to the zero time.Time, which NewInstrument's own
// validation turns into IdentityStatusInvalidOnboardedAt, never silently
// folded into "missing".
func ClassifyOnboardedAtMs(ms int64, present bool) *time.Time {
	if !present || ms == 0 {
		return nil
	}
	if ms < 0 {
		invalid := time.Time{}
		return &invalid
	}
	onboardedAt := time.UnixMilli(ms)
	return &onboardedAt
}

// IdentityKey returns this Instrument's own versioned identity key and
// true, or "" and false if IdentityStatus is not IdentityStatusReady.
// Format: "<exchange>:<canonical_market_type>:<native_market_id>:
// <onboarded_at_unix_ms>" -- the trailing onboarded-at component is what
// makes this a VERSIONED identity: a symbol delisted and relisted later
// under the same native market id gets a different key, matching the
// same principle schurfer_analytics.instruments/momentum_flow_event_
// repository already establish on the Python analytics side (never merge
// cross-venue assets by bare symbol; use exchange + market id + onboard
// date). This foundation stage never compares two Instruments against
// each other -- that is a later cross-venue resolution step's job.
func (i Instrument) IdentityKey() (string, bool) {
	if i.IdentityStatus != IdentityStatusReady || i.OnboardedAt == nil {
		return "", false
	}
	return fmt.Sprintf(
		"%s:%s:%s:%d",
		i.Exchange, i.CanonicalMarketType, i.NativeMarketID, i.OnboardedAt.UnixMilli(),
	), true
}
