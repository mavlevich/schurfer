package pumps

import (
	"net/url"
	"strings"
	"testing"
)

func TestGateOHLCVURLPercentEncodesUnicodeContract(t *testing.T) {
	t.Parallel()
	rawURL := gateOHLCVURL("草根文化", 15, 192)
	if strings.Contains(rawURL, "草根文化") {
		t.Fatalf("URL contains unescaped Unicode: %s", rawURL)
	}
	parsed, err := url.Parse(rawURL)
	if err != nil {
		t.Fatal(err)
	}
	query := parsed.Query()
	if got := query.Get("contract"); got != "草根文化_USDT" {
		t.Errorf("contract = %q, want 草根文化_USDT", got)
	}
	if got := query.Get("interval"); got != "15m" {
		t.Errorf("interval = %q, want 15m", got)
	}
	if got := query.Get("limit"); got != "192" {
		t.Errorf("limit = %q, want 192", got)
	}
}

func TestParseBybit(t *testing.T) {
	cases := []struct {
		name    string
		raw     string
		wantErr bool
		wantLen int
	}{
		{
			name:    "happy path",
			raw:     `{"retCode":0,"retMsg":"OK","result":{"list":[["1700000120000","42100","42200","42000","42150","110"],["1700000060000","42000","42100","41900","42050","100"],["1700000000000","41900","42000","41800","41950","90"]]}}`,
			wantLen: 3,
		},
		{
			name:    "exchange error",
			raw:     `{"retCode":10001,"retMsg":"symbol not found","result":{"list":[]}}`,
			wantErr: true,
		},
		{
			name:    "malformed json",
			raw:     `{not valid json`,
			wantErr: true,
		},
		{
			name:    "short row",
			raw:     `{"retCode":0,"retMsg":"OK","result":{"list":[["1700000000000","42000"]]}}`,
			wantErr: true,
		},
		{
			name:    "bad price",
			raw:     `{"retCode":0,"retMsg":"OK","result":{"list":[["1700000000000","not-a-number","42100","41900","42050","100"]]}}`,
			wantErr: true,
		},
		{
			name:    "bad timestamp",
			raw:     `{"retCode":0,"retMsg":"OK","result":{"list":[["not-a-ts","42000","42100","41900","42050","100"]]}}`,
			wantErr: true,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			candles, err := parseBybit([]byte(tc.raw))
			if tc.wantErr && err == nil {
				t.Fatal("expected error, got nil")
			}
			if !tc.wantErr && err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if !tc.wantErr && len(candles) != tc.wantLen {
				t.Fatalf("want %d candles, got %d", tc.wantLen, len(candles))
			}
		})
	}
}

func TestParseBybitChronologicalOrder(t *testing.T) {
	// Bybit returns newest first; parseBybit must reverse to oldest-first.
	raw := `{"retCode":0,"retMsg":"OK","result":{"list":[["1700000120000","42100","42200","42000","42150","110"],["1700000060000","42000","42100","41900","42050","100"],["1700000000000","41900","42000","41800","41950","90"]]}}`
	candles, err := parseBybit([]byte(raw))
	if err != nil {
		t.Fatal(err)
	}
	if len(candles) != 3 {
		t.Fatalf("want 3 candles, got %d", len(candles))
	}
	if candles[0].Time >= candles[1].Time || candles[1].Time >= candles[2].Time {
		t.Fatalf("candles not in chronological order: %v", []int64{candles[0].Time, candles[1].Time, candles[2].Time})
	}
}

func TestParseBinance(t *testing.T) {
	cases := []struct {
		name    string
		raw     string
		wantErr bool
		wantLen int
	}{
		{
			name:    "happy path",
			raw:     `[[1700000000000,"41900","42000","41800","41950","90",1700003599999,"3800000",90,"45","1900000","0"],[1700003600000,"42000","42100","41900","42050","100",1700007199999,"4200000",100,"50","2100000","0"]]`,
			wantLen: 2,
		},
		{
			name:    "exchange error",
			raw:     `{"code":-1121,"msg":"Invalid symbol."}`,
			wantErr: true,
		},
		{
			name:    "malformed json",
			raw:     `{not valid`,
			wantErr: true,
		},
		{
			name:    "short row",
			raw:     `[[1700000000000,"41900"]]`,
			wantErr: true,
		},
		{
			name:    "bad open price",
			raw:     `[[1700000000000,"not-a-price","42000","41800","41950","90",1700003599999,"3800000",90,"45","1900000","0"]]`,
			wantErr: true,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			candles, err := parseBinance([]byte(tc.raw))
			if tc.wantErr && err == nil {
				t.Fatal("expected error, got nil")
			}
			if !tc.wantErr && err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if !tc.wantErr && len(candles) != tc.wantLen {
				t.Fatalf("want %d candles, got %d", tc.wantLen, len(candles))
			}
		})
	}
}

