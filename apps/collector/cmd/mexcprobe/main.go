// Command mexcprobe is the sealed sizing probe of the MEXC realtime canary
// (docs/engineering/realtime-market-capture-design-v1.md, rollout step 1).
//
// It subscribes to the public deal stream of every enabled USDT-margined MEXC
// perpetual for a fixed duration and writes ONE JSON report of operational
// counters: messages and bytes by channel, per-second rates, receive-minus-event
// lag percentiles, reconnects, subscription acknowledgements and errors, the
// payload shape and the field names observed.
//
// Sealed protocol: no price, volume, side or any other market value is printed,
// logged, kept or summarized. Values are parsed only to check that the documented
// fields are present and well formed; the report carries counts and timings
// only (TestReportCarriesNoMarketValues checks the report's own fields).
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log/slog"
	"math/rand/v2"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"slices"
	"sort"
	"sync"
	"syscall"
	"time"

	"github.com/gorilla/websocket"
)

const ReportVersion = "mexc_sealed_probe_v1"

type contract struct {
	Symbol       string   `json:"symbol"`
	State        int      `json:"state"`
	QuoteCoin    string   `json:"quoteCoin"`
	SettleCoin   string   `json:"settleCoin"`
	ContractSize *float64 `json:"contractSize"`
}

// Universe returns the enabled USDT-quoted, USDT-settled contracts, sorted. Contracts
// without a contract size are counted apart: their volume could not be converted.
func Universe(body []byte) (symbols []string, missingSize int, err error) {
	var resp struct {
		Success bool       `json:"success"`
		Data    []contract `json:"data"`
	}
	if err := json.Unmarshal(body, &resp); err != nil {
		return nil, 0, fmt.Errorf("contract detail: %w", err)
	}
	if !resp.Success {
		return nil, 0, errors.New("contract detail: success=false")
	}
	for _, c := range resp.Data {
		if c.State != 0 || c.QuoteCoin != "USDT" || c.SettleCoin != "USDT" {
			continue
		}
		if c.ContractSize == nil || *c.ContractSize <= 0 {
			missingSize++
		}
		symbols = append(symbols, c.Symbol)
	}
	sort.Strings(symbols)
	return symbols, missingSize, nil
}

// Stats are the probe's counters. Only counts and timings; never a market value.
type Stats struct {
	mu                sync.Mutex
	messagesByChannel map[string]int64
	bytesByChannel    map[string]int64
	trades            int64
	malformedTrades   int64
	payloadObjects    int64
	payloadLists      int64
	fieldsSeen        map[string]int64
	lagEventMS        []int64 // receive minus trade time, sampled
	lagPushMS         []int64 // receive minus message ts, sampled
	perSecond         map[int64]int64
	reconnects        int64
	gapMS             []int64
	errorsByCode      map[string]int64
	acks              int64
	pongOffsetMS      []int64
}

const maxSamples = 200_000

func NewStats() *Stats {
	return &Stats{
		messagesByChannel: map[string]int64{},
		bytesByChannel:    map[string]int64{},
		fieldsSeen:        map[string]int64{},
		perSecond:         map[int64]int64{},
		errorsByCode:      map[string]int64{},
	}
}

func sample(dst []int64, v int64) []int64 {
	if len(dst) < maxSamples {
		return append(dst, v)
	}
	dst[rand.IntN(len(dst))] = v //nolint:gosec // sampling, not security; keeps the sample bounded
	return dst
}

var requiredTradeFields = []string{"p", "v", "T", "t"}

// Observe records one frame received at `received`.
func (s *Stats) Observe(frame []byte, received time.Time) {
	var msg struct {
		Channel string          `json:"channel"`
		Data    json.RawMessage `json:"data"`
		Ts      *int64          `json:"ts"`
		Code    *int64          `json:"code"`
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.perSecond[received.Unix()]++
	if err := json.Unmarshal(frame, &msg); err != nil {
		s.messagesByChannel["unparseable"]++
		s.bytesByChannel["unparseable"] += int64(len(frame))
		return
	}
	channel := msg.Channel
	if channel == "" {
		channel = "none"
	}
	s.messagesByChannel[channel]++
	s.bytesByChannel[channel] += int64(len(frame))
	nowMS := received.UnixMilli()
	switch channel {
	case "push.deal":
		if msg.Ts != nil {
			s.lagPushMS = sample(s.lagPushMS, nowMS-*msg.Ts)
		}
		s.observeTrades(msg.Data, nowMS)
	case "rs.sub.deal":
		s.acks++
	case "pong":
		var serverMS int64
		if json.Unmarshal(msg.Data, &serverMS) == nil && serverMS > 0 {
			s.pongOffsetMS = sample(s.pongOffsetMS, nowMS-serverMS)
		}
	case "rs.error":
		code := "unknown"
		if msg.Code != nil {
			code = fmt.Sprint(*msg.Code)
		}
		s.errorsByCode[code]++
	}
}

func (s *Stats) observeTrades(raw json.RawMessage, nowMS int64) {
	var items []map[string]json.RawMessage
	if len(raw) > 0 && raw[0] == '[' {
		s.payloadLists++
		if json.Unmarshal(raw, &items) != nil {
			s.malformedTrades++
			return
		}
	} else {
		s.payloadObjects++
		var one map[string]json.RawMessage
		if json.Unmarshal(raw, &one) != nil {
			s.malformedTrades++
			return
		}
		items = []map[string]json.RawMessage{one}
	}
	for _, item := range items {
		for key := range item {
			s.fieldsSeen[key]++
		}
		if !wellFormed(item) {
			s.malformedTrades++
			continue
		}
		s.trades++
		var tradeMS int64
		if json.Unmarshal(item["t"], &tradeMS) == nil {
			s.lagEventMS = sample(s.lagEventMS, nowMS-tradeMS)
		}
	}
}

// wellFormed checks presence and type only: numbers for p, v, t and T in {1, 2}.
// The values themselves are discarded here and never reach the report.
func wellFormed(item map[string]json.RawMessage) bool {
	for _, key := range requiredTradeFields {
		if _, ok := item[key]; !ok {
			return false
		}
	}
	var number float64
	for _, key := range []string{"p", "v", "t"} {
		if json.Unmarshal(item[key], &number) != nil {
			return false
		}
	}
	var side int
	if json.Unmarshal(item["T"], &side) != nil || (side != 1 && side != 2) {
		return false
	}
	return true
}

func (s *Stats) Reconnected(gap time.Duration) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.reconnects++
	s.gapMS = append(s.gapMS, gap.Milliseconds())
}

