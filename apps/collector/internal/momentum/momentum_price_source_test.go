package momentum

import (
	"testing"
	"time"
)

func tradeAt(price float64, when when, id string) Trade {
	return Trade{
		Symbol:     "AKEUSDT",
		Side:       SideBuy,
		Price:      price,
		Size:       1,
		EventAt:    when.eventAt,
		ReceivedAt: when.receivedAt,
		TradeID:    id,
	}
}

// when lets a test set EventAt and ReceivedAt independently -- the
// out-of-order tests below need trades to ARRIVE (ReceivedAt) in a
// different order than their own EventAt, which the package's existing
// trade() helper (EventAt == ReceivedAt always) cannot express.
type when struct{ eventAt, receivedAt time.Time }

func at2(offsetSeconds int) when {
	t := at(offsetSeconds)
	return when{eventAt: t, receivedAt: t}
}

func TestNewDefaultsToTickerLastAndNewWithPriceSourceIsExplicit(t *testing.T) {
	tickerEngine := New()
	if _, err := tickerEngine.AddTrade(tradeAt(100, at2(0), "id1")); err != nil {
		t.Fatal(err)
	}
	closed := tickerEngine.Flush(at(61))
	if len(closed) != 1 {
		t.Fatalf("got %d closed bars, want 1", len(closed))
	}
	if closed[0].PriceSource != PriceSourceTickerLast {
		t.Fatalf("PriceSource = %q, want %q", closed[0].PriceSource, PriceSourceTickerLast)
	}
	// New()'s own default: a trade alone never moves price for a
	// ticker-sourced engine.
	if closed[0].OpenPrice != nil || closed[0].ClosePrice != nil {
		t.Fatalf("OpenPrice/ClosePrice = %v/%v, want nil (ticker-sourced engine, trade-only activity)",
			closed[0].OpenPrice, closed[0].ClosePrice)
	}

	tradeEngine := NewWithPriceSource(PriceSourceAggregateTrade)
	if tradeEngine == nil {
		t.Fatal("NewWithPriceSource returned nil")
	}
}

func TestAggregateTradePriceSourceBuildsOHLCFromAcceptedTrades(t *testing.T) {
	e := NewWithPriceSource(PriceSourceAggregateTrade)
	prices := []float64{100, 105, 95, 102}
	for i, price := range prices {
		if _, err := e.AddTrade(tradeAt(price, at2(i), "id"+string(rune('a'+i)))); err != nil {
			t.Fatal(err)
		}
	}
	closed := e.Flush(at(61))
	if len(closed) != 1 {
		t.Fatalf("got %d closed bars, want 1", len(closed))
	}
	bar := closed[0]
	if bar.PriceSource != PriceSourceAggregateTrade {
		t.Fatalf("PriceSource = %q, want %q", bar.PriceSource, PriceSourceAggregateTrade)
	}
	if !bar.PriceObservedThisMinute {
		t.Fatal("PriceObservedThisMinute = false, want true")
	}
	if bar.OpenPrice == nil || *bar.OpenPrice != 100 {
		t.Fatalf("OpenPrice = %v, want 100 (first accepted EventAt)", bar.OpenPrice)
	}
	if bar.ClosePrice == nil || *bar.ClosePrice != 102 {
		t.Fatalf("ClosePrice = %v, want 102 (last accepted EventAt)", bar.ClosePrice)
	}
	if bar.HighPrice == nil || *bar.HighPrice != 105 {
		t.Fatalf("HighPrice = %v, want 105", bar.HighPrice)
	}
	if bar.LowPrice == nil || *bar.LowPrice != 95 {
		t.Fatalf("LowPrice = %v, want 95", bar.LowPrice)
	}
}

