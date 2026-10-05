package burstengine

import (
	"github.com/mavlevich/schurfer/collector/internal/streamrt"
	"testing"
)

func TestReviewHeartbeatMustCoverDisconnectLagAllowance(t *testing.T) {
	e := New(HYP030())
	quiet(e)
	e.OnEvent(trade(40, 30, 1.06, 10000))
	e.OnEvent(streamrt.Event{Lifecycle: &streamrt.Lifecycle{Kind: streamrt.Heartbeat, Symbols: []string{"AAAUSDT"}, At: at(41, .3), LastFrameAt: at(41, .3)}})
	fired := e.Tick(at(41, .4))
	e.OnEvent(streamrt.Event{Lifecycle: &streamrt.Lifecycle{Kind: streamrt.Disconnected, Symbols: []string{"AAAUSDT"}, At: at(42, .3), LastFrameAt: at(41, .3)}})
	if len(fired) > 0 && !e.symbols["AAAUSDT"].complete(at(40, 0)) {
		t.Fatalf("already emitted %d signal on a bar invalidated by the same last-frame watermark minus LagAllowance", len(fired))
	}
}

func TestReviewDroppedHeartbeatCanRecoverWithoutForcedReconnect(t *testing.T) {
	e := New(HYP030())
	quiet(e)
	e.OnEvent(streamrt.Event{Lifecycle: &streamrt.Lifecycle{Kind: streamrt.Overflow, Symbols: []string{"AAAUSDT"}, At: at(40, 2), Since: at(40, 1), Dropped: 1, DroppedState: true}})
	// Runtime heartbeats are emitted only after the venue has confirmed subscription.
	e.OnEvent(streamrt.Event{Lifecycle: &streamrt.Lifecycle{Kind: streamrt.Heartbeat, Symbols: []string{"AAAUSDT"}, SessionID: "healthy-session", At: at(40, 3), LastFrameAt: at(40, 3)}})
	e.OnEvent(trade(41, 30, 1, 1000))
	e.OnEvent(trade(42, 30, 1.06, 10000))
	e.OnEvent(streamrt.Event{Lifecycle: &streamrt.Lifecycle{Kind: streamrt.Heartbeat, Symbols: []string{"AAAUSDT"}, SessionID: "healthy-session", At: at(43, 3), LastFrameAt: at(43, 3)}})
	got := e.Tick(at(43, 3))
	if len(got) != 1 {
		t.Fatalf("healthy stream remains permanently incomplete after a dropped heartbeat: signals=%d gapOpen=%v suppressed=%d", len(got), e.symbols["AAAUSDT"].gapOpen(), e.Stats.SuppressedByGap)
	}
}