func percentiles(values []int64) map[string]int64 {
	if len(values) == 0 {
		return map[string]int64{}
	}
	sorted := slices.Clone(values)
	slices.Sort(sorted)
	at := func(q float64) int64 { return sorted[int(q*float64(len(sorted)-1))] }
	return map[string]int64{
		"p50": at(0.5), "p90": at(0.9), "p99": at(0.99), "max": sorted[len(sorted)-1], "n": int64(len(sorted)),
	}
}

// Report is the probe's only output. Every field is a count, a size or a timing.
type Report struct {
	Version              string                      `json:"version"`
	StartedAt            time.Time                   `json:"started_at"`
	EndedAt              time.Time                   `json:"ended_at"`
	Compress             bool                        `json:"compress"`
	Symbols              int                         `json:"symbols"`
	SymbolsMissingSize   int                         `json:"symbols_missing_contract_size"`
	Connections          int                         `json:"connections"`
	SymbolsPerConnection int                         `json:"symbols_per_connection"`
	Acks                 int64                       `json:"subscription_acks"`
	ErrorsByCode         map[string]int64            `json:"errors_by_code"`
	MessagesByChannel    map[string]int64            `json:"messages_by_channel"`
	BytesByChannel       map[string]int64            `json:"bytes_by_channel"`
	Trades               int64                       `json:"trades"`
	MalformedTrades      int64                       `json:"malformed_trades"`
	PayloadShape         map[string]int64            `json:"payload_shape"`
	FieldsSeen           map[string]int64            `json:"fields_seen"`
	MessagesPerSecond    map[string]int64            `json:"messages_per_second"`
	LagMS                map[string]map[string]int64 `json:"lag_ms"`
	Reconnects           int64                       `json:"reconnects"`
	ReconnectGapMS       map[string]int64            `json:"reconnect_gap_ms"`
	BytesPerDayEstimate  int64                       `json:"bytes_per_day_estimate"`
	TradesPerDayEstimate int64                       `json:"trades_per_day_estimate"`
}

func (s *Stats) Report(start, end time.Time, compress bool, symbols, missing, conns, perConn int) Report {
	s.mu.Lock()
	defer s.mu.Unlock()
	var rates []int64
	for second, n := range s.perSecond {
		if second > start.Unix() && second < end.Unix() { // whole seconds only
			rates = append(rates, n)
		}
	}
	var bytes int64
	for _, b := range s.bytesByChannel {
		bytes += b
	}
	seconds := end.Sub(start).Seconds()
	perDay := func(v int64) int64 {
		if seconds <= 0 {
			return 0
		}
		return int64(float64(v) / seconds * 86400)
	}
	return Report{
		Version: ReportVersion, StartedAt: start.UTC(), EndedAt: end.UTC(), Compress: compress,
		Symbols: symbols, SymbolsMissingSize: missing, Connections: conns,
		SymbolsPerConnection: perConn, Acks: s.acks, ErrorsByCode: s.errorsByCode,
		MessagesByChannel: s.messagesByChannel, BytesByChannel: s.bytesByChannel,
		Trades: s.trades, MalformedTrades: s.malformedTrades,
		PayloadShape: map[string]int64{"object": s.payloadObjects, "list": s.payloadLists},
		FieldsSeen:   s.fieldsSeen, MessagesPerSecond: percentiles(rates),
		LagMS: map[string]map[string]int64{
			"receive_minus_trade_time": percentiles(s.lagEventMS),
			"receive_minus_push_ts":    percentiles(s.lagPushMS),
			"receive_minus_pong_time":  percentiles(s.pongOffsetMS),
		},
		Reconnects: s.reconnects, ReconnectGapMS: percentiles(s.gapMS),
		BytesPerDayEstimate: perDay(bytes), TradesPerDayEstimate: perDay(s.trades),
	}
}

