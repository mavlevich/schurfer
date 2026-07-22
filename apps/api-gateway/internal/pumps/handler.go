package pumps

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"math"
	"net/http"
	"sort"
	"strconv"
	"strings"
	"time"
	"unicode"
	"unicode/utf8"

	"github.com/go-chi/chi/v5"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/redis/go-redis/v9"
)

var empty = []byte(`{"ts":0,"count":0,"min_change_pct":null,"pumps":[]}`)

const maxBaseRunes = 20

// isValidBase accepts exchange symbols made only from Unicode letters and decimal
// digits. Unicode support is required for legitimate contracts such as 草根文化_USDT,
// while the allow-list still rejects path/query delimiters, whitespace, control
// characters, and traversal strings before a base reaches Redis, SQL, or exchange URLs.
func isValidBase(base string) bool {
	if base == "" || !utf8.ValidString(base) {
		return false
	}
	count := 0
	for _, r := range base {
		if !unicode.IsLetter(r) && !unicode.IsDigit(r) {
			return false
		}
		count++
		if count > maxBaseRunes {
			return false
		}
	}
	return true
}

type exchangeEntry struct {
	Exchange     string  `json:"exchange"`
	Symbol       string  `json:"symbol"`
	Price        string  `json:"price"`
	ChangePct    float64 `json:"change_pct"`
	High24h      string  `json:"high_24h"`
	Volume24hUSD float64 `json:"volume_24h_usd"`
}

type pumpEntry struct {
	Base         string          `json:"base"`
	MaxChangePct float64         `json:"max_change_pct"`
	Exchanges    []exchangeEntry `json:"exchanges"`
}

type pumpsPayload struct {
	TS           int64       `json:"ts"`
	Count        int         `json:"count"`
	MinChangePct *float64    `json:"min_change_pct"`
	Pumps        []pumpEntry `json:"pumps"`
}

type historyEntry struct {
	Base        string          `json:"base"`
	Episode     int             `json:"episode"`
	FirstSeenAt int64           `json:"first_seen_at"`
	LastSeenAt  int64           `json:"last_seen_at"`
	ClosedAt    *int64          `json:"closed_at"`
	PeakPct     float64         `json:"peak_pct"`
	LastPct     float64         `json:"last_pct"`
	RetracePct  *float64        `json:"retrace_pct"`
	IsLive      bool            `json:"is_live"`
	Exchanges   json.RawMessage `json:"exchanges"`
}

type oiSnapshotEntry struct {
	Exchange string  `json:"exchange"`
	OiUSD    float64 `json:"oi_usd"`
	TS       int64   `json:"ts"`
}

type oiResponse struct {
	Base             string            `json:"base"`
	Snapshots        []oiSnapshotEntry `json:"snapshots"`
	CurrentTotalUSD  float64           `json:"current_total_usd"`
	BaselineTotalUSD float64           `json:"baseline_total_usd"`
	DeltaPct         *float64          `json:"delta_pct"`
	PumpFirstSeenAt  *int64            `json:"pump_first_seen_at"`
}

const (
	// fundingElevatedThreshold is 0.1% per 8h — above this longs are paying heavily.
	fundingElevatedThreshold = 0.001
	// fundingPeriodsPerYear converts an 8h funding rate to annualized APR (3 × 365).
	fundingPeriodsPerYear = 1095
	// fundingModerateThreshold is 0.05% per 8h — elevated but not yet crowded.
	fundingModerateThreshold = 0.0005

	signalMaxScore = 10

	pumpAgeLateHours     = 4.0 // >4h = pump is old, high time risk
	pumpAgeMatureHours   = 1.0 // 1-4h = pump maturing
	priceExtentHighPct   = 100.0
	priceExtentMidPct    = 30.0
	oiChangeThresholdPct = 5.0  // ±5% OI change = meaningful trend
	retraceHighPts       = 15.0 // >15 pct-points below peak = entry likely missed
	retraceMidPts        = 5.0  // <5 pct-points below peak = still near peak, ideal entry
)

type fundingEntry struct {
	Exchange   string  `json:"exchange"`
	Rate       float64 `json:"rate"`
	RatePct    float64 `json:"rate_pct"`
	AprPct     float64 `json:"apr_pct"`
	IsElevated bool    `json:"is_elevated"`
	RecordedAt int64   `json:"recorded_at"`
}

type fundingResponse struct {
	Base            string         `json:"base"`
	PumpFirstSeenAt *int64         `json:"pump_first_seen_at"`
	Exchanges       []fundingEntry `json:"exchanges"`
}

// pgxRow is satisfied by pgx.Row from pgxpool — extracted into an interface
// so tests can inject stubs without a live database connection.
type pgxRow interface {
	Scan(dest ...any) error
}

// pgxPool is the subset of *pgxpool.Pool used by Handler.
// *pgxpool.Pool satisfies this interface; tests inject a stubQuerier.
type pgxPool interface {
	QueryRow(ctx context.Context, sql string, args ...any) pgxRow
	Query(ctx context.Context, sql string, args ...any) (pgx.Rows, error)
}

