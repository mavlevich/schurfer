package binance

import (
	"context"
	"time"
)

// tokenBucket is a minimal, dependency-free rate limiter backing
// PollOpenInterest's concurrent worker pool (see openinterest.go's own
// OpenInterestSchedulerConfig doc comment for why a token bucket replaced
// the single-goroutine round-robin it used to be). Capacity tokens are
// available immediately at construction -- every worker can issue its
// first request without waiting on a single refill tick, the same
// cold-start reasoning the previous design documented ("the FIRST poll
// fires immediately, not after waiting one full interval") -- and after
// that, pacing comes entirely from refill, one token per tick received.
//
// refill is injected rather than built internally so tests can drive it
// deterministically (send synthetic ticks on a channel they control)
// instead of waiting on real wall-clock time; production callers pass a
// time.Ticker's own channel.
type tokenBucket struct {
	tokens chan struct{}
	done   chan struct{}
}

func newTokenBucket(capacity int, refill <-chan time.Time) *tokenBucket {
	if capacity < 1 {
		capacity = 1
	}
	b := &tokenBucket{
		tokens: make(chan struct{}, capacity),
		done:   make(chan struct{}),
	}
	for i := 0; i < capacity; i++ {
		b.tokens <- struct{}{}
	}
	go func() {
		for {
			select {
			case <-refill:
				select {
				case b.tokens <- struct{}{}:
				default:
					// Bucket is already full; this tick's token is simply
					// not needed right now, not queued for later (a token
					// bucket caps burst, it does not accumulate unbounded
					// credit for an idle period).
				}
			case <-b.done:
				return
			}
		}
	}()
	return b
}

// wait blocks until a token is available, returning true, or ctx is
// cancelled first, returning false. Callers must check the return value:
// a false means no token was actually consumed.
func (b *tokenBucket) wait(ctx context.Context) bool {
	select {
	case <-b.tokens:
		return true
	case <-ctx.Done():
		return false
	}
}

// stop releases the bucket's own refill goroutine. Safe to call once;
// calling it twice panics (close on a closed channel), matching every
// other one-shot stop in this package (e.g. tokenBucket has no Stop-twice
// caller anywhere in this codebase today).
func (b *tokenBucket) stop() {
	close(b.done)
}
