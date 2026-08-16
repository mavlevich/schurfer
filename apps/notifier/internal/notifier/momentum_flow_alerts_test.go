package notifier

import (
	"context"
	"errors"
	"strings"
	"testing"

	"github.com/alicebob/miniredis/v2"
)

type stubMomentumFlowReader struct {
	opens       []momentumFlowPaperOpen
	outcomes    []momentumFlowPaperOutcome
	opensErr    error
	outcomesErr error
}

func (r stubMomentumFlowReader) ReadNewMomentumFlowPaperOpens(
	_ context.Context,
) ([]momentumFlowPaperOpen, error) {
	return r.opens, r.opensErr
}

func (r stubMomentumFlowReader) ReadNewMomentumFlowPaperOutcomes(
	_ context.Context,
) ([]momentumFlowPaperOutcome, error) {
	return r.outcomes, r.outcomesErr
}

func TestPostgresAlertRecorderReadsNewMomentumFlowPaperOpens(t *testing.T) {
	db := &stubAlertDB{rows: &stubAlertRows{values: [][]any{
		{"paper-1", "BTCUSDT", "bybit", 63000.5, 50.0},
	}}}
	recorder := &postgresAlertRecorder{pool: db}

	opens, err := recorder.ReadNewMomentumFlowPaperOpens(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if len(opens) != 1 {
		t.Fatalf("opens = %d, want 1", len(opens))
	}
	want := momentumFlowPaperOpen{
		PaperID: "paper-1", Symbol: "BTCUSDT", Exchange: "bybit", EntryVWAP: 63000.5, NotionalUSD: 50.0,
	}
	if opens[0] != want {
		t.Fatalf("open = %#v, want %#v", opens[0], want)
	}
	if !strings.Contains(db.query, "FROM app.momentum_flow_paper_probes") {
		t.Fatalf("unexpected query: %s", db.query)
	}
}

func TestPostgresAlertRecorderReadsNewMomentumFlowPaperOutcomes(t *testing.T) {
	db := &stubAlertDB{rows: &stubAlertRows{values: [][]any{
		{"paper-1", "BTCUSDT", "bybit", 240, 1.25, 0.63},
	}}}
	recorder := &postgresAlertRecorder{pool: db}

	outcomes, err := recorder.ReadNewMomentumFlowPaperOutcomes(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if len(outcomes) != 1 {
		t.Fatalf("outcomes = %d, want 1", len(outcomes))
	}
	want := momentumFlowPaperOutcome{
		PaperID: "paper-1", Symbol: "BTCUSDT", Exchange: "bybit",
		HorizonMinutes: 240, NetReturnPct: 1.25, NetPnLUSD: 0.63,
	}
	if outcomes[0] != want {
		t.Fatalf("outcome = %#v, want %#v", outcomes[0], want)
	}
	if !strings.Contains(db.query, "FROM app.momentum_flow_paper_outcomes") {
		t.Fatalf("unexpected query: %s", db.query)
	}
	// Regression: must select each probe's own FINAL (max) horizon only,
	// not every intermediate 5/15/30/60/120-minute row too.
	if !strings.Contains(db.query, "max(o2.horizon_minutes)") {
		t.Fatalf("query does not restrict to each probe's own final horizon: %s", db.query)
	}
}

func TestPostgresAlertRecorderPropagatesMomentumFlowQueryError(t *testing.T) {
	want := errors.New("db down")
	recorder := &postgresAlertRecorder{pool: &stubAlertDB{queryErr: want}}

	if _, err := recorder.ReadNewMomentumFlowPaperOpens(context.Background()); !errors.Is(err, want) {
		t.Fatalf("opens error = %v, want %v", err, want)
	}
	if _, err := recorder.ReadNewMomentumFlowPaperOutcomes(context.Background()); !errors.Is(err, want) {
		t.Fatalf("outcomes error = %v, want %v", err, want)
	}
}

func TestTick_MomentumFlowPaperOpenAlertsOncePerProbe(t *testing.T) {
	calls, done := newTelegramCounter(t)
	defer done()

	mr := miniredis.RunT(t)
	n := newTestNotifier(t, mr, "tok", "cid")
	n.momentumFlow = stubMomentumFlowReader{
		opens: []momentumFlowPaperOpen{
			{PaperID: "p1", Symbol: "BTCUSDT", Exchange: "bybit", EntryVWAP: 63000, NotionalUSD: 50},
		},
	}
	setPumpsPayload(t, mr, payload{Scanned: []string{"binance"}})

	_ = n.tick(context.Background())
	_ = n.tick(context.Background()) // same probe id again, must not re-alert

	if *calls != 1 {
		t.Fatalf("momentum flow open alerts = %d, want 1 (deduped across ticks)", *calls)
	}
	if !mr.Exists(redisKeyMomentumFlowOpenSeenPfx + "p1") {
		t.Fatal("open de-dup key missing")
	}
}

func TestTick_MomentumFlowPaperOutcomeAlertsOncePerProbe(t *testing.T) {
	calls, done := newTelegramCounter(t)
	defer done()

	mr := miniredis.RunT(t)
	n := newTestNotifier(t, mr, "tok", "cid")
	n.momentumFlow = stubMomentumFlowReader{
		outcomes: []momentumFlowPaperOutcome{
			{
				PaperID: "p1", Symbol: "BTCUSDT", Exchange: "bybit",
				HorizonMinutes: 240, NetReturnPct: 1.25, NetPnLUSD: 0.63,
			},
		},
	}
	setPumpsPayload(t, mr, payload{Scanned: []string{"binance"}})

	_ = n.tick(context.Background())
	_ = n.tick(context.Background())

	if *calls != 1 {
		t.Fatalf("momentum flow outcome alerts = %d, want 1 (deduped across ticks)", *calls)
	}
	if !mr.Exists(redisKeyMomentumFlowOutcomeSeenPfx + "p1") {
		t.Fatal("outcome de-dup key missing")
	}
}

func TestTick_MomentumFlowOpenAlertFailureReleasesClaim(t *testing.T) {
	defer failingTelegram(t)()

	mr := miniredis.RunT(t)
	n := newTestNotifier(t, mr, "tok", "cid")
	n.momentumFlow = stubMomentumFlowReader{
		opens: []momentumFlowPaperOpen{
			{PaperID: "p1", Symbol: "BTCUSDT", Exchange: "bybit", EntryVWAP: 63000, NotionalUSD: 50},
		},
	}
	setPumpsPayload(t, mr, payload{Scanned: []string{"binance"}})

	_ = n.tick(context.Background()) // send fails

	if mr.Exists(redisKeyMomentumFlowOpenSeenPfx + "p1") {
		t.Error("claim must be released when the alert fails to send, so the next tick retries")
	}
}

func TestTick_MomentumFlowOutcomeAlertFailureReleasesClaim(t *testing.T) {
	defer failingTelegram(t)()

	mr := miniredis.RunT(t)
	n := newTestNotifier(t, mr, "tok", "cid")
	n.momentumFlow = stubMomentumFlowReader{
		outcomes: []momentumFlowPaperOutcome{
			{
				PaperID: "p1", Symbol: "BTCUSDT", Exchange: "bybit",
				HorizonMinutes: 240, NetReturnPct: -2.0, NetPnLUSD: -1.0,
			},
		},
	}
	setPumpsPayload(t, mr, payload{Scanned: []string{"binance"}})

	_ = n.tick(context.Background())

	if mr.Exists(redisKeyMomentumFlowOutcomeSeenPfx + "p1") {
		t.Error("claim must be released when the alert fails to send, so the next tick retries")
	}
}

func TestFormatSignedUSD(t *testing.T) {
	cases := map[float64]string{
		3.456: "+$3.46",
		-1.88: "-$1.88",
		0:     "+$0.00",
	}
	for amount, want := range cases {
		if got := formatSignedUSD(amount); got != want {
			t.Errorf("formatSignedUSD(%v) = %q, want %q", amount, got, want)
		}
	}
}

func TestFormatPrice(t *testing.T) {
	cases := map[float64]string{
		63451.2345: "63451.2345",
		0.0004881:  "0.00048810",
	}
	for price, want := range cases {
		if got := formatPrice(price); got != want {
			t.Errorf("formatPrice(%v) = %q, want %q", price, got, want)
		}
	}
}

func TestTick_MomentumFlowOutcomesStillProcessedWhenOpensReadFails(t *testing.T) {
	calls, done := newTelegramCounter(t)
	defer done()

	mr := miniredis.RunT(t)
	n := newTestNotifier(t, mr, "tok", "cid")
	n.momentumFlow = stubMomentumFlowReader{
		opensErr: errors.New("opens query failed"),
		outcomes: []momentumFlowPaperOutcome{
			{
				PaperID: "p1", Symbol: "BTCUSDT", Exchange: "bybit",
				HorizonMinutes: 240, NetReturnPct: 1.25, NetPnLUSD: 0.63,
			},
		},
	}
	setPumpsPayload(t, mr, payload{Scanned: []string{"binance"}})

	_ = n.tick(context.Background())

	if *calls != 1 {
		t.Fatalf(
			"outcome alert = %d, want 1: a failed opens read must not block the independent outcomes read",
			*calls,
		)
	}
}