func TestParseOKX(t *testing.T) {
	cases := []struct {
		name    string
		raw     string
		wantErr bool
		wantLen int
	}{
		{
			name:    "happy path",
			raw:     `{"code":"0","msg":"","data":[["1700000120000","42100","42200","42000","42150","110","4620000",""],["1700000060000","42000","42100","41900","42050","100","4200000",""]]}`,
			wantLen: 2,
		},
		{
			name:    "exchange error",
			raw:     `{"code":"51001","msg":"Instrument ID does not exist","data":[]}`,
			wantErr: true,
		},
		{
			name:    "malformed json",
			raw:     `not json`,
			wantErr: true,
		},
		{
			name:    "short row",
			raw:     `{"code":"0","msg":"","data":[["1700000000000","42100"]]}`,
			wantErr: true,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			candles, err := parseOKX([]byte(tc.raw))
			if tc.wantErr && err == nil {
				t.Fatal("expected error, got nil")
			}
			if !tc.wantErr && err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if !tc.wantErr && len(candles) != tc.wantLen {
				t.Fatalf("want %d candles, got %d", tc.wantLen, len(candles))
			}
		})
	}
}

func TestParseOKXChronologicalOrder(t *testing.T) {
	raw := `{"code":"0","msg":"","data":[["1700000120000","42100","42200","42000","42150","110","0",""],["1700000060000","42000","42100","41900","42050","100","0",""],["1700000000000","41900","42000","41800","41950","90","0",""]]}`
	candles, err := parseOKX([]byte(raw))
	if err != nil {
		t.Fatal(err)
	}
	if candles[0].Time >= candles[1].Time || candles[1].Time >= candles[2].Time {
		t.Fatal("candles not in chronological order")
	}
}

func TestReverseCandles(t *testing.T) {
	candles := []Candle{{Time: 3}, {Time: 2}, {Time: 1}}
	reverseCandles(candles)
	if candles[0].Time != 1 || candles[1].Time != 2 || candles[2].Time != 3 {
		t.Fatalf("want [1 2 3], got [%d %d %d]", candles[0].Time, candles[1].Time, candles[2].Time)
	}
}

func TestParseRowShortRow(t *testing.T) {
	_, err := parseRow([]string{"123", "1.0"}, 0, 1, 2, 3, 4, 5)
	if err == nil {
		t.Fatal("expected error for short row")
	}
}

func TestGateInterval(t *testing.T) {
	cases := []struct {
		minutes int
		want    string
	}{
		{5, "5m"},
		{15, "15m"},
		{30, "30m"},
		{60, "1h"},
		{240, "4h"},
		{480, "8h"},
		{720, "12h"},
		{1440, "1d"},
	}
	for _, tc := range cases {
		if got := gateInterval(tc.minutes); got != tc.want {
			t.Errorf("gateInterval(%d) = %q, want %q", tc.minutes, got, tc.want)
		}
	}
}

func TestParseGate(t *testing.T) {
	cases := []struct {
		name    string
		raw     string
		wantErr bool
		wantLen int
	}{
		{
			name:    "happy path",
			raw:     `[{"t":1700000000,"o":"41900","h":"42000","l":"41800","c":"41950","v":90},{"t":1700003600,"o":"42000","h":"42100","l":"41900","c":"42050","v":100}]`,
			wantLen: 2,
		},
		{
			name:    "malformed json",
			raw:     `not json`,
			wantErr: true,
		},
		{
			name:    "bad open price",
			raw:     `[{"t":1700000000,"o":"not-a-price","h":"42000","l":"41800","c":"41950","v":90}]`,
			wantErr: true,
		},
		{
			name:    "empty array",
			raw:     `[]`,
			wantLen: 0,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			candles, err := parseGate([]byte(tc.raw))
			if tc.wantErr && err == nil {
				t.Fatal("expected error, got nil")
			}
			if !tc.wantErr && err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if !tc.wantErr && len(candles) != tc.wantLen {
				t.Fatalf("want %d candles, got %d", tc.wantLen, len(candles))
			}
		})
	}
}

func TestParseGateChronologicalOrder(t *testing.T) {
	// Gate returns oldest-first — no reversal needed, verify order is preserved.
	raw := `[{"t":1700000000,"o":"41900","h":"42000","l":"41800","c":"41950","v":90},{"t":1700003600,"o":"42000","h":"42100","l":"41900","c":"42050","v":100},{"t":1700007200,"o":"42050","h":"42200","l":"42000","c":"42150","v":110}]`
	candles, err := parseGate([]byte(raw))
	if err != nil {
		t.Fatal(err)
	}
	if len(candles) != 3 {
		t.Fatalf("want 3 candles, got %d", len(candles))
	}
	if candles[0].Time >= candles[1].Time || candles[1].Time >= candles[2].Time {
		t.Fatal("candles not in chronological order")
	}
}
