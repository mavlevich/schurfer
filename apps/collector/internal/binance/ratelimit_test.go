package binance

import (
	"context"
	"testing"
	"time"
)

func TestTokenBucketStartsFull(t *testing.T) {
	t.Parallel()
	refill := make(chan time.Time) // never ticks in this test
	b := newTokenBucket(3, refill)
	defer b.stop()

	ctx, cancel := context.WithTimeout(context.Background(), 200*time.Millisecond)
	defer cancel()

	for i := 0; i < 3; i++ {
		if !b.wait(ctx) {
			t.Fatalf("wait() #%d = false, want a token immediately available (bucket starts full)", i)
		}
	}
}

func TestTokenBucketBlocksOnceEmptyUntilRefill(t *testing.T) {
	t.Parallel()
	refill := make(chan time.Time)
	b := newTokenBucket(1, refill)
	defer b.stop()

	ctx := context.Background()
	if !b.wait(ctx) {
		t.Fatal("first wait() = false, want the initial token")
	}

	// Bucket is empty now. A short-lived context proves wait() actually
	// blocks (does not return true spuriously) rather than proving
	// anything about real time.
	blockedCtx, cancel := context.WithTimeout(ctx, 30*time.Millisecond)
	defer cancel()
	if b.wait(blockedCtx) {
		t.Fatal("wait() returned true with no refill and an empty bucket, want it to block until ctx is done")
	}

	// Now inject a refill tick and confirm a token becomes available.
	refillCtx, cancel2 := context.WithTimeout(context.Background(), time.Second)
	defer cancel2()
	done := make(chan bool, 1)
	go func() { done <- b.wait(refillCtx) }()
	refill <- time.Now()
	select {
	case ok := <-done:
		if !ok {
			t.Fatal("wait() = false after a refill tick, want true")
		}
	case <-time.After(time.Second):
		t.Fatal("wait() never returned after a refill tick")
	}
}

func TestTokenBucketRefillDoesNotExceedCapacity(t *testing.T) {
	// A refill tick while the bucket is already full must not queue up
	// credit for later -- a token bucket caps burst, it does not
	// accumulate unbounded backlog during an idle period.
	t.Parallel()
	refill := make(chan time.Time, 5)
	b := newTokenBucket(1, refill)
	defer b.stop()

	// Bucket starts full (capacity 1). Send several refill ticks while it
	// stays full; give the bucket's own goroutine time to process them.
	for i := 0; i < 5; i++ {
		refill <- time.Now()
	}
	time.Sleep(50 * time.Millisecond)

	ctx, cancel := context.WithTimeout(context.Background(), 200*time.Millisecond)
	defer cancel()
	if !b.wait(ctx) {
		t.Fatal("expected the one available token")
	}

	// No leftover credit: a second wait must block (nothing left) until we
	// send one more real refill tick.
	blockedCtx, cancel2 := context.WithTimeout(context.Background(), 30*time.Millisecond)
	defer cancel2()
	if b.wait(blockedCtx) {
		t.Fatal("wait() returned true immediately, want the excess refill ticks to have been dropped, not queued")
	}
}

func TestTokenBucketWaitReturnsFalseOnContextCancellation(t *testing.T) {
	t.Parallel()
	refill := make(chan time.Time)
	b := newTokenBucket(0, refill) // capacity clamped to 1 internally, but never refilled
	defer b.stop()

	ctx := context.Background()
	if !b.wait(ctx) {
		t.Fatal("first wait() = false, want the initial token")
	}

	cancelledCtx, cancel := context.WithCancel(context.Background())
	cancel()
	if b.wait(cancelledCtx) {
		t.Fatal("wait() = true on an already-cancelled context, want false")
	}
}