// poolAdapter wraps *pgxpool.Pool so its QueryRow return (pgx.Row struct)
// is returned as the pgxRow interface.
type poolAdapter struct{ inner *pgxpool.Pool }

func (a *poolAdapter) QueryRow(ctx context.Context, sql string, args ...any) pgxRow {
	return a.inner.QueryRow(ctx, sql, args...)
}

func (a *poolAdapter) Query(ctx context.Context, sql string, args ...any) (pgx.Rows, error) {
	return a.inner.Query(ctx, sql, args...)
}

type Handler struct {
	rdb  *redis.Client
	pool pgxPool
}

func NewHandler(rdb *redis.Client, pool *pgxpool.Pool) *Handler {
	return &Handler{rdb: rdb, pool: &poolAdapter{inner: pool}}
}

func (h *Handler) List(w http.ResponseWriter, r *http.Request) {
	data, err := h.rdb.Get(r.Context(), "pumps:latest").Bytes()
	if errors.Is(err, redis.Nil) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write(empty)
		return
	}
	if err != nil {
		slog.Error("pumps.redis_get", "err", err)
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	_, _ = w.Write(data)
}

func (h *Handler) Token(w http.ResponseWriter, r *http.Request) {
	base := strings.ToUpper(chi.URLParam(r, "base"))
	if !isValidBase(base) {
		http.Error(w, "invalid token", http.StatusBadRequest)
		return
	}

	payload, err := h.loadPumps(r.Context())
	if errors.Is(err, redis.Nil) {
		http.Error(w, "not found", http.StatusNotFound)
		return
	}
	if err != nil {
		slog.Error("pumps.token.redis_get", "err", err)
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}

	for _, p := range payload.Pumps {
		if p.Base == base {
			w.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(w).Encode(p)
			return
		}
	}
	http.Error(w, "not found", http.StatusNotFound)
}

func (h *Handler) OHLCV(w http.ResponseWriter, r *http.Request) {
	base := strings.ToUpper(chi.URLParam(r, "base"))
	if !isValidBase(base) {
		http.Error(w, "invalid token", http.StatusBadRequest)
		return
	}

	interval := 60
	if v := r.URL.Query().Get("interval"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			interval = n
		}
	}
	limit := 200
	if v := r.URL.Query().Get("limit"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 && n <= 1000 {
			limit = n
		}
	}

	exchanges := h.rankedExchanges(r.Context(), base)
	if len(exchanges) == 0 {
		http.Error(w, "no supported exchange for OHLCV", http.StatusNotFound)
		return
	}

	cacheKey := fmt.Sprintf("ohlcv:%s:%d:%d", base, interval, limit)
	if cached, err := h.rdb.Get(r.Context(), cacheKey).Bytes(); err == nil {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write(cached)
		return
	}

	// Stop trying once we have 80% of the requested candles; 20 is the
	// absolute floor so a nearly-empty response doesn't prematurely win.
	goodEnough := limit * 4 / 5
	if goodEnough < 20 {
		goodEnough = 20
	}
	var bestCandles []Candle
	var bestExchange string
	for _, ex := range exchanges {
		candles, err := fetchOHLCV(r.Context(), ex, base, interval, limit)
		if err != nil {
			slog.Warn("pumps.ohlcv.fetch", "exchange", ex, "base", base, "err", err)
			continue
		}
		if len(candles) > len(bestCandles) {
			bestCandles = candles
			bestExchange = ex
		}
		if len(candles) >= goodEnough {
			break
		}
	}

	if len(bestCandles) == 0 {
		http.Error(w, "no OHLCV data available", http.StatusNotFound)
		return
	}

	payload, _ := json.Marshal(map[string]any{
		"base":     base,
		"exchange": bestExchange,
		"interval": interval,
		"candles":  bestCandles,
	})
	_ = h.rdb.Set(r.Context(), cacheKey, payload, 60*time.Second).Err()

	w.Header().Set("Content-Type", "application/json")
	_, _ = w.Write(payload)
}

