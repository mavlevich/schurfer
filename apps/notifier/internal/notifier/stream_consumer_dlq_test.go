package notifier

import (
	"context"
	"errors"
	"net/url"
	"strings"
	"testing"

	"github.com/alicebob/miniredis/v2"
	"github.com/redis/go-redis/v9"
)

func TestDeadLetterMoveIsAtomicAndIdempotent(t *testing.T) {
	ctx := context.Background()
	mr := miniredis.RunT(t)
	rdb := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	t.Cleanup(func() { _ = rdb.Close() })
	c := NewStreamConsumer(rdb, nil, "tok", "cid")
	if err := c.setupGroup(ctx); err != nil {
		t.Fatal(err)
	}
	id, err := rdb.XAdd(ctx, &redis.XAddArgs{Stream: StreamOutboxV1,
		Values: map[string]any{"data": `{"x":1}`}}).Result()
	if err != nil {
		t.Fatal(err)
	}
	streams, err := rdb.XReadGroup(ctx, &redis.XReadGroupArgs{Group: ConsumerGroup,
		Consumer: ConsumerName, Streams: []string{StreamOutboxV1, ">"}, Count: 1, Block: -1}).Result()
	if err != nil {
		t.Fatal(err)
	}
	msg := streams[0].Messages[0]
	for range 3 { // a repeat after a crash or a lost reply adds nothing
		c.moveToDLQ(ctx, msg, "test")
	}
	if got := rdb.XLen(ctx, StreamDLQV1).Val(); got != 1 {
		t.Fatalf("dead letters = %d, want 1", got)
	}
	if got := rdb.XLen(ctx, StreamOutboxV1).Val(); got != 0 {
		t.Fatalf("outbox entries = %d, want 0", got)
	}
	summary, err := rdb.XPending(ctx, StreamOutboxV1, ConsumerGroup).Result()
	if err != nil || summary.Count != 0 {
		t.Fatalf("pending = %+v, err %v", summary, err)
	}
	dead := rdb.XRange(ctx, StreamDLQV1, "-", "+").Val()
	if dead[0].Values["source_id"] != id || dead[0].Values["reason"] != "test" {
		t.Fatalf("dead letter = %+v", dead[0].Values)
	}
}

func TestDeadLetterMoveSurvivesAFailedXADD(t *testing.T) {
	ctx := context.Background()
	mr := miniredis.RunT(t)
	rdb := redis.NewClient(&redis.Options{Addr: mr.Addr()})
	t.Cleanup(func() { _ = rdb.Close() })
	c := NewStreamConsumer(rdb, nil, "tok", "cid")
	if err := c.setupGroup(ctx); err != nil {
		t.Fatal(err)
	}
	if _, err := rdb.XAdd(ctx, &redis.XAddArgs{Stream: StreamOutboxV1,
		Values: map[string]any{"data": `{"x":1}`}}).Result(); err != nil {
		t.Fatal(err)
	}
	streams, err := rdb.XReadGroup(ctx, &redis.XReadGroupArgs{Group: ConsumerGroup,
		Consumer: ConsumerName, Streams: []string{StreamOutboxV1, ">"}, Count: 1, Block: -1}).Result()
	if err != nil {
		t.Fatal(err)
	}
	msg := streams[0].Messages[0]
	state := func() (dead, outbox, pending int64, marker bool) {
		summary, err := rdb.XPending(ctx, StreamOutboxV1, ConsumerGroup).Result()
		if err != nil {
			t.Fatal(err)
		}
		dead = rdb.XLen(ctx, StreamDLQV1).Val()
		return dead, rdb.XLen(ctx, StreamOutboxV1).Val(), summary.Count,
			rdb.Exists(ctx, dlqMarkerKey(msg.ID)).Val() == 1
	}
	// The dead-letter key holds a string: XADD fails with WRONGTYPE inside the script.
	if err := rdb.Set(ctx, StreamDLQV1, "not a stream", 0).Err(); err != nil {
		t.Fatal(err)
	}
	c.moveToDLQ(ctx, msg, "test")
	summary, _ := rdb.XPending(ctx, StreamOutboxV1, ConsumerGroup).Result()
	if summary.Count != 1 || rdb.XLen(ctx, StreamOutboxV1).Val() != 1 ||
		rdb.Exists(ctx, dlqMarkerKey(msg.ID)).Val() != 0 {
		t.Fatalf("after a failed XADD: pending %d, outbox %d, marker %d", summary.Count,
			rdb.XLen(ctx, StreamOutboxV1).Val(), rdb.Exists(ctx, dlqMarkerKey(msg.ID)).Val())
	}
	if err := rdb.Del(ctx, StreamDLQV1).Err(); err != nil { // Redis recovers
		t.Fatal(err)
	}
	for range 2 {
		c.moveToDLQ(ctx, msg, "test")
	}
	if dead, outbox, pending, marker := state(); dead != 1 || outbox != 0 || pending != 0 || !marker {
		t.Fatalf("after recovery: dead letters %d, outbox %d, pending %d, marker %v",
			dead, outbox, pending, marker)
	}
}

func TestTelegramErrorsNeverCarryTheBotToken(t *testing.T) {
	const token = "999999:FAKE-token-for-redaction"
	previousAPI := _telegramAPI
	_telegramAPI = "http://127.0.0.1:1/bot%s/sendMessage" // nothing listens: a transport error
	t.Cleanup(func() { _telegramAPI = previousAPI })
	err := sendMessage(context.Background(), "hi", token, "cid", "")
	if err == nil {
		t.Fatal("expected a transport error")
	}
	if strings.Contains(err.Error(), token) || strings.Contains(err.Error(), "FAKE-token") {
		t.Fatalf("token leaked: %q", err.Error())
	}
	wrapped := &url.Error{Op: "Post", URL: "https://api.telegram.org/bot" + token + "/sendMessage",
		Err: errors.New("dial tcp: i/o timeout for " + token)}
	got := redactedError("send", wrapped, token).Error()
	if strings.Contains(got, token) || !strings.Contains(got, "i/o timeout") {
		t.Fatalf("redacted = %q", got)
	}
}
