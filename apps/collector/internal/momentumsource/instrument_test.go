package momentumsource

import (
	"fmt"
	"testing"
	"time"
)

func TestNewInstrumentReadyOnCompleteValidData(t *testing.T) {
	t.Parallel()
	observedAt := time.Unix(2_000_000, 0).UTC()
	onboardedAt := observedAt.Add(-30 * 24 * time.Hour)
	got := NewInstrument("bybit", "BTCUSDT", "BTC", "USDT", "USDT", "LinearPerpetual", linearUSDTPerpetual, &onboardedAt, observedAt)
	if got.IdentityStatus != IdentityStatusReady {
		t.Fatalf("IdentityStatus = %q, want ready", got.IdentityStatus)
	}
	if got.OnboardedAt == nil || !got.OnboardedAt.Equal(onboardedAt) {
		t.Fatalf("OnboardedAt = %v, want %v", got.OnboardedAt, onboardedAt)
	}
	key, ok := got.IdentityKey()
	if !ok {
		t.Fatal("IdentityKey() ok = false, want true for a ready instrument")
	}
	want := fmt.Sprintf("bybit:linear_usdt_perpetual:BTCUSDT:%d", onboardedAt.UnixMilli())
	if key != want {
		t.Fatalf("IdentityKey() = %q, want %q", key, want)
	}
}

func TestNewInstrumentMissingOnboardedAt(t *testing.T) {
	t.Parallel()
	observedAt := time.Unix(2_000_000, 0).UTC()
	got := NewInstrument("binance", "ETHUSDT", "ETH", "USDT", "USDT", "PERPETUAL", linearUSDTPerpetual, nil, observedAt)
	if got.IdentityStatus != IdentityStatusMissingOnboardedAt {
		t.Fatalf("IdentityStatus = %q, want missing_onboarded_at", got.IdentityStatus)
	}
	if got.OnboardedAt != nil {
		t.Fatal("OnboardedAt must stay nil when the venue never reported one")
	}
	if _, ok := got.IdentityKey(); ok {
		t.Fatal("IdentityKey() must refuse to produce a key without a real onboarded_at")
	}
}

func TestNewInstrumentInvalidOnboardedAtZero(t *testing.T) {
	t.Parallel()
	observedAt := time.Unix(2_000_000, 0).UTC()
	zero := time.Time{}
	got := NewInstrument("bybit", "BTCUSDT", "BTC", "USDT", "USDT", "LinearPerpetual", linearUSDTPerpetual, &zero, observedAt)
	if got.IdentityStatus != IdentityStatusInvalidOnboardedAt {
		t.Fatalf("IdentityStatus = %q, want invalid_onboarded_at", got.IdentityStatus)
	}
	if got.OnboardedAt != nil {
		t.Fatal("OnboardedAt must stay nil on an invalid value, not carry the garbled zero time through")
	}
}

func TestNewInstrumentInvalidOnboardedAtFarFuture(t *testing.T) {
	t.Parallel()
	observedAt := time.Unix(2_000_000, 0).UTC()
	farFuture := observedAt.Add(365 * 24 * time.Hour)
	got := NewInstrument("bybit", "BTCUSDT", "BTC", "USDT", "USDT", "LinearPerpetual", linearUSDTPerpetual, &farFuture, observedAt)
	if got.IdentityStatus != IdentityStatusInvalidOnboardedAt {
		t.Fatalf("IdentityStatus = %q, want invalid_onboarded_at", got.IdentityStatus)
	}
}

func TestNewInstrumentOnboardedAtRecentlyIsValid(t *testing.T) {
	t.Parallel()
	// Regression: a symbol that listed minutes before this catalog fetch
	// (onboarded_at very close to observedAt, even slightly after it due
	// to clock skew between this collector and the venue) is a real,
	// valid case, not a sign of a garbled value.
	observedAt := time.Unix(2_000_000, 0).UTC()
	justListed := observedAt.Add(-5 * time.Minute)
	got := NewInstrument("binance", "NEWUSDT", "NEW", "USDT", "USDT", "PERPETUAL", linearUSDTPerpetual, &justListed, observedAt)
	if got.IdentityStatus != IdentityStatusReady {
		t.Fatalf("IdentityStatus = %q, want ready for a recently-listed symbol", got.IdentityStatus)
	}
}

func TestNewInstrumentInvalidAssets(t *testing.T) {
	t.Parallel()
	observedAt := time.Unix(2_000_000, 0).UTC()
	onboardedAt := observedAt.Add(-time.Hour)
	for _, tc := range []struct {
		name           string
		base, quote, s string
	}{
		{"empty base", "", "USDT", "USDT"},
		{"empty quote", "BTC", "", "USDT"},
		{"empty settle", "BTC", "USDT", ""},
		// Regression for a code-review finding: IdentityStatusInvalidAssets'
		// own doc comment promises Quote/Settle are rejected when they are
		// not the single asset this foundation stage supports (USDT), not
		// merely when empty -- both current venue catalogs already
		// pre-filter to USDT before reaching NewInstrument, so this proves
		// the type's own fail-closed guarantee does not rely on that
		// upstream filtering alone.
		{"non-USDT quote", "BTC", "USDC", "USDT"},
		{"non-USDT settle", "BTC", "USDT", "BUSD"},
	} {
		got := NewInstrument("bybit", "BTCUSDT", tc.base, tc.quote, tc.s, "LinearPerpetual", linearUSDTPerpetual, &onboardedAt, observedAt)
		if got.IdentityStatus != IdentityStatusInvalidAssets {
			t.Fatalf("%s: IdentityStatus = %q, want invalid_assets", tc.name, got.IdentityStatus)
		}
	}
}

func TestNewInstrumentUnsupportedMarketType(t *testing.T) {
	t.Parallel()
	observedAt := time.Unix(2_000_000, 0).UTC()
	onboardedAt := observedAt.Add(-time.Hour)
	got := NewInstrument("okx", "BTC-USDT-SWAP", "BTC", "USDT", "USDT", "SWAP", "inverse_perpetual", &onboardedAt, observedAt)
	if got.IdentityStatus != IdentityStatusUnsupportedMarketType {
		t.Fatalf("IdentityStatus = %q, want unsupported_market_type", got.IdentityStatus)
	}
	if _, ok := got.IdentityKey(); ok {
		t.Fatal("IdentityKey() must refuse an unsupported market type even with otherwise-complete data")
	}
}

// TestNewInstrumentStatusPriority pins the exact order NewInstrument
// checks failure conditions in: invalid_assets is checked before
// unsupported_market_type is checked before missing/invalid onboarded_at.
// This matters for reproducibility -- a caller with multiple simultaneous
// problems always gets the same one status back, not whichever the
// implementation happens to check last.
func TestNewInstrumentStatusPriority(t *testing.T) {
	t.Parallel()
	observedAt := time.Unix(2_000_000, 0).UTC()
	got := NewInstrument("okx", "BTC-USDT-SWAP", "", "USDT", "USDT", "SWAP", "inverse_perpetual", nil, observedAt)
	if got.IdentityStatus != IdentityStatusInvalidAssets {
		t.Fatalf("IdentityStatus = %q, want invalid_assets to take priority", got.IdentityStatus)
	}
}