func (h *Handler) History(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()

	// Optional filters: ?exchange=binance&since=1700000000&until=1700099999
	exchange := q.Get("exchange")
	since := parseUnixParam(q.Get("since"))
	until := parseUnixParam(q.Get("until"))

	query := `
		SELECT base, episode,
		       extract(epoch from first_seen_at)::bigint,
		       extract(epoch from last_seen_at)::bigint,
		       extract(epoch from closed_at)::bigint,
		       peak_pct, last_pct, retrace_pct, exchanges
		FROM app.pump_events
		WHERE ($1::text IS NULL OR exchanges @> jsonb_build_array(jsonb_build_object('exchange', $1::text)))
		  AND ($2::bigint IS NULL OR extract(epoch from last_seen_at) >= $2)
		  AND ($3::bigint IS NULL OR extract(epoch from first_seen_at) <= $3)
		  AND (($2 IS NOT NULL OR $3 IS NOT NULL) OR last_seen_at > NOW() - INTERVAL '24 hours')
		ORDER BY peak_pct DESC
		LIMIT 500`

	rows, err := h.pool.Query(r.Context(), query,
		nullableString(exchange),
		since,
		until,
	)
	if err != nil {
		slog.Error("pumps.history.query", "err", err)
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}
	defer rows.Close()

	live := map[string]bool{}
	if payload, err := h.loadPumps(r.Context()); err == nil {
		for _, p := range payload.Pumps {
			live[p.Base] = true
		}
	}

	entries := make([]historyEntry, 0)
	for rows.Next() {
		var e historyEntry
		var exJSON []byte
		if err := rows.Scan(
			&e.Base, &e.Episode,
			&e.FirstSeenAt, &e.LastSeenAt, &e.ClosedAt,
			&e.PeakPct, &e.LastPct, &e.RetracePct,
			&exJSON,
		); err != nil {
			slog.Error("pumps.history.scan", "err", err)
			http.Error(w, "internal error", http.StatusInternalServerError)
			return
		}
		e.Exchanges = json.RawMessage(exJSON)
		e.IsLive = live[e.Base] && e.ClosedAt == nil
		entries = append(entries, e)
	}
	if err := rows.Err(); err != nil {
		slog.Error("pumps.history.rows", "err", err)
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}

	out, _ := json.Marshal(entries)
	w.Header().Set("Content-Type", "application/json")
	_, _ = w.Write(out)
}

// TokenHistory returns all pump episodes for a single token, newest first.
func (h *Handler) TokenHistory(w http.ResponseWriter, r *http.Request) {
	base := strings.ToUpper(chi.URLParam(r, "base"))
	if !isValidBase(base) {
		http.Error(w, "invalid token", http.StatusBadRequest)
		return
	}

	rows, err := h.pool.Query(r.Context(), `
		SELECT episode,
		       extract(epoch from first_seen_at)::bigint,
		       extract(epoch from last_seen_at)::bigint,
		       extract(epoch from closed_at)::bigint,
		       peak_pct, last_pct, retrace_pct, exchanges
		FROM app.pump_events
		WHERE base = $1
		ORDER BY first_seen_at DESC
		LIMIT 100`, base)
	if err != nil {
		slog.Error("pumps.token_history.query", "base", base, "err", err)
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}
	defer rows.Close()

	live := map[string]bool{}
	if payload, err := h.loadPumps(r.Context()); err == nil {
		for _, p := range payload.Pumps {
			live[p.Base] = true
		}
	}

	entries := make([]historyEntry, 0)
	for rows.Next() {
		var e historyEntry
		var exJSON []byte
		e.Base = base
		if err := rows.Scan(
			&e.Episode,
			&e.FirstSeenAt, &e.LastSeenAt, &e.ClosedAt,
			&e.PeakPct, &e.LastPct, &e.RetracePct,
			&exJSON,
		); err != nil {
			slog.Error("pumps.token_history.scan", "err", err)
			http.Error(w, "internal error", http.StatusInternalServerError)
			return
		}
		e.Exchanges = json.RawMessage(exJSON)
		e.IsLive = live[e.Base] && e.ClosedAt == nil
		entries = append(entries, e)
	}
	if err := rows.Err(); err != nil {
		slog.Error("pumps.token_history.rows", "err", err)
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}

	out, _ := json.Marshal(entries)
	w.Header().Set("Content-Type", "application/json")
	_, _ = w.Write(out)
}

// OI returns open interest history for a token plus the delta vs the start
// of its pump episode, aggregated across exchanges. Scoped to a single
// episode (the open one if there is one, else the most recently closed one)
// so repeat pumps on the same token never mix OI data across episodes.
func (h *Handler) OI(w http.ResponseWriter, r *http.Request) {
	base := strings.ToUpper(chi.URLParam(r, "base"))
	if !isValidBase(base) {
		http.Error(w, "invalid token", http.StatusBadRequest)
		return
	}

	var eventID *int64
	var firstSeenAt *int64
	err := h.pool.QueryRow(r.Context(),
		`SELECT id, extract(epoch from first_seen_at)::bigint FROM app.pump_events
		 WHERE base = $1 ORDER BY closed_at IS NULL DESC, first_seen_at DESC LIMIT 1`,
		base,
	).Scan(&eventID, &firstSeenAt)
	if err != nil && !errors.Is(err, pgx.ErrNoRows) {
		slog.Error("pumps.oi.episode_query", "base", base, "err", err)
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}

	snapshots := make([]oiSnapshotEntry, 0)
	if eventID != nil {
		// Latest 1000 rows first (DESC), then re-ordered ASC for the response —
		// taking the oldest 1000 would make "latest" stale for long-lived episodes.
		rows, err := h.pool.Query(r.Context(),
			`SELECT exchange, oi_usd, ts FROM (
				 SELECT exchange, oi_usd, extract(epoch from recorded_at)::bigint AS ts, recorded_at
				 FROM app.oi_snapshots WHERE event_id = $1
				 ORDER BY recorded_at DESC LIMIT 1000
			 ) recent ORDER BY recorded_at ASC`,
			*eventID,
		)
		if err != nil {
			slog.Error("pumps.oi.query", "base", base, "err", err)
			http.Error(w, "internal error", http.StatusInternalServerError)
			return
		}
		defer rows.Close()

		for rows.Next() {
			var e oiSnapshotEntry
			if err := rows.Scan(&e.Exchange, &e.OiUSD, &e.TS); err != nil {
				slog.Error("pumps.oi.scan", "err", err)
				http.Error(w, "internal error", http.StatusInternalServerError)
				return
			}
			snapshots = append(snapshots, e)
		}
		if err := rows.Err(); err != nil {
			slog.Error("pumps.oi.rows", "err", err)
			http.Error(w, "internal error", http.StatusInternalServerError)
			return
		}
	}

	currentTotal, baselineTotal, deltaPct := aggregateOI(snapshots, firstSeenAt)

	out := oiResponse{
		Base:             base,
		Snapshots:        snapshots,
		CurrentTotalUSD:  currentTotal,
		BaselineTotalUSD: baselineTotal,
		DeltaPct:         deltaPct,
		PumpFirstSeenAt:  firstSeenAt,
	}
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(out)
}

