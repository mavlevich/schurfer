package burstengine

import (
	"testing"
	"time"

	"github.com/mavlevich/schurfer/collector/internal/streamrt"
)

var t0 = time.Date(2026, 10, 5, 12, 0, 0, 0, time.UTC)

func trade(minute int, second float64, price, notional float64) streamrt.Event {
	at := t0.Add(time.Duration(minute)*time.Minute + time.Duration(second*float64(time.Second)))
	return streamrt.Event{Trade: &streamrt.Trade{
		Exchange: "bybit", Symbol: "AAAUSDT", Price: price, Notional: notional,
		EventAt: at, ReceivedAt: at.Add(100 * time.Millisecond),
	}}
}

func lifecycle(kind streamrt.LifecycleKind, minute int, second float64) streamrt.Event {
	at := t0.Add(time.Duration(minute)*time.Minute + time.Duration(second*float64(time.Second)))
	return streamrt.Event{Lifecycle: &streamrt.Lifecycle{Kind: kind, Symbols: []string{"AAAUSDT"}, At: at}}
}

// quiet feeds 40 ordinary minutes (turnover 1,000 at price 1.0) starting at minute 0,
// after a Connected in minute -1.
func quiet(e *Engine) {
	e.OnEvent(lifecycle(streamrt.Connected, -1, 30))
	for m := range 40 {
		e.OnEvent(trade(m, 10, 1.0, 600))
		e.OnEvent(trade(m, 40, 1.0, 400))
	}
}

func at(minute int, second float64) time.Time {
	return t0.Add(time.Duration(minute)*time.Minute + time.Duration(second*float64(time.Second)))
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
	if !s.BarStart.Equal(at(40, 0)) || s.Median != 1_000 || s.Turnover != 10_000 {
		t.Fatalf("%+v", s)
	}
	if s.LastTradeLag != 0 {
		t.Fatalf("last trade at 59.9 s received 0.1 s later: lag %v", s.LastTradeLag)
	}
}

func TestASignalIsNotLostWhenTheNextMinuteTradesFirst(t *testing.T) {
	e := New(HYP030())
	quiet(e)
	e.OnEvent(trade(40, 30, 1.06, 10_000))
	e.OnEvent(trade(41, 0.05, 1.07, 100)) // the next minute arrives before the tick
	if got := e.Tick(at(41, 0.3)); len(got) != 1 {
		t.Fatalf("signals %d", len(got))
	}
}

func TestAGapDuringTheBurstMinuteSuppressesTheSignal(t *testing.T) {
	e := New(HYP030())
	quiet(e)
	e.OnEvent(trade(40, 5, 1.03, 4_000))
	e.OnEvent(lifecycle(streamrt.Disconnected, 40, 20))
	e.OnEvent(lifecycle(streamrt.Connected, 40, 25))
	e.OnEvent(trade(40, 50, 1.06, 6_000))
	if got := e.Tick(at(41, 1)); len(got) != 0 {
		t.Fatalf("fired on an incomplete bar: %+v", got)
	}
	if e.Stats.SuppressedByGap != 1 {
		t.Fatalf("stats %+v", e.Stats)
	}
}

func TestAnOverflowMarksTheOpenBarIncomplete(t *testing.T) {
	e := New(HYP030())
	quiet(e)
	e.OnEvent(trade(40, 5, 1.06, 10_000))
	e.OnEvent(lifecycle(streamrt.Overflow, 40, 30))
	if got := e.Tick(at(41, 1)); len(got) != 0 {
		t.Fatalf("fired after an overflow: %+v", got)
	}
}

func TestEmptyMinutesEnterTheMedianAsZeroTurnover(t *testing.T) {
	e := New(HYP030())
	e.OnEvent(lifecycle(streamrt.Connected, -1, 30))
	for m := 0; m < 40; m += 2 { // trades only every other minute
		e.OnEvent(trade(m, 10, 1.0, 1_000))
	}
	e.OnEvent(trade(40, 30, 1.06, 2_000)) // 2x the traded minutes, but the median is 0..1000
	e.Tick(at(41, 1))
	if e.Stats.EmptyBars == 0 {
		t.Fatalf("no empty bars were built: %+v", e.Stats)
	}
	if e.Stats.Signals != 0 {
		t.Fatal("empty minutes must pull the median down, not be skipped")
	}
}

func TestTooFewCompleteBarsAndTheCooldownBlock(t *testing.T) {
	e := New(HYP030())
	e.OnEvent(lifecycle(streamrt.Connected, -1, 30))
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

func TestALateTradeIsCountedNotAdded(t *testing.T) {
	e := New(HYP030())
	quiet(e)
	e.OnEvent(trade(39, 59, 5.0, 1e9)) // older than the newest bar
	if e.Stats.LateTrades != 0 {
		t.Fatalf("a trade of the open minute is not late: %+v", e.Stats)
	}
	e.OnEvent(trade(38, 10, 5.0, 1e9))
	if e.Stats.LateTrades != 1 {
		t.Fatalf("stats %+v", e.Stats)
	}
}

func TestTheFirstBarAfterAConnectIsIncomplete(t *testing.T) {
	e := New(HYP030())
	quiet(e)
	e.OnEvent(lifecycle(streamrt.Disconnected, 40, 0))
	e.OnEvent(lifecycle(streamrt.Connected, 41, 20))
	e.OnEvent(trade(41, 30, 1.0, 1_000))
	e.OnEvent(trade(42, 30, 1.06, 10_000)) // previous bar (minute 41) is incomplete
	if got := e.Tick(at(43, 1)); len(got) != 0 {
		t.Fatalf("fired with an incomplete previous bar: %+v", got)
	}
}