// runConnection keeps one connection subscribed until ctx ends, reconnecting with
// jittered backoff. Disconnect gaps are recorded.
func runConnection(ctx context.Context, url string, symbols []string, compress bool, stats *Stats) {
	backoff := time.Second
	var lostAt time.Time
	for ctx.Err() == nil {
		err := session(ctx, url, symbols, compress, stats, func() {
			if !lostAt.IsZero() {
				stats.Reconnected(time.Since(lostAt))
				lostAt = time.Time{}
			}
			backoff = time.Second
		})
		if ctx.Err() != nil {
			return
		}
		slog.Warn("mexcprobe.disconnected", "err", err)
		if lostAt.IsZero() {
			lostAt = time.Now()
		}
		jitter := time.Duration(rand.Int64N(int64(backoff) / 2)) //nolint:gosec // backoff jitter
		select {
		case <-ctx.Done():
			return
		case <-time.After(backoff + jitter):
		}
		backoff = min(backoff*2, 30*time.Second)
	}
}

func session(
	ctx context.Context, url string, symbols []string, compress bool, stats *Stats, connected func(),
) error {
	conn, resp, err := websocket.DefaultDialer.DialContext(ctx, url, nil)
	if resp != nil && resp.Body != nil {
		_ = resp.Body.Close()
	}
	if err != nil {
		return fmt.Errorf("dial: %w", err)
	}
	defer func() { _ = conn.Close() }()
	for _, symbol := range symbols {
		sub := map[string]any{
			"method": "sub.deal",
			"param":  map[string]any{"symbol": symbol, "compress": compress},
		}
		if err := conn.WriteJSON(sub); err != nil {
			return fmt.Errorf("subscribe: %w", err)
		}
	}
	connected()
	done := make(chan struct{})
	defer close(done)
	var writeMu sync.Mutex
	go func() {
		ticker := time.NewTicker(15 * time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-done:
				return
			case <-ctx.Done():
				_ = conn.Close()
				return
			case <-ticker.C:
				writeMu.Lock()
				_ = conn.WriteJSON(map[string]string{"method": "ping"})
				writeMu.Unlock()
			}
		}
	}()
	for {
		if err := conn.SetReadDeadline(time.Now().Add(60 * time.Second)); err != nil {
			return err
		}
		_, frame, err := conn.ReadMessage()
		if err != nil {
			return fmt.Errorf("read: %w", err)
		}
		stats.Observe(frame, time.Now())
	}
}

func fetchUniverse(ctx context.Context, restBase string) ([]string, int, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, restBase+"/api/v1/contract/detail", nil)
	if err != nil {
		return nil, 0, err
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return nil, 0, err
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode != http.StatusOK {
		return nil, 0, fmt.Errorf("contract detail: HTTP %d", resp.StatusCode)
	}
	body, err := io.ReadAll(io.LimitReader(resp.Body, 64<<20))
	if err != nil {
		return nil, 0, err
	}
	return Universe(body)
}

func chunk(symbols []string, size int) [][]string {
	var out [][]string
	for i := 0; i < len(symbols); i += size {
		out = append(out, symbols[i:min(i+size, len(symbols))])
	}
	return out
}

func main() {
	os.Exit(run())
}

func run() int {
	duration := flag.Duration("duration", 30*time.Minute, "how long to listen")
	perConn := flag.Int("per-connection", 50, "symbols per websocket connection")
	compress := flag.Bool("compress", false, "MEXC deal aggregation (false = individual deals)")
	restBase := flag.String("rest", "https://contract.mexc.com", "contract REST base URL")
	wsURL := flag.String("ws", "wss://contract.mexc.com/edge", "contract websocket URL")
	out := flag.String("out", "", "report path (JSON)")
	flag.Parse()
	if *out == "" || *perConn <= 0 {
		fmt.Fprintln(os.Stderr, "usage: mexcprobe -out report.json [-duration 30m]")
		return 2
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	symbols, missing, err := fetchUniverse(ctx, *restBase)
	if err != nil {
		slog.Error("mexcprobe.universe", "err", err)
		return 1
	}
	stats := NewStats()
	listen, cancel := context.WithTimeout(ctx, *duration)
	defer cancel()
	groups := chunk(symbols, *perConn)
	start := time.Now()
	var wg sync.WaitGroup
	for _, group := range groups {
		wg.Add(1)
		go func() {
			defer wg.Done()
			runConnection(listen, *wsURL, group, *compress, stats)
		}()
	}
	wg.Wait()
	report := stats.Report(start, time.Now(), *compress, len(symbols), missing, len(groups), *perConn)
	body, err := json.MarshalIndent(report, "", " ")
	if err == nil {
		err = os.MkdirAll(filepath.Dir(*out), 0o750)
	}
	if err == nil {
		err = os.WriteFile(*out, append(body, '\n'), 0o600)
	}
	if err != nil {
		slog.Error("mexcprobe.report", "err", err)
		return 1
	}
	fmt.Println(*out)
	return 0
}