// Funding returns the latest funding rate per exchange for a token's current pump
// episode (or the most recently closed one). Each entry includes the raw 8h rate,
// rate_pct (×100), annualized APR, and an is_elevated flag (rate > 0.1% / 8h).
func (h *Handler) Funding(w http.ResponseWriter, r *http.Request) {
	base := strings.ToUpper(chi.URLParam(r, "base"))
	if !isValidBase(base) {
		http.Error(w, "invalid token", http.StatusBadRequest)
		return
	}

	var eventID *int64
	var firstSeenAt *int64
	err := h.pool.QueryRow(r.Context(),
		`SELECT id, extract(epoch from first_seen_at)::bigint FROM app.pump_events
		 WHERE base = $1 ORDER BY closed_at IS NULL DESC, first_seen_at DESC LIMIT 1`,
		base,
	).Scan(&eventID, &firstSeenAt)
	if err != nil && !errors.Is(err, pgx.ErrNoRows) {
		slog.Error("pumps.funding.episode_query", "base", base, "err", err)
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}

	entries := make([]fundingEntry, 0)
	if eventID != nil {
		// Latest row per exchange via DISTINCT ON — gives the most recent rate for each.
		rows, err := h.pool.Query(r.Context(),
			`SELECT DISTINCT ON (exchange) exchange, rate,
			        extract(epoch from recorded_at)::bigint
			 FROM app.funding_rate_snapshots
			 WHERE event_id = $1
			 ORDER BY exchange, recorded_at DESC`,
			*eventID,
		)
		if err != nil {
			slog.Error("pumps.funding.query", "base", base, "err", err)
			http.Error(w, "internal error", http.StatusInternalServerError)
			return
		}
		defer rows.Close()

		for rows.Next() {
			var exchange string
			var rate float64
			var ts int64
			if err := rows.Scan(&exchange, &rate, &ts); err != nil {
				slog.Error("pumps.funding.scan", "err", err)
				http.Error(w, "internal error", http.StatusInternalServerError)
				return
			}
			entries = append(entries, fundingEntry{
				Exchange:   exchange,
				Rate:       rate,
				RatePct:    rate * 100,
				AprPct:     rate * fundingPeriodsPerYear * 100,
				IsElevated: rate > fundingElevatedThreshold,
				RecordedAt: ts,
			})
		}
		if err := rows.Err(); err != nil {
			slog.Error("pumps.funding.rows", "err", err)
			http.Error(w, "internal error", http.StatusInternalServerError)
			return
		}
	}

	out := fundingResponse{
		Base:            base,
		PumpFirstSeenAt: firstSeenAt,
		Exchanges:       entries,
	}
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(out)
}

type signalComponent struct {
	Value  float64 `json:"value"`
	Points int     `json:"points"`
	Max    int     `json:"max"`
	Note   string  `json:"note"`
}

type signalComponents struct {
	PumpAge         signalComponent `json:"pump_age"`
	PriceExtent     signalComponent `json:"price_extent"`
	OiTrend         signalComponent `json:"oi_trend"`
	FundingRate     signalComponent `json:"funding_rate"`
	RetraceFromPeak signalComponent `json:"retrace_from_peak"`
}

type signalEpisode struct {
	ID          int64   `json:"id"`
	FirstSeenAt int64   `json:"first_seen_at"`
	AgeHours    float64 `json:"age_hours"`
	PeakPct     float64 `json:"peak_pct"`
	LastPct     float64 `json:"last_pct"`
	IsOpen      bool    `json:"is_open"`
}

// signalDataQuality reports which data sources were successfully fetched.
// If a source is false its component falls back to 0 pts — callers should
// treat the verdict as provisional when either field is false.
type signalDataQuality struct {
	OI      bool `json:"oi"`
	Funding bool `json:"funding"`
}

