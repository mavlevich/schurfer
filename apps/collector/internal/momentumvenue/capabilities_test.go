package momentumvenue

import (
	"strings"
	"testing"
)

func TestV1ValidatesAndKeepsUnauditedVenuesFailClosed(t *testing.T) {
	matrix := V1()
	if err := matrix.Validate(); err != nil {
		t.Fatalf("Validate() error = %v", err)
	}

	gate, ok := matrix.Venue("gate", "linear_usdt_perpetual")
	if !ok {
		t.Fatal("gate venue missing")
	}
	if gate.Trades.Status != StatusNotAudited || gate.Trades.Transport != TransportUnknown {
		t.Fatalf("gate trades = %#v, want fail-closed not_audited/unknown", gate.Trades)
	}
}

func TestV1DoesNotEquateDocumentedBinanceWithImplementedBybit(t *testing.T) {
	matrix := V1()
	bybit, _ := matrix.Venue("bybit", "linear_usdt_perpetual")
	binance, _ := matrix.Venue("binance", "linear_usdt_perpetual")

	if bybit.Trades.Status != StatusImplemented {
		t.Fatalf("Bybit trade status = %q", bybit.Trades.Status)
	}
	if binance.Trades.Status != StatusDocumented {
		t.Fatalf("Binance trade status = %q", binance.Trades.Status)
	}
	if binance.OIValue.Status != StatusProbeRequired {
		t.Fatalf("Binance OI-value status = %q", binance.OIValue.Status)
	}
	if !strings.Contains(strings.Join(binance.Trades.Constraints, " "), "not semantically identical") {
		t.Fatalf("Binance trade constraints do not preserve aggregation mismatch: %#v", binance.Trades.Constraints)
	}
}

func TestV1RecordsCurrentBybitUniverseScopeGap(t *testing.T) {
	bybit, _ := V1().Venue("bybit", "linear_usdt_perpetual")
	if !strings.Contains(strings.Join(bybit.Universe.Constraints, " "), "contractType") {
		t.Fatalf("Bybit universe constraints hide the current contract-type gap: %#v", bybit.Universe.Constraints)
	}
}

func TestValidateRejectsImplementedCapabilityWithoutEvidence(t *testing.T) {
	matrix := V1()
	matrix.Venues[0].Trades.EvidenceURLs = nil
	if err := matrix.Validate(); err == nil || !strings.Contains(err.Error(), "official evidence") {
		t.Fatalf("Validate() error = %v, want official-evidence failure", err)
	}
}

func TestValidateRejectsUnauditedCapabilityThatClaimsTransport(t *testing.T) {
	matrix := V1()
	matrix.Venues[2].Trades.Transport = TransportWebSocket
	if err := matrix.Validate(); err == nil || !strings.Contains(err.Error(), "unknown transport") {
		t.Fatalf("Validate() error = %v, want fail-closed transport failure", err)
	}
}

func TestValidateRejectsDuplicateVenue(t *testing.T) {
	matrix := V1()
	matrix.Venues = append(matrix.Venues, matrix.Venues[0])
	if err := matrix.Validate(); err == nil || !strings.Contains(err.Error(), "duplicate venue") {
		t.Fatalf("Validate() error = %v, want duplicate failure", err)
	}
}

func TestKeysAreDeterministic(t *testing.T) {
	got := V1().Keys()
	want := []string{
		"binance:linear_usdt_perpetual",
		"bitget:linear_usdt_perpetual",
		"bybit:linear_usdt_perpetual",
		"gate:linear_usdt_perpetual",
		"mexc:linear_usdt_perpetual",
		"okx:linear_usdt_perpetual",
		"xt:linear_usdt_perpetual",
	}
	if len(got) != len(want) {
		t.Fatalf("Keys() = %#v, want %#v", got, want)
	}
	for index := range want {
		if got[index] != want[index] {
			t.Fatalf("Keys()[%d] = %q, want %q", index, got[index], want[index])
		}
	}
}
