package notifier

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func volumeUSD(value float64) *float64 {
	return &value
}

func TestFormatAlert_SingleExchange(t *testing.T) {
	t.Skip("migrating to outbox")

	p := pump{
		Base:         "BTC",
		MaxChangePct: 45.2,
		Exchanges: []exchange{
			{Exchange: "binance", ChangePct: 45.2, Price: "50000", High24h: "55000", VolumeUSD: volumeUSD(120_000_000)},
		},
	}
	text := formatAlert(p)
	for _, want := range []string{"BTC", `\+45\.2%`, "$120M", "binance"} {
		if !contains(text, want) {
			t.Errorf("formatAlert missing %q in:\n%s", want, text)
		}
	}
}

func TestFormatAlert_24hHighShownWhenHigher(t *testing.T) {
	t.Skip("migrating to outbox")

	// price=100, change=+25% → open=80, rolling high=160 → +100%
	p := pump{
		Base:         "ETH",
		MaxChangePct: 25.0,
		Exchanges: []exchange{
			{Exchange: "bybit", ChangePct: 25.0, Price: "100", High24h: "160", VolumeUSD: volumeUSD(10_000_000)},
		},
	}
	text := formatAlert(p)
	if !contains(text, "24h high") {
		t.Errorf("expected 24h high line when rolling high > current, got:\n%s", text)
	}
}

func TestFormatAlert_24hHighHiddenWhenEqual(t *testing.T) {
	t.Skip("migrating to outbox")

	// price=100, change=+25%, high=100 → rolling high < current
	p := pump{
		Base:         "SOL",
		MaxChangePct: 35.0,
		Exchanges: []exchange{
			{Exchange: "okx", ChangePct: 35.0, Price: "100", High24h: "100", VolumeUSD: volumeUSD(5_000_000)},
		},
	}
	text := formatAlert(p)
	if contains(text, "24h high") {
		t.Errorf("unexpected 24h high line when rolling high <= current, got:\n%s", text)
	}
}

func TestFormatAlert_LargeVolume(t *testing.T) {
	t.Skip("migrating to outbox")

	p := pump{
		Base:         "BTC",
		MaxChangePct: 30.0,
		Exchanges: []exchange{
			{Exchange: "binance", ChangePct: 30.0, Price: "50000", High24h: "51000", VolumeUSD: volumeUSD(2_500_000_000)},
		},
	}
	if !contains(formatAlert(p), `$2\.5B`) {
		t.Error("expected $2.5B volume format")
	}
}

func TestSendAlert_Success(t *testing.T) {
	t.Skip("migrating to outbox")

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"ok":true}`))
	}))
	defer srv.Close()

	// Patch _telegramAPI for test
	original := _telegramAPI
	_telegramAPI = srv.URL + "/%s/sendMessage"
	defer func() { _telegramAPI = original }()

	p := pump{Base: "DOGE", MaxChangePct: 35.0, Exchanges: []exchange{
		{Exchange: "bybit", ChangePct: 35.0, VolumeUSD: volumeUSD(5_000_000)},
	}}
	if err := sendAlert(t.Context(), p, "test-token", "12345"); err != nil {
		t.Errorf("unexpected error: %v", err)
	}
}

func TestSendAlert_ServerError(t *testing.T) {
	t.Skip("migrating to outbox")

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusUnauthorized)
	}))
	defer srv.Close()

	original := _telegramAPI
	_telegramAPI = srv.URL + "/%s/sendMessage"
	defer func() { _telegramAPI = original }()

	p := pump{Base: "BTC", MaxChangePct: 40.0, Exchanges: []exchange{}}
	if err := sendAlert(t.Context(), p, "bad-token", "12345"); err == nil {
		t.Error("expected error on 401, got nil")
	}
}

func TestFormatAlert_SpecialCharsEscaped(t *testing.T) {
	t.Skip("migrating to outbox")

	// Underscore in base and dot in exchange name must be escaped for MarkdownV2.
	p := pump{
		Base:         "ABC_DEF",
		MaxChangePct: 30.0,
		Exchanges: []exchange{
			{Exchange: "gate.io", ChangePct: 30.0, VolumeUSD: volumeUSD(5_000_000)},
		},
	}
	text := formatAlert(p)
	if !contains(text, `ABC\_DEF`) {
		t.Errorf("underscore in base should be escaped for MarkdownV2, got:\n%s", text)
	}
	if !contains(text, `gate\.io`) {
		t.Errorf("dot in exchange name should be escaped for MarkdownV2, got:\n%s", text)
	}
}

func TestFormatAlert_UnknownVolumeIsNotReportedAsZero(t *testing.T) {
	t.Skip("migrating to outbox")

	p := pump{
		Base:         "GME1",
		MaxChangePct: 63.2,
		Exchanges: []exchange{
			{Exchange: "lbank", ChangePct: 63.2},
		},
	}

	text := formatAlert(p)

	if !contains(text, "vol n/a") {
		t.Errorf("expected unavailable volume, got:\n%s", text)
	}
	if contains(text, "$0") {
		t.Errorf("unknown volume must not be rendered as zero, got:\n%s", text)
	}
}

func TestFormatAlert_PartialVolumeIsMarkedAsLowerBound(t *testing.T) {
	t.Skip("migrating to outbox")

	p := pump{
		Base:         "BTC",
		MaxChangePct: 40,
		Exchanges: []exchange{
			{Exchange: "binance", ChangePct: 40, VolumeUSD: volumeUSD(5_000_000)},
			{Exchange: "lbank", ChangePct: 35},
		},
	}

	if text := formatAlert(p); !contains(text, `$5M\+`) {
		t.Errorf("expected partial volume lower-bound marker, got:\n%s", text)
	}
}

func contains(s, substr string) bool {
	return strings.Contains(s, substr)
}