type signalsResponse struct {
	Base        string            `json:"base"`
	Verdict     string            `json:"verdict"`
	Score       int               `json:"score"`
	MaxScore    int               `json:"max_score"`
	ComputedAt  int64             `json:"computed_at"` // Unix seconds — used by trader for freshness check
	Episode     signalEpisode     `json:"episode"`
	Components  signalComponents  `json:"components"`
	DataQuality signalDataQuality `json:"data_quality"`
}

// scoreSignals computes the five signal components and their total score.
// Extracted as a pure function so it is testable without a database or HTTP layer.
func scoreSignals(ep signalEpisode, currentOI, baselineOI, maxFunding float64) (signalComponents, int) {
	var c signalComponents

	// 1. Pump age (0-2 pts)
	c.PumpAge = signalComponent{Value: ep.AgeHours, Max: 2}
	switch {
	case ep.AgeHours > pumpAgeLateHours:
		c.PumpAge.Points = 2
		c.PumpAge.Note = fmt.Sprintf("extended pump (%.1fh), high time risk", ep.AgeHours)
	case ep.AgeHours > pumpAgeMatureHours:
		c.PumpAge.Points = 1
		c.PumpAge.Note = fmt.Sprintf("pump maturing (%.1fh)", ep.AgeHours)
	default:
		c.PumpAge.Note = fmt.Sprintf("early pump (%.1fh), may continue", ep.AgeHours)
	}

	// 2. Price extent (0-2 pts) — uses peak_pct as the high-water mark.
	c.PriceExtent = signalComponent{Value: ep.PeakPct, Max: 2}
	switch {
	case ep.PeakPct > priceExtentHighPct:
		c.PriceExtent.Points = 2
		c.PriceExtent.Note = fmt.Sprintf("very extended (+%.0f%%), distribution risk", ep.PeakPct)
	case ep.PeakPct > priceExtentMidPct:
		c.PriceExtent.Points = 1
		c.PriceExtent.Note = fmt.Sprintf("significant pump (+%.0f%%)", ep.PeakPct)
	default:
		c.PriceExtent.Note = fmt.Sprintf("moderate move (+%.0f%%)", ep.PeakPct)
	}

	// 3. OI trend (0-2 pts) — compares latest per-exchange total vs earliest.
	c.OiTrend = signalComponent{Max: 2}
	if currentOI > 0 && baselineOI > 0 {
		oiDeltaPct := (currentOI - baselineOI) / baselineOI * 100
		c.OiTrend.Value = math.Round(oiDeltaPct*100) / 100
		switch {
		case oiDeltaPct < -oiChangeThresholdPct:
			c.OiTrend.Points = 2
			c.OiTrend.Note = fmt.Sprintf("OI declining (%.1f%%), distribution underway", oiDeltaPct)
		case oiDeltaPct > oiChangeThresholdPct:
			c.OiTrend.Note = fmt.Sprintf("OI growing (+%.1f%%), new money entering", oiDeltaPct)
		default:
			c.OiTrend.Points = 1
			c.OiTrend.Note = fmt.Sprintf("OI neutral (%.1f%%)", oiDeltaPct)
		}
	} else {
		c.OiTrend.Note = "no OI data"
	}

	// 4. Funding rate (0-2 pts) — max rate across all exchanges for this episode.
	c.FundingRate = signalComponent{Value: maxFunding, Max: 2}
	switch {
	case maxFunding > fundingElevatedThreshold:
		c.FundingRate.Points = 2
		c.FundingRate.Note = fmt.Sprintf("crowded longs (%.3f%% per 8h)", maxFunding*100)
	case maxFunding > fundingModerateThreshold:
		c.FundingRate.Points = 1
		c.FundingRate.Note = fmt.Sprintf("elevated funding (%.3f%% per 8h)", maxFunding*100)
	default:
		c.FundingRate.Note = fmt.Sprintf("normal funding (%.3f%% per 8h)", maxFunding*100)
	}

	// 5. Retrace from peak (0-2 pts) — price still near its high = ideal short entry.
	// Inverted: more points when retrace is SMALL (price has not moved far from peak yet).
	// Large retrace means the optimal entry has already passed.
	retrace := ep.PeakPct - ep.LastPct
	c.RetraceFromPeak = signalComponent{Value: retrace, Max: 2}
	switch {
	case retrace <= retraceMidPts:
		c.RetraceFromPeak.Points = 2
		c.RetraceFromPeak.Note = fmt.Sprintf("still near peak (%.1f pts) — ideal entry window", retrace)
	case retrace <= retraceHighPts:
		c.RetraceFromPeak.Points = 1
		c.RetraceFromPeak.Note = fmt.Sprintf("cooling from peak (%.1f pts) — entry still viable", retrace)
	default:
		c.RetraceFromPeak.Note = fmt.Sprintf("far from peak (%.1f pts) — optimal entry likely passed", retrace)
	}

	total := c.PumpAge.Points + c.PriceExtent.Points + c.OiTrend.Points +
		c.FundingRate.Points + c.RetraceFromPeak.Points
	return c, total
}

