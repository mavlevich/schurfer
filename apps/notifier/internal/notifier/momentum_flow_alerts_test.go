package notifier

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"

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
	watchDecisionAt := time.Date(2026, 8, 16, 14, 32, 7, 0, time.UTC)
	entryAt := time.Date(2026, 8, 16, 14, 32, 12, 0, time.UTC)
	return60m, oiGrowth60m, buyImbalance15m := 2.3, 1.8, 0.18
	db := &stubAlertDB{rows: &stubAlertRows{values: [][]any{
		{
			"paper-1", "BTCUSDT", "bybit", 63000.5, 50.0, 3,
			watchDecisionAt, entryAt,
			return60m, oiGrowth60m, buyImbalance15m,
		},
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
		Leverage: 3, WatchDecisionAt: watchDecisionAt, EntryAt: entryAt,
		Return60mPct: &return60m, OIGrowth60mPct: &oiGrowth60m, BuyImbalance15m: &buyImbalance15m,
	}
	if opens[0].PaperID != want.PaperID || opens[0].Leverage != want.Leverage ||
		!opens[0].WatchDecisionAt.Equal(want.WatchDecisionAt) || !opens[0].EntryAt.Equal(want.EntryAt) ||
		*opens[0].Return60mPct != *want.Return60mPct || *opens[0].OIGrowth60mPct != *want.OIGrowth60mPct ||
		*opens[0].BuyImbalance15m != *want.BuyImbalance15m {
		t.Fatalf("open = %#v, want %#v", opens[0], want)
	}
	if !strings.Contains(db.query, "FROM app.momentum_flow_paper_probes") {
		t.Fatalf("unexpected query: %s", db.query)
	}
	if !strings.Contains(db.query, "app.momentum_flow_paper_runs") {
		t.Fatalf("query must join momentum_flow_paper_runs for the triggering contract's own leverage: %s", db.query)
	}
}

// TestPostgresAlertRecorderReadsMomentumFlowPaperOpenWithMissingSignal is a
// regression: an evaluation row can be pruned/archived by the time this
// alert reads it (the LEFT JOIN in the query), so the signal features must
// come back nil rather than crash the scan or fabricate zeros.
func TestPostgresAlertRecorderReadsMomentumFlowPaperOpenWithMissingSignal(t *testing.T) {
	watchDecisionAt := time.Date(2026, 8, 16, 14, 32, 7, 0, time.UTC)
	entryAt := time.Date(2026, 8, 16, 14, 32, 12, 0, time.UTC)
	db := &stubAlertDB{rows: &stubAlertRows{values: [][]any{
		{
			"paper-1", "BTCUSDT", "bybit", 63000.5, 50.0, 1,
			watchDecisionAt, entryAt,
			nil, nil, nil,
		},
	}}}
	recorder := &postgresAlertRecorder{pool: db}

	opens, err := recorder.ReadNewMomentumFlowPaperOpens(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if len(opens) != 1 {
		t.Fatalf("opens = %d, want 1", len(opens))
	}
	if opens[0].Return60mPct != nil || opens[0].OIGrowth60mPct != nil || opens[0].BuyImbalance15m != nil {
		t.Fatalf("want nil signal features when the evaluation row is missing, got %#v", opens[0])
	}
}

func TestPostgresAlertRecorderReadsNewMomentumFlowPaperOutcomes(t *testing.T) {
	entryAt := time.Date(2026, 8, 16, 10, 26, 34, 0, time.UTC)
	exitAt := time.Date(2026, 8, 16, 14, 26, 34, 0, time.UTC)
	db := &stubAlertDB{rows: &stubAlertRows{values: [][]any{
		{"paper-1", "BTCUSDT", "bybit", 240, 1.25, 0.63, entryAt, exitAt},
	}}}
	recorder := &postgresAlertRecorder{pool: db}

	outcomes, err := recorder.ReadNewMomentumFlowPaperOutcomes(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if len(outcomes) != 1 {
		t.Fatalf("outcomes = %d, want 1", len(outcomes))
	}
	got := outcomes[0]
	if got.PaperID != "paper-1" || got.HorizonMinutes != 240 || got.NetReturnPct != 1.25 || got.NetPnLUSD != 0.63 ||
		!got.EntryAt.Equal(entryAt) || got.ExitAt == nil || !got.ExitAt.Equal(exitAt) {
		t.Fatalf("outcome = %#v", got)
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

// TestPostgresAlertRecorderReadsMomentumFlowPaperOutcomeWithUnresolvedExit is
// a regression: exit_at is NULL for exit_unresolved outcomes (a max-hold
// exit that could not get a clean quote before its deadline -- the schema's
// own momentum_flow_paper_exit_shape CHECK requires this), so ExitAt must
// come back nil rather than crash the scan or fabricate a close time.
func TestPostgresAlertRecorderReadsMomentumFlowPaperOutcomeWithUnresolvedExit(t *testing.T) {
	entryAt := time.Date(2026, 8, 16, 10, 26, 34, 0, time.UTC)
	db := &stubAlertDB{rows: &stubAlertRows{values: [][]any{
		{"paper-1", "BTCUSDT", "bybit", 240, -1.0, -0.5, entryAt, nil},
	}}}
	recorder := &postgresAlertRecorder{pool: db}

	outcomes, err := recorder.ReadNewMomentumFlowPaperOutcomes(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if len(outcomes) != 1 {
		t.Fatalf("outcomes = %d, want 1", len(outcomes))
	}
	if outcomes[0].ExitAt != nil {
		t.Fatalf("want nil ExitAt for an unresolved exit, got %v", *outcomes[0].ExitAt)
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

func TestMomentumFlowSizeLine(t *testing.T) {
	if got := momentumFlowSizeLine(50, 1); got != "$50 notional, no leverage" {
		t.Errorf("leverage=1: got %q", got)
	}
	if got := momentumFlowSizeLine(150, 3); got != "$150 notional, 3x leverage ($50 margin)" {
		t.Errorf("leverage=3: got %q", got)
	}
}

func TestMomentumFlowSignalLine(t *testing.T) {
	if got := momentumFlowSignalLine(nil, nil, nil); got != "Signal: unavailable" {
		t.Errorf("all nil: got %q", got)
	}
	r, oi, bi := 2.3, 1.8, 0.18
	got := momentumFlowSignalLine(&r, &oi, &bi)
	want := "Signal: 60m return +2.3% · OI growth 60m +1.8% · buy imbalance 15m +0.18"
	if got != want {
		t.Errorf("got %q, want %q", got, want)
	}
}

func TestMomentumFlowSignalLinePartial(t *testing.T) {
	r := 2.3
	got := momentumFlowSignalLine(&r, nil, nil)
	if got != "Signal: 60m return +2.3%" {
		t.Errorf("got %q", got)
	}
}

func TestMomentumFlowTimingLine(t *testing.T) {
	entry := time.Date(2026, 8, 16, 10, 26, 34, 0, time.UTC)
	exit := time.Date(2026, 8, 16, 14, 26, 34, 0, time.UTC)

	if got := momentumFlowTimingLine(entry, &exit); got != "Opened 10:26:34 UTC → closed 14:26:34 UTC" {
		t.Errorf("closed: got %q", got)
	}
	if got := momentumFlowTimingLine(entry, nil); got != "Opened 10:26:34 UTC" {
		t.Errorf("unresolved: got %q", got)
	}
}

// TestMomentumFlowMessagesHaveNoEmDash is a regression: the previous
// "research probe, not a live position" phrasing used an em-dash ("—"),
// which reads as an AI trace and was flagged for it. Neither message may
// contain one going forward.
func TestMomentumFlowMessagesHaveNoEmDash(t *testing.T) {
	watchDecisionAt := time.Date(2026, 8, 16, 14, 32, 7, 0, time.UTC)
	entryAt := time.Date(2026, 8, 16, 14, 32, 12, 0, time.UTC)
	r, oi, bi := 2.3, 1.8, 0.18
	open := momentumFlowPaperOpen{
		PaperID: "p1", Symbol: "GPSUSDT", Exchange: "bybit", EntryVWAP: 104.82, NotionalUSD: 150,
		Leverage: 3, WatchDecisionAt: watchDecisionAt, EntryAt: entryAt,
		Return60mPct: &r, OIGrowth60mPct: &oi, BuyImbalance15m: &bi,
	}
	openMsg := formatMomentumFlowOpenMessage(open)
	if strings.ContainsRune(openMsg, '—') {
		t.Errorf("open message contains an em-dash: %q", openMsg)
	}
	if !strings.Contains(openMsg, "MOMENTUM-FLOW LONG") {
		t.Errorf("open message must name the strategy explicitly, got %q", openMsg)
	}

	exitAt := entryAt.Add(240 * time.Minute)
	outcome := momentumFlowPaperOutcome{
		PaperID: "p1", Symbol: "GPSUSDT", Exchange: "bybit",
		HorizonMinutes: 240, NetReturnPct: 0.82, NetPnLUSD: 0.41,
		EntryAt: entryAt, ExitAt: &exitAt,
	}
	outcomeMsg := formatMomentumFlowOutcomeMessage(outcome)
	if strings.ContainsRune(outcomeMsg, '—') {
		t.Errorf("outcome message contains an em-dash: %q", outcomeMsg)
	}
	if !strings.Contains(outcomeMsg, "MOMENTUM-FLOW LONG CLOSED") {
		t.Errorf("outcome message must name the strategy explicitly, got %q", outcomeMsg)
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
