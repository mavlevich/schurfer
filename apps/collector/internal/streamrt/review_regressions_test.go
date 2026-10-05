package streamrt

import (
	"testing"
	"time"
)

func TestReviewDroppingOnlyAHeartbeatDoesNotLoseSubscriptionState(t *testing.T) {
	rt := New(BybitCodec{}, Config{QueueSize: 2})
	s := &shard{symbols: []string{"AAAUSDT"}}
	now := time.Now()
	for range 2 {
		rt.emit(s, Event{Trade: &Trade{Symbol: "AAAUSDT", EventAt: now}})
	}
	rt.emit(s, Event{Lifecycle: &Lifecycle{Kind: Heartbeat, Symbols: s.symbols, At: now, LastFrameAt: now}})
	<-rt.events
	<-rt.events
	rt.emit(s, Event{Lifecycle: &Lifecycle{Kind: Heartbeat, Symbols: s.symbols, At: now.Add(time.Second), LastFrameAt: now.Add(time.Second)}})
	overflow := <-rt.events
	if overflow.Lifecycle == nil || overflow.Lifecycle.Kind != Overflow {
		t.Fatal("missing overflow")
	}
	if overflow.Lifecycle.DroppedState {
		t.Fatal("dropping only a heartbeat reports unknown subscription state and opens an indefinite gap in the engine")
	}
}