func signalVerdict(score int) string {
	switch {
	case score >= 9:
		return "prime_short"
	case score >= 6:
		return "short_setup"
	case score >= 4:
		return "cooling_off"
	default:
		return "pumping"
	}
}

type tokenStatsResponse struct {
	Base                string   `json:"base"`
	EpisodeCount        int      `json:"episode_count"`
	RetraceCount        int      `json:"retrace_count"`
	Confidence          string   `json:"confidence"`
	AvgPeakPct          float64  `json:"avg_peak_pct"`
	MedianPeakPct       float64  `json:"median_peak_pct"`
	AvgRetracePct       *float64 `json:"avg_retrace_pct"`
	MedianRetracePct    *float64 `json:"median_retrace_pct"`
	MinRetracePct       *float64 `json:"min_retrace_pct"`
	MaxRetracePct       *float64 `json:"max_retrace_pct"`
	AvgDurationHours    float64  `json:"avg_duration_hours"`
	MedianDurationHours float64  `json:"median_duration_hours"`
}

// statsConfidence maps episode count to a data quality label so the UI can
// warn when aggregates are based on very few observations.
func statsConfidence(n int) string {
	switch {
	case n >= 6:
		return "high"
	case n >= 3:
		return "medium"
	default:
		return "low"
	}
}

func round1(v float64) float64 { return math.Round(v*10) / 10 }
func roundPtr1(v *float64) *float64 {
	if v == nil {
		return nil
	}
	r := math.Round(*v*10) / 10
	return &r
}

// Stats returns aggregate statistics across all closed pump episodes for a
// token. 404 if no closed episodes exist yet. retrace_pct fields are null when
// no episode has retrace data (retrace_pct = last_pct - peak_pct, always ≤ 0).
func (h *Handler) Stats(w http.ResponseWriter, r *http.Request) {
	base := strings.ToUpper(chi.URLParam(r, "base"))
	if !isValidBase(base) {
		http.Error(w, "invalid token", http.StatusBadRequest)
		return
	}

	var (
		episodeCount        int
		retraceCount        int
		avgPeakPct          float64
		medianPeakPct       float64
		avgRetracePct       *float64
		medianRetracePct    *float64
		minRetracePct       *float64
		maxRetracePct       *float64
		avgDurationHours    float64
		medianDurationHours float64
	)

	err := h.pool.QueryRow(r.Context(), `
		SELECT
		  COUNT(*)                                                                        AS episode_count,
		  COUNT(retrace_pct)                                                              AS retrace_count,
		  COALESCE(AVG(peak_pct), 0)                                                     AS avg_peak_pct,
		  COALESCE(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY peak_pct), 0)             AS median_peak_pct,
		  AVG(retrace_pct)                                                                AS avg_retrace_pct,
		  PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY retrace_pct)                       AS median_retrace_pct,
		  MIN(retrace_pct)                                                                AS min_retrace_pct,
		  MAX(retrace_pct)                                                                AS max_retrace_pct,
		  COALESCE(AVG(EXTRACT(epoch FROM (closed_at - first_seen_at)) / 3600), 0)       AS avg_duration_hours,
		  COALESCE(PERCENTILE_CONT(0.5) WITHIN GROUP (
		    ORDER BY EXTRACT(epoch FROM (closed_at - first_seen_at)) / 3600
		  ), 0)                                                                           AS median_duration_hours
		FROM app.pump_events
		WHERE base = $1
		  AND closed_at IS NOT NULL`,
		base,
	).Scan(
		&episodeCount, &retraceCount,
		&avgPeakPct, &medianPeakPct,
		&avgRetracePct, &medianRetracePct,
		&minRetracePct, &maxRetracePct,
		&avgDurationHours, &medianDurationHours,
	)
	if err != nil {
		slog.Error("pumps.stats.query", "base", base, "err", err)
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}
	if episodeCount == 0 {
		http.Error(w, "not found", http.StatusNotFound)
		return
	}

	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(tokenStatsResponse{
		Base:                base,
		EpisodeCount:        episodeCount,
		RetraceCount:        retraceCount,
		Confidence:          statsConfidence(episodeCount),
		AvgPeakPct:          round1(avgPeakPct),
		MedianPeakPct:       round1(medianPeakPct),
		AvgRetracePct:       roundPtr1(avgRetracePct),
		MedianRetracePct:    roundPtr1(medianRetracePct),
		MinRetracePct:       roundPtr1(minRetracePct),
		MaxRetracePct:       roundPtr1(maxRetracePct),
		AvgDurationHours:    round1(avgDurationHours),
		MedianDurationHours: round1(medianDurationHours),
	})
}

const signalsCacheKey = "signals:"
const signalsCacheTTL = 2 * time.Minute

