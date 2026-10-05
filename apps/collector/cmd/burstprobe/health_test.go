package main

import (
	"context"
	"testing"
	"time"

	"github.com/alicebob/miniredis/v2"
	"github.com/redis/go-redis/v9"

	"github.com/mavlevich/schurfer/collector/internal/burstengine"
	"github.com/mavlevich/schurfer/collector/internal/streamrt"
)

func TestHealthIsReplacedWholeAndNamesItsRun(t *testing.T) {
	server := miniredis.RunT(t)
	rdb := redis.NewClient(&redis.Options{Addr: server.Addr()})
	defer func() { _ = rdb.Close() }()
	ctx := context.Background()
	publish(ctx, rdb, "run1", map[string]int64{"signals": 17, "frames": 5}, time.Now())
	publish(ctx, rdb, "run2", map[string]int64{"frames": 1}, time.Now())
	got, err := rdb.HGetAll(ctx, healthKey).Result()
	if err != nil {
		t.Fatal(err)
	}
	if _, stale := got["signals"]; stale || got["run_id"] != "run2" || got["frames"] != "1" {
		t.Fatalf("health mixes runs: %v", got)
	}
}

func TestQueuedEventsAreAppliedBeforeAnyMinuteIsDecided(t *testing.T) {
	events := make(chan streamrt.Event, 4)
	engine := burstengine.New(burstengine.HYP030())
	at := time.Date(2026, 10, 5, 12, 0, 30, 0, time.UTC)
	for i := range 3 {
		events <- streamrt.Event{Trade: &streamrt.Trade{
			Exchange: "bybit", Symbol: "AAAUSDT", TradeID: string(rune('a' + i)), Price: 1,
			Notional: 1, EventAt: at, ReceivedAt: at,
		}}
	}
	if !drain(events, engine) || len(events) != 0 || engine.Stats.Trades != 3 {
		t.Fatalf("drain left %d queued, applied %d", len(events), engine.Stats.Trades)
	}
	close(events)
	if drain(events, engine) {
		t.Fatal("a closed stream must end the loop")
	}
}
