package burstengine

import (
	"fmt"
	"testing"
	"time"

	"github.com/mavlevich/schurfer/collector/internal/streamrt"
)

var t0 = time.Date(2026, 10, 5, 12, 0, 0, 0, time.UTC)

func at(minute int, second float64) time.Time {
	return t0.Add(time.Duration(minute)*time.Minute + time.Duration(second*float64(time.Second)))
}

var ids int

func trade(minute int, second float64, price, notional float64) streamrt.Event {
	ids++
	return tradeID(fmt.Sprint(ids), minute, second, price, notional)
}

func tradeID(id string, minute int, second float64, price, notional float64) streamrt.Event {
	when := at(minute, second)
	return streamrt.Event{Trade: &streamrt.Trade{
		Exchange: "bybit", Symbol: "AAAUSDT", TradeID: id, Price: price, Notional: notional,
		EventAt: when, ReceivedAt: when.Add(100 * time.Millisecond),
	}}
}

func lifecycle(kind streamrt.LifecycleKind, minute int, second float64) streamrt.Event {
	return streamrt.Event{Lifecycle: &streamrt.Lifecycle{
		Kind: kind, Symbols: []string{"AAAUSDT"}, At: at(minute, second),
	}}
}

// quiet subscribes in minute -1 and feeds 40 ordinary minutes (turnover 1,000 at 1.0).
func quiet(e *Engine) {
	e.OnEvent(lifecycle(streamrt.Connected, -1, 20))
	e.OnEvent(lifecycle(streamrt.Subscribed, -1, 30))
	for m := range 40 {
		e.OnEvent(trade(m, 10, 1.0, 600))
		e.OnEvent(trade(m, 40, 1.0, 400))
	}
}

func TestABurstFiresAfterTheBarEndPlusGrace(t *testing.T) {
	e := New(HYP030())
	quiet(e)
	e.OnEvent(trade(40, 5, 1.03, 4_000))
	e.OnEvent(trade(40, 59.9, 1.06, 6_000))
	if got := e.Tick(at(41, 0.1)); len(got) != 0 {
		t.Fatalf("fired before the grace ended: %+v", got)
	}
	got := e.Tick(at(41, 0.3))
	if len(got) != 1 {
		t.Fatalf("signals %d stats %+v", len(got), e.Stats)
	}
	s := got[0]
	if !s.BarStart.Equal(at(40, 0)) || s.Median != 1_000 || s.Turnover != 10_000 ||
		s.Contract != ContractVersion || s.LastTradeLag != 0 {
		t.Fatalf("%+v", s)
	}
}

func TestATradeWithinGraceStillCountsAfterTheNextMinuteStarted(t *testing.T) {
	e := New(HYP030())
	quiet(e)
	e.OnEvent(trade(40, 30, 1.03, 4_000))
	e.OnEvent(trade(41, 0.05, 1.03, 100))    // the next minute arrives first
	e.OnEvent(trade(40, 59.95, 1.06, 6_000)) // minute 40, inside the grace
	if e.Stats.LateTrades != 0 {
		t.Fatalf("a trade inside the grace was dropped: %+v", e.Stats)
	}
	if got := e.Tick(at(41, 0.3)); len(got) != 1 || got[0].Turnover != 10_000 {
		t.Fatalf("signals %+v", got)
	}
	e.OnEvent(trade(40, 59.99, 9.0, 1e9)) // after finalization: late, never reopens
	if e.Stats.LateTrades != 1 || len(e.symbols["AAAUSDT"].open) != 1 {
		t.Fatalf("stats %+v open %d", e.Stats, len(e.symbols["AAAUSDT"].open))
	}
}

func TestMissedMinutesOfADisconnectAreIncomplete(t *testing.T) {
	e := New(HYP030())
	quiet(e)
	e.OnEvent(streamrt.Event{Lifecycle: &streamrt.Lifecycle{
		Kind: streamrt.Disconnected, Symbols: []string{"AAAUSDT"},
		At: at(43, 0), LastFrameAt: at(40, 1), // detected late; the gap starts at 40:01
	}})
	e.OnEvent(lifecycle(streamrt.Connected, 43, 5))
	e.OnEvent(lifecycle(streamrt.Subscribed, 43, 10))
	e.OnEvent(trade(44, 30, 1.0, 1_000))
	e.Tick(at(45, 1))
	s := e.symbols["AAAUSDT"]
	for _, bar := range s.history[len(s.history)-5:] {
		minute := int(bar.Start.Sub(t0) / time.Minute)
		want := minute >= 44 // 40..43 overlap the gap
		if bar.Complete != want {
			t.Fatalf("minute %d complete=%v", minute, bar.Complete)
		}
	}
}