func TestAggregateTradeOpenAndCloseUseEventAtNotArrivalOrder(t *testing.T) {
	e := NewWithPriceSource(PriceSourceAggregateTrade)
	// Arrival order: price 200 (EventAt +30s) arrives FIRST, then price 100
	// (EventAt +5s) arrives SECOND -- a real out-of-order delivery case
	// (network reordering, reconnect replay). Open must still reflect the
	// EARLIEST EventAt's price (100), not whichever trade the engine saw
	// first (200).
	if _, err := e.AddTrade(tradeAt(200, at2(30), "later-event-arrives-first")); err != nil {
		t.Fatal(err)
	}
	if _, err := e.AddTrade(tradeAt(100, at2(5), "earlier-event-arrives-second")); err != nil {
		t.Fatal(err)
	}
	closed := e.Flush(at(61))
	bar := closed[0]
	if bar.OpenPrice == nil || *bar.OpenPrice != 100 {
		t.Fatalf("OpenPrice = %v, want 100 (earliest EventAt, despite arriving second)", bar.OpenPrice)
	}
	if bar.ClosePrice == nil || *bar.ClosePrice != 200 {
		t.Fatalf("ClosePrice = %v, want 200 (latest EventAt, despite arriving first)", bar.ClosePrice)
	}
	// High/Low are unaffected by arrival or event order -- plain min/max.
	if bar.HighPrice == nil || *bar.HighPrice != 200 {
		t.Fatalf("HighPrice = %v, want 200", bar.HighPrice)
	}
	if bar.LowPrice == nil || *bar.LowPrice != 100 {
		t.Fatalf("LowPrice = %v, want 100", bar.LowPrice)
	}
}

func TestAggregateTradeLateTradeDoesNotChangeAlreadyClosedBarOHLC(t *testing.T) {
	e := NewWithPriceSource(PriceSourceAggregateTrade)
	if _, err := e.AddTrade(tradeAt(100, at2(0), "first-minute")); err != nil {
		t.Fatal(err)
	}
	closed, err := e.AddTrade(tradeAt(150, at2(65), "second-minute-opens"))
	if err != nil {
		t.Fatal(err)
	}
	if len(closed) != 1 {
		t.Fatalf("got %d closed bars opening the second minute, want 1", len(closed))
	}
	firstBar := closed[0]
	if firstBar.ClosePrice == nil || *firstBar.ClosePrice != 100 {
		t.Fatalf("first bar ClosePrice = %v, want 100", firstBar.ClosePrice)
	}
	// A trade whose EventAt belongs to the now-closed first minute arrives
	// late -- existing AddTrade lateDropped path must reject it before
	// price logic ever runs; the already-closed bar's own OHLC (returned
	// above) must not be retroactively touched, and the CURRENT (second
	// minute's) bar must not see this price either.
	lateClosed, err := e.AddTrade(tradeAt(999, at2(10), "late-arrival"))
	if err != nil {
		t.Fatal(err)
	}
	if len(lateClosed) != 0 {
		t.Fatalf("late trade produced %d closed bars, want 0", len(lateClosed))
	}
	finalClosed := e.Flush(at(130))
	if len(finalClosed) != 1 {
		t.Fatalf("got %d bars flushing the second minute, want 1", len(finalClosed))
	}
	secondBar := finalClosed[0]
	if secondBar.OpenPrice == nil || *secondBar.OpenPrice != 150 {
		t.Fatalf("second bar OpenPrice = %v, want 150 (late trade's price=999 must not appear)", secondBar.OpenPrice)
	}
	if secondBar.LateTradesDropped != 1 {
		t.Fatalf("LateTradesDropped = %d, want 1", secondBar.LateTradesDropped)
	}
}

func TestAggregateTradeDuplicateTradeIDDoesNotDoubleCountPrice(t *testing.T) {
	e := NewWithPriceSource(PriceSourceAggregateTrade)
	dup := tradeAt(100, at2(0), "dup-id")
	if _, err := e.AddTrade(dup); err != nil {
		t.Fatal(err)
	}
	// Same TradeID, different price -- if dedup did not run before the
	// price update, this would corrupt High/Close to 500.
	resend := tradeAt(500, at2(1), "dup-id")
	if _, err := e.AddTrade(resend); err != nil {
		t.Fatal(err)
	}
	closed := e.Flush(at(61))
	bar := closed[0]
	if bar.DuplicateTradesDropped != 1 {
		t.Fatalf("DuplicateTradesDropped = %d, want 1", bar.DuplicateTradesDropped)
	}
	if bar.ClosePrice == nil || *bar.ClosePrice != 100 {
		t.Fatalf("ClosePrice = %v, want 100 (the duplicate resend must never reach price logic)", bar.ClosePrice)
	}
	if bar.HighPrice == nil || *bar.HighPrice != 100 {
		t.Fatalf("HighPrice = %v, want 100", bar.HighPrice)
	}
}

