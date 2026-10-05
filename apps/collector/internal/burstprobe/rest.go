// Package burstprobe is the HYP-030 bounded path measurement
// (docs/research/hyp030-path-measurement-v1.md): it acts on burstengine signals by
// fetching the quotes an entry and an exit would have met, and the funding actually
// settled over the hold. Every market value goes to sealed files only; the health
// record and the daily summary carry counters and durations. It never sends an order.
package burstprobe

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"time"
)

// REST is the Bybit public market REST client of the probe.
type REST struct {
	Base   string // e.g. https://api.bybit.com
	Client *http.Client
}

// BookSnapshot is one 50-level order book with its timing. Prices and sizes stay
// strings as the venue sent them: they are sealed, never computed on here.
type BookSnapshot struct {
	Symbol      string      `json:"symbol"`
	Bids        [][2]string `json:"bids"`
	Asks        [][2]string `json:"asks"`
	ExchangeTS  int64       `json:"exchange_ts_ms"`
	MatchingTS  int64       `json:"matching_ts_ms"`
	UpdateID    int64       `json:"update_id"`
	Seq         int64       `json:"seq"`
	RequestedAt time.Time   `json:"requested_at"`
	ReceivedAt  time.Time   `json:"received_at"`
}

// Funding is the ticker's funding state at a moment.
type Funding struct {
	Rate            string    `json:"rate"`
	NextFundingMs   int64     `json:"next_funding_ms"`
	IntervalHours   int       `json:"interval_hours"`
	ReceivedAt      time.Time `json:"received_at"`
	SettledOverHold []Settled `json:"settled_over_hold,omitempty"`
}

// Settled is one funding settlement from the history.
type Settled struct {
	Rate        string `json:"rate"`
	TimestampMs int64  `json:"timestamp_ms"`
}

type envelope struct {
	RetCode int             `json:"retCode"`
	RetMsg  string          `json:"retMsg"`
	Result  json.RawMessage `json:"result"`
}

func (r REST) get(ctx context.Context, path string, query url.Values) (json.RawMessage, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, r.Base+path+"?"+query.Encode(), nil)
	if err != nil {
		return nil, err
	}
	client := r.Client
	if client == nil {
		client = http.DefaultClient
	}
	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("%s: HTTP %d", path, resp.StatusCode)
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, 4<<20))
	if err != nil {
		return nil, err
	}
	var env envelope
	if err := json.Unmarshal(body, &env); err != nil {
		return nil, fmt.Errorf("%s: decode: %w", path, err)
	}
	if env.RetCode != 0 {
		return nil, fmt.Errorf("%s: retCode %d", path, env.RetCode)
	}
	return env.Result, nil
}

// Book fetches the 50-level linear order book.
func (r REST) Book(ctx context.Context, symbol string) (BookSnapshot, error) {
	requested := time.Now()
	raw, err := r.get(ctx, "/v5/market/orderbook", url.Values{
		"category": {"linear"}, "symbol": {symbol}, "limit": {"50"},
	})
	received := time.Now()
	if err != nil {
		return BookSnapshot{}, err
	}
	var result struct {
		Symbol string      `json:"s"`
		Bids   [][2]string `json:"b"`
		Asks   [][2]string `json:"a"`
		TS     int64       `json:"ts"`
		CTS    int64       `json:"cts"`
		U      int64       `json:"u"`
		Seq    int64       `json:"seq"`
	}
	if err := json.Unmarshal(raw, &result); err != nil {
		return BookSnapshot{}, fmt.Errorf("orderbook: %w", err)
	}
	if result.Symbol != symbol || len(result.Bids) == 0 || len(result.Asks) == 0 {
		return BookSnapshot{}, errors.New("orderbook: empty or for another symbol")
	}
	return BookSnapshot{
		Symbol: symbol, Bids: result.Bids, Asks: result.Asks, ExchangeTS: result.TS,
		MatchingTS: result.CTS, UpdateID: result.U, Seq: result.Seq,
		RequestedAt: requested, ReceivedAt: received,
	}, nil
}

// Funding fetches the ticker's current funding rate and next settlement.
func (r REST) Funding(ctx context.Context, symbol string) (Funding, error) {
	raw, err := r.get(ctx, "/v5/market/tickers", url.Values{"category": {"linear"}, "symbol": {symbol}})
	received := time.Now()
	if err != nil {
		return Funding{}, err
	}
	var result struct {
		List []struct {
			Symbol        string `json:"symbol"`
			FundingRate   string `json:"fundingRate"`
			NextFunding   string `json:"nextFundingTime"`
			IntervalHours string `json:"fundingIntervalHour"`
		} `json:"list"`
	}
	if err := json.Unmarshal(raw, &result); err != nil {
		return Funding{}, fmt.Errorf("tickers: %w", err)
	}
	if len(result.List) != 1 || result.List[0].Symbol != symbol {
		return Funding{}, errors.New("tickers: not exactly the requested symbol")
	}
	item := result.List[0]
	next, _ := strconv.ParseInt(item.NextFunding, 10, 64)
	interval, _ := strconv.Atoi(item.IntervalHours)
	return Funding{Rate: item.FundingRate, NextFundingMs: next, IntervalHours: interval, ReceivedAt: received}, nil
}

// SettledBetween returns the funding settlements in [from, to].
func (r REST) SettledBetween(ctx context.Context, symbol string, from, to time.Time) ([]Settled, error) {
	raw, err := r.get(ctx, "/v5/market/funding/history", url.Values{
		"category": {"linear"}, "symbol": {symbol},
		"startTime": {strconv.FormatInt(from.UnixMilli(), 10)},
		"endTime":   {strconv.FormatInt(to.UnixMilli(), 10)},
	})
	if err != nil {
		return nil, err
	}
	var result struct {
		List []struct {
			Symbol    string `json:"symbol"`
			Rate      string `json:"fundingRate"`
			Timestamp string `json:"fundingRateTimestamp"`
		} `json:"list"`
	}
	if err := json.Unmarshal(raw, &result); err != nil {
		return nil, fmt.Errorf("funding history: %w", err)
	}
	out := make([]Settled, 0, len(result.List))
	for _, item := range result.List {
		ts, err := strconv.ParseInt(item.Timestamp, 10, 64)
		if err != nil || item.Symbol != symbol {
			continue
		}
		if ts >= from.UnixMilli() && ts <= to.UnixMilli() {
			out = append(out, Settled{Rate: item.Rate, TimestampMs: ts})
		}
	}
	return out, nil
}