func TestAnOverflowSpanningAPendingBarSuppressesItsSignal(t *testing.T) {
	e := New(HYP030())
	quiet(e)
	e.OnEvent(trade(40, 30, 1.06, 10_000))
	e.OnEvent(trade(41, 0.05, 1.06, 100)) // minute 40 is now pending, not current
	e.OnEvent(streamrt.Event{Lifecycle: &streamrt.Lifecycle{
		Kind: streamrt.Overflow, Symbols: []string{"AAAUSDT"},
		At: at(41, 0.1), Since: at(40, 50), Dropped: 3,
	}})
	if got := e.Tick(at(41, 0.3)); len(got) != 0 {
		t.Fatalf("fired over dropped data: %+v", got)
	}
	if e.Stats.SuppressedByGap != 1 {
		t.Fatalf("stats %+v", e.Stats)
	}
}

func TestARepeatedTradeIDNeverCountsTwice(t *testing.T) {
	e := New(HYP030())
	quiet(e)
	e.OnEvent(tradeID("burst", 40, 30, 1.06, 3_000))
	e.OnEvent(tradeID("burst", 40, 30, 1.06, 3_000)) // a retransmit would make 6,000
	if got := e.Tick(at(41, 1)); len(got) != 0 {
		t.Fatalf("a duplicate crossed the turnover threshold: %+v", got)
	}
	if e.Stats.Duplicates != 1 {
		t.Fatalf("stats %+v", e.Stats)
	}
	e.OnEvent(tradeID("", 41, 30, 1.0, 1))
	if e.Stats.MissingID != 1 {
		t.Fatalf("a trade without an id must be refused: %+v", e.Stats)
	}
}

func TestCloseFollowsExchangeTimeNotArrival(t *testing.T) {
	e := New(HYP030())
	quiet(e)
	e.OnEvent(trade(40, 50, 1.00, 5_000))
	e.OnEvent(trade(40, 10, 1.06, 5_000)) // earlier by exchange time, arrives later
	if got := e.Tick(at(41, 1)); len(got) != 0 {
		t.Fatalf("an earlier trade became the close: %+v", got)
	}
	bar := e.symbols["AAAUSDT"].history[len(e.symbols["AAAUSDT"].history)-1]
	if bar.Open != 1.06 || bar.Close != 1.00 {
		t.Fatalf("open %v close %v", bar.Open, bar.Close)
	}
}

func TestNothingIsCompleteBeforeTheFirstSubscribed(t *testing.T) {
	e := New(HYP030())
	e.OnEvent(lifecycle(streamrt.Connected, -1, 20))
	for m := range 40 {
		e.OnEvent(trade(m, 10, 1.0, 1_000))
	}
	e.OnEvent(trade(40, 30, 1.06, 10_000))
	if got := e.Tick(at(41, 1)); len(got) != 0 {
		t.Fatalf("fired without any acknowledged subscription: %+v", got)
	}
	if e.Stats.IncompleteBars != e.Stats.Bars {
		t.Fatalf("stats %+v", e.Stats)
	}
}

func TestEmptyMinutesEnterTheMedianAsZeroTurnover(t *testing.T) {
	e := New(HYP030())
	e.OnEvent(lifecycle(streamrt.Subscribed, -1, 30))
	for m := 0; m < 40; m += 2 {
		e.OnEvent(trade(m, 10, 1.0, 1_000))
	}
	e.OnEvent(trade(40, 30, 1.06, 2_000))
	e.Tick(at(41, 1))
	if e.Stats.EmptyBars == 0 || e.Stats.Signals != 0 {
		t.Fatalf("stats %+v", e.Stats)
	}
}

func TestTooFewCompleteBarsAndTheCooldownBlock(t *testing.T) {
	e := New(HYP030())
	e.OnEvent(lifecycle(streamrt.Subscribed, -1, 30))
	for m := range 20 {
		e.OnEvent(trade(m, 10, 1.0, 1_000))
	}
	e.OnEvent(trade(20, 30, 1.06, 10_000))
	if got := e.Tick(at(21, 1)); len(got) != 0 {
		t.Fatal("fired with only 20 bars in the median window")
	}
	e2 := New(HYP030())
	quiet(e2)
	e2.OnEvent(trade(40, 30, 1.06, 10_000))
	e2.OnEvent(trade(41, 30, 1.06, 100))
	e2.OnEvent(trade(42, 30, 1.12, 50_000))
	if got := e2.Tick(at(43, 1)); len(got) != 1 {
		t.Fatalf("the cooldown must keep one firing per hour: %d", len(got))
	}
}