func TestAddTickerObservationMirrorsCanonicalPriceFields(t *testing.T) {
	e := New()
	obs := tickerAt(at(0))
	obs.LastPrice = f(42.5)
	if _, err := e.AddTickerObservation(obs); err != nil {
		t.Fatal(err)
	}
	closed := e.Flush(at(61))
	bar := closed[0]
	if !bar.PriceObservedThisMinute {
		t.Fatal("PriceObservedThisMinute = false, want true")
	}
	if bar.FirstPriceEventAt == nil || !bar.FirstPriceEventAt.Equal(at(0)) {
		t.Fatalf("FirstPriceEventAt = %v, want %v", bar.FirstPriceEventAt, at(0))
	}
	if bar.LastPriceEventAt == nil || !bar.LastPriceEventAt.Equal(at(0)) {
		t.Fatalf("LastPriceEventAt = %v, want %v", bar.LastPriceEventAt, at(0))
	}
	if bar.FirstPriceReceivedAt == nil || bar.LastPriceReceivedAt == nil {
		t.Fatal("FirstPriceReceivedAt/LastPriceReceivedAt not set")
	}
	// Mirrors the pre-existing Ticker* fields exactly -- same values, not
	// a different computation.
	if !bar.FirstPriceEventAt.Equal(*bar.FirstTickerEventAt) {
		t.Fatalf("FirstPriceEventAt (%v) != FirstTickerEventAt (%v)", bar.FirstPriceEventAt, bar.FirstTickerEventAt)
	}
}

func TestOpenInterestCompleteMirrorsTickerComplete(t *testing.T) {
	e := NewWithPriceSource(PriceSourceAggregateTrade)
	if _, err := e.AddTrade(tradeAt(100, at2(0), "id")); err != nil {
		t.Fatal(err)
	}
	closed := e.Flush(at(61))
	bar := closed[0]
	// No AddTickerObservation call at all in this bar's window (Binance's
	// own shape: OI never arrived this minute) -- TickerComplete/
	// OpenInterestComplete must agree, both reflecting "never healthy".
	if bar.OpenInterestComplete != bar.TickerComplete {
		t.Fatalf("OpenInterestComplete (%v) != TickerComplete (%v)", bar.OpenInterestComplete, bar.TickerComplete)
	}
}

func TestPriceCompleteMirrorsTheFeedThisEnginesPriceSourceActuallyUses(t *testing.T) {
	tickerEngine := New()
	if _, err := tickerEngine.AddTickerObservation(tickerAt(at(0))); err != nil {
		t.Fatal(err)
	}
	tickerBar := tickerEngine.Flush(at(61))[0]
	if tickerBar.PriceComplete != tickerBar.TickerComplete {
		t.Fatalf("ticker-sourced PriceComplete (%v) != TickerComplete (%v)", tickerBar.PriceComplete, tickerBar.TickerComplete)
	}

	tradeEngine := NewWithPriceSource(PriceSourceAggregateTrade)
	if _, err := tradeEngine.AddTrade(tradeAt(100, at2(0), "id")); err != nil {
		t.Fatal(err)
	}
	tradeBar := tradeEngine.Flush(at(61))[0]
	if tradeBar.PriceComplete != tradeBar.TradesComplete {
		t.Fatalf("trade-sourced PriceComplete (%v) != TradesComplete (%v)", tradeBar.PriceComplete, tradeBar.TradesComplete)
	}
}
