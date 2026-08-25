package main

import (
	"testing"
	"time"

	"github.com/mavlevich/schurfer/collector/internal/binance"
)

func quote(symbol string, eventSecond int64, bid float64) binance.PublicBookTicker {
	at := time.Unix(eventSecond, 0).UTC()
	return binance.PublicBookTicker{
		Symbol: symbol, EventAt: at, ReceivedAt: at, BidPrice: bid, AskPrice: bid + 1,
	}
}

func TestBookTickerMailboxCoalescesToNewestValuePerSymbol(t *testing.T) {
	t.Parallel()
	mailbox := newBookTickerMailbox(2)
	mailbox.Offer(quote("BTCUSDT", 1, 100))
	mailbox.Offer(quote("BTCUSDT", 3, 102))
	mailbox.Offer(quote("BTCUSDT", 2, 101)) // late older event must not win

	items := mailbox.Take(10)
	if len(items) != 1 || items[0].BidPrice != 102 {
		t.Fatalf("items = %+v, want only newest BTC quote", items)
	}
	stats := mailbox.Stats()
	if stats.CoalescedTotal != 2 || stats.DropsTotal != 0 || stats.Peak != 1 {
		t.Fatalf("stats = %+v", stats)
	}
}

func TestBookTickerMailboxIsBoundedByUniqueSymbols(t *testing.T) {
	t.Parallel()
	mailbox := newBookTickerMailbox(2)
	mailbox.Offer(quote("BTCUSDT", 1, 100))
	mailbox.Offer(quote("ETHUSDT", 1, 200))
	mailbox.Offer(quote("SOLUSDT", 1, 300))

	stats := mailbox.Stats()
	if stats.Depth != 2 || stats.Peak != 2 || stats.DropsTotal != 1 {
		t.Fatalf("stats = %+v", stats)
	}
}

func TestBookTickerMailboxTakeIsBoundedAndResignalsRemainingWork(t *testing.T) {
	t.Parallel()
	mailbox := newBookTickerMailbox(3)
	mailbox.Offer(quote("BTCUSDT", 1, 100))
	mailbox.Offer(quote("ETHUSDT", 1, 200))
	mailbox.Offer(quote("SOLUSDT", 1, 300))
	<-mailbox.Ready()

	if got := len(mailbox.Take(1)); got != 1 {
		t.Fatalf("first batch = %d, want 1", got)
	}
	select {
	case <-mailbox.Ready():
	case <-time.After(time.Second):
		t.Fatal("mailbox did not signal remaining work")
	}
	if got := len(mailbox.Take(10)); got != 2 {
		t.Fatalf("second batch = %d, want 2", got)
	}
}