// computeSignals queries the DB for an open pump episode and scores it.
// Returns (result, false, nil) on success, (_, true, nil) when no open episode
// exists, or (_, false, err) on a DB error.
func (h *Handler) computeSignals(ctx context.Context, base string) (signalsResponse, bool, error) {
	var eventID int64
	var firstSeenAtUnix int64
	var peakPct, lastPct float64
	err := h.pool.QueryRow(ctx,
		`SELECT id, extract(epoch from first_seen_at)::bigint, peak_pct, last_pct
		 FROM app.pump_events
		 WHERE base = $1 AND closed_at IS NULL
		 LIMIT 1`,
		base,
	).Scan(&eventID, &firstSeenAtUnix, &peakPct, &lastPct)
	if errors.Is(err, pgx.ErrNoRows) {
		return signalsResponse{}, true, nil
	}
	if err != nil {
		return signalsResponse{}, false, err
	}

	ageHours := math.Round(time.Since(time.Unix(firstSeenAtUnix, 0)).Hours()*100) / 100
	ep := signalEpisode{
		ID:          eventID,
		FirstSeenAt: firstSeenAtUnix,
		AgeHours:    ageHours,
		PeakPct:     peakPct,
		LastPct:     lastPct,
		IsOpen:      true,
	}

	var currentOI float64
	var currentOICount int
	oiCurrentOK := h.pool.QueryRow(ctx,
		`SELECT COALESCE(SUM(oi_usd), 0), COUNT(*) FROM (
		   SELECT DISTINCT ON (exchange) oi_usd
		   FROM app.oi_snapshots WHERE event_id = $1
		   ORDER BY exchange, recorded_at DESC
		 ) t`,
		eventID,
	).Scan(&currentOI, &currentOICount) == nil && currentOICount > 0

	var baselineOI float64
	var baselineOICount int
	oiBaselineOK := h.pool.QueryRow(ctx,
		`SELECT COALESCE(SUM(oi_usd), 0), COUNT(*) FROM (
		   SELECT DISTINCT ON (exchange) oi_usd
		   FROM app.oi_snapshots WHERE event_id = $1
		   ORDER BY exchange, recorded_at ASC
		 ) t`,
		eventID,
	).Scan(&baselineOI, &baselineOICount) == nil && baselineOICount > 0

	oiOK := oiCurrentOK && oiBaselineOK
	if !oiOK {
		slog.Warn("pumps.signals.oi_unavailable", "base", base,
			"current_ok", oiCurrentOK, "baseline_ok", oiBaselineOK)
	}

	var maxFunding float64
	var fundingCount int
	fundingOK := h.pool.QueryRow(ctx,
		`SELECT COALESCE(MAX(rate), 0), COUNT(*) FROM (
		   SELECT DISTINCT ON (exchange) rate
		   FROM app.funding_rate_snapshots WHERE event_id = $1
		   ORDER BY exchange, recorded_at DESC
		 ) t`,
		eventID,
	).Scan(&maxFunding, &fundingCount) == nil && fundingCount > 0

	if !fundingOK {
		slog.Warn("pumps.signals.funding_unavailable", "base", base)
	}

	components, score := scoreSignals(ep, currentOI, baselineOI, maxFunding)
	verdict := signalVerdict(score)
	if !oiOK && !fundingOK {
		verdict = "insufficient_data"
	}

	return signalsResponse{
		Base:        base,
		Verdict:     verdict,
		Score:       score,
		MaxScore:    signalMaxScore,
		ComputedAt:  time.Now().Unix(),
		Episode:     ep,
		Components:  components,
		DataQuality: signalDataQuality{OI: oiOK, Funding: fundingOK},
	}, false, nil
}

// CacheSignals computes the signal score for base and writes it to Redis.
// When no open episode exists the stale key is deleted so trader cannot act on it.
func (h *Handler) CacheSignals(ctx context.Context, base string) error {
	base = strings.ToUpper(base)
	if !isValidBase(base) {
		return nil
	}
	out, notFound, err := h.computeSignals(ctx, base)
	if err != nil {
		return err
	}
	if notFound {
		return h.rdb.Del(ctx, signalsCacheKey+base).Err()
	}
	data, err := json.Marshal(out)
	if err != nil {
		return err
	}
	return h.rdb.Set(ctx, signalsCacheKey+base, data, signalsCacheTTL).Err()
}

