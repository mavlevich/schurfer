package pumps

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/redis/go-redis/v9"
)

var (
	empty     = []byte(`{"ts":0,"count":0,"min_change_pct":null,"pumps":[]}`)
	validBase = regexp.MustCompile(`^[A-Z0-9]{1,20}$`)
)

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
	FirstSeenAt int64           `json:"first_seen_at"`
	LastSeenAt  int64           `json:"last_seen_at"`
	PeakPct     float64         `json:"peak_pct"`
	LastPct     float64         `json:"last_pct"`
	IsLive      bool            `json:"is_live"`
	Exchanges   json.RawMessage `json:"exchanges"`
}

type Handler struct {
	rdb  *redis.Client
	pool *pgxpool.Pool
}

func NewHandler(rdb *redis.Client, pool *pgxpool.Pool) *Handler {
	return &Handler{rdb: rdb, pool: pool}
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
	if !validBase.MatchString(base) {
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
	if !validBase.MatchString(base) {
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

	exchange := h.pickExchange(r.Context(), base)
	cacheKey := fmt.Sprintf("ohlcv:%s:%s:%d:%d", exchange, base, interval, limit)

	if cached, err := h.rdb.Get(r.Context(), cacheKey).Bytes(); err == nil {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write(cached)
		return
	}

	candles, err := fetchOHLCV(r.Context(), exchange, base, interval, limit)
	if err != nil {
		slog.Error("pumps.ohlcv.fetch", "exchange", exchange, "base", base, "err", err)
		http.Error(w, "failed to fetch OHLCV", http.StatusBadGateway)
		return
	}

	payload, _ := json.Marshal(map[string]any{
		"base":     base,
		"exchange": exchange,
		"interval": interval,
		"candles":  candles,
	})
	_ = h.rdb.Set(r.Context(), cacheKey, payload, 60*time.Second).Err()

	w.Header().Set("Content-Type", "application/json")
	_, _ = w.Write(payload)
}

func (h *Handler) History(w http.ResponseWriter, r *http.Request) {
	rows, err := h.pool.Query(r.Context(), `
		SELECT base,
		       extract(epoch from first_seen_at)::bigint,
		       extract(epoch from last_seen_at)::bigint,
		       peak_pct, last_pct, exchanges
		FROM app.pump_events
		WHERE last_seen_at > NOW() - INTERVAL '24 hours'
		ORDER BY peak_pct DESC
		LIMIT 200
	`)
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
		if err := rows.Scan(&e.Base, &e.FirstSeenAt, &e.LastSeenAt, &e.PeakPct, &e.LastPct, &exJSON); err != nil {
			slog.Error("pumps.history.scan", "err", err)
			http.Error(w, "internal error", http.StatusInternalServerError)
			return
		}
		e.Exchanges = json.RawMessage(exJSON)
		e.IsLive = live[e.Base]
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

// pickExchange returns the best exchange for OHLCV data for the given base.
// Prefers Binance > Bybit > OKX; falls back to Bybit if none are available.
func (h *Handler) pickExchange(ctx context.Context, base string) string {
	preferred := []string{"binance", "bybit", "okx"}

	payload, err := h.loadPumps(ctx)
	if err != nil {
		return "bybit"
	}

	available := map[string]bool{}
	for _, p := range payload.Pumps {
		if p.Base == base {
			for _, ex := range p.Exchanges {
				available[ex.Exchange] = true
			}
			break
		}
	}

	for _, ex := range preferred {
		if available[ex] {
			return ex
		}
	}
	return "bybit"
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