// Signals returns a composite short-readiness score for a token's active pump
// episode. 404 if no open episode exists — signals only apply to live pumps.
// Score 0-10 from five components: pump age, price extent, OI trend, funding
// rate, and retrace from peak. Verdict: pumping / cooling_off / short_setup /
// prime_short / insufficient_data (when both OI and funding queries failed).
func (h *Handler) Signals(w http.ResponseWriter, r *http.Request) {
	base := strings.ToUpper(chi.URLParam(r, "base"))
	if !isValidBase(base) {
		http.Error(w, "invalid token", http.StatusBadRequest)
		return
	}
	out, notFound, err := h.computeSignals(r.Context(), base)
	if notFound {
		http.Error(w, "no open pump episode", http.StatusNotFound)
		return
	}
	if err != nil {
		slog.Error("pumps.signals.episode_query", "base", base, "err", err)
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(out)
}

// aggregateOI computes current vs baseline total OI (summed across exchanges)
// from a single episode's snapshots, ordered ascending by recorded_at.
// baseline is each exchange's first snapshot at/after firstSeenAt (or its
// first snapshot at all, if firstSeenAt is nil); current is each exchange's
// latest snapshot. Pulled out of the handler so this logic — the part that
// actually has edge cases (single snapshot, no baseline, etc.) — is unit
// testable without a database.
func aggregateOI(snapshots []oiSnapshotEntry, firstSeenAt *int64) (current, baseline float64, deltaPct *float64) {
	latestByExchange := map[string]float64{}
	baselineByExchange := map[string]float64{}
	for _, e := range snapshots {
		latestByExchange[e.Exchange] = e.OiUSD
		if _, seen := baselineByExchange[e.Exchange]; !seen {
			if firstSeenAt == nil || e.TS >= *firstSeenAt {
				baselineByExchange[e.Exchange] = e.OiUSD
			}
		}
	}

	for _, v := range latestByExchange {
		current += v
	}
	for _, v := range baselineByExchange {
		baseline += v
	}

	if baseline > 0 {
		d := (current - baseline) / baseline * 100
		deltaPct = &d
	}
	return current, baseline, deltaPct
}

func parseUnixParam(s string) *int64 {
	if s == "" {
		return nil
	}
	v, err := strconv.ParseInt(s, 10, 64)
	if err != nil {
		return nil
	}
	return &v
}

func nullableString(s string) *string {
	if s == "" {
		return nil
	}
	return &s
}

// supportedOHLCV is the set of exchanges we can fetch OHLCV candles from.
var supportedOHLCV = map[string]bool{
	"binance": true,
	"bybit":   true,
	"okx":     true,
	"gate":    true,
	"bingx":   true,
	"mexc":    true,
	"xt":      true,
}

// ohlcvPriority is a tie-breaker when volumes are equal (e.g. DB fallback
// where all volumes are 0). Lower index = higher priority.
var ohlcvPriority = map[string]int{
	"binance": 0,
	"bybit":   1,
	"okx":     2,
	"gate":    3,
	"bingx":   4,
	"mexc":    5,
	"xt":      6,
}

// rankExchangeEntries filters entries to supportedOHLCV, sorts by volume
// descending, and uses ohlcvPriority as a deterministic tie-breaker.
// Pure function — extracted for testability.
func rankExchangeEntries(entries []exchangeEntry) []string {
	type exVol struct {
		exchange string
		volume   float64
	}
	var ranked []exVol
	for _, ex := range entries {
		if supportedOHLCV[ex.Exchange] {
			ranked = append(ranked, exVol{ex.Exchange, ex.Volume24hUSD})
		}
	}
	sort.Slice(ranked, func(i, j int) bool {
		if ranked[i].volume != ranked[j].volume {
			return ranked[i].volume > ranked[j].volume
		}
		return ohlcvPriority[ranked[i].exchange] < ohlcvPriority[ranked[j].exchange]
	})
	out := make([]string, len(ranked))
	for i, e := range ranked {
		out[i] = e.exchange
	}
	return out
}

// rankedExchanges returns exchanges available for base sorted by 24h volume
// descending, filtered to those supported for OHLCV. Live Redis snapshot is
// checked first (has volume); falls back to DB (no volume, order arbitrary).
func (h *Handler) rankedExchanges(ctx context.Context, base string) []string {
	if payload, err := h.loadPumps(ctx); err == nil {
		for _, p := range payload.Pumps {
			if p.Base == base {
				if ranked := rankExchangeEntries(p.Exchanges); len(ranked) > 0 {
					return ranked
				}
				break
			}
		}
	}

	if h.pool != nil {
		var dbEntries []exchangeEntry
		for ex := range h.dbExchanges(ctx, base) {
			dbEntries = append(dbEntries, exchangeEntry{Exchange: ex})
		}
		return rankExchangeEntries(dbEntries)
	}

	return nil
}

func (h *Handler) dbExchanges(ctx context.Context, base string) map[string]bool {
	var raw []byte
	// No time window: a token's episode history (and its chart) stays visible
	// on TokenPage indefinitely, so the exchange list used to fetch that
	// chart shouldn't expire after 24h either — pick the most recent episode.
	err := h.pool.QueryRow(ctx,
		`SELECT exchanges FROM app.pump_events WHERE base = $1 ORDER BY last_seen_at DESC LIMIT 1`,
		base,
	).Scan(&raw)
	if err != nil || len(raw) == 0 {
		return nil
	}
	var entries []struct {
		Exchange string `json:"exchange"`
	}
	if err := json.Unmarshal(raw, &entries); err != nil {
		return nil
	}
	out := make(map[string]bool, len(entries))
	for _, e := range entries {
		out[e.Exchange] = true
	}
	return out
}

func (h *Handler) loadPumps(ctx context.Context) (*pumpsPayload, error) {
	data, err := h.rdb.Get(ctx, "pumps:latest").Bytes()
	if err != nil {
		return nil, err
	}
	var p pumpsPayload
	if err := json.Unmarshal(data, &p); err != nil {
		return nil, err
	}
	return &p, nil
}
