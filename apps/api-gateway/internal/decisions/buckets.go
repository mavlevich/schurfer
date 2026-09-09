package decisions

import (
	"encoding/json"
	"log/slog"
	"net/http"
	"strconv"
	"time"
)

// Bucketing belongs on the server, and the reason is a measurement rather than a
// preference. The token chart needs three things per candle -- how many
// decisions fell in it, whether any of them opened something, and which reason
// dominated the rest. Fetching the rows to work that out client-side cannot do
// it: /api/decisions orders by ts DESC and caps at 200, so a token with 2210
// skips got its most recent 200 and the older half of the visible candles had no
// markers at all, with nothing on screen saying so.
//
// One row per candle is a few hundred rows for any window a chart draws, so the
// answer is exact, bounded, and needs no truncation notice.

const (
	// A day of one-minute candles is 1440 buckets; a wide window at 1m is the
	// worst case and this bounds it without truncating anything a chart would
	// actually draw.
	maxBuckets = 5000
	// Anything below a minute cannot align to a candle this system has.
	minBucketSeconds = 60
	// A week per bucket. Beyond this the grouping stops being a candle.
	maxBucketSeconds = 604800
)

type decisionBucket struct {
	// Seconds since epoch, the candle's opening time.
	Time int64 `json:"time"`
	// How many decisions fell into this candle.
	Count int `json:"count"`
	// True when at least one of them opened something, live or paper.
	Opened bool `json:"opened"`
	// The most frequent reason in this candle.
	DominantReason string `json:"dominant_reason"`
	// How many distinct reasons appeared, so the caller can say "+2 other"
	// without being sent every row.
	DistinctReasons int `json:"distinct_reasons"`
}

type bucketsResponse struct {
	BucketSeconds int              `json:"bucket_seconds"`
	Truncated     bool             `json:"truncated"`
	Buckets       []decisionBucket `json:"buckets"`
}

// parseTime accepts an empty value as "no bound" and anything else as RFC3339.
func parseTime(value string) (*time.Time, error) {
	if value == "" {
		return nil, nil
	}
	parsed, err := time.Parse(time.RFC3339, value)
	if err != nil {
		return nil, err
	}
	return &parsed, nil
}

// Buckets handles GET /api/decisions/buckets
// Query params: base, since, until (RFC3339), bucket_seconds
func (h *Handler) Buckets(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()

	base := q.Get("base")
	if base == "" {
		// Required, not defaulted. Bucketing every token's decisions together
		// produces a chart-shaped answer to a question nobody asked.
		http.Error(w, "base is required", http.StatusBadRequest)
		return
	}

	since, sinceErr := parseTime(q.Get("since"))
	until, untilErr := parseTime(q.Get("until"))
	if sinceErr != nil || untilErr != nil {
		// Rejected rather than ignored. A mistyped bound that silently widens the
		// window to everything returns a plausible answer to a different
		// question, and the caller cannot tell which one it got.
		http.Error(w, "since and until must be RFC3339 timestamps", http.StatusBadRequest)
		return
	}
	if since != nil && until != nil && !since.Before(*until) {
		http.Error(w, "since must be before until", http.StatusBadRequest)
		return
	}

	bucketSeconds := minBucketSeconds
	if v := q.Get("bucket_seconds"); v != "" {
		n, err := strconv.Atoi(v)
		if err != nil || n < minBucketSeconds || n > maxBucketSeconds {
			http.Error(w, "bucket_seconds must be between 60 and 604800", http.StatusBadRequest)
			return
		}
		bucketSeconds = n
	}

	args := []any{base, bucketSeconds}
	where := "WHERE d.base = $1"
	if since != nil {
		args = append(args, *since)
		where += " AND d.ts >= $" + strconv.Itoa(len(args))
	}
	if until != nil {
		args = append(args, *until)
		where += " AND d.ts < $" + strconv.Itoa(len(args))
	}
	args = append(args, maxBuckets+1)

	// mode() gives the most frequent reason exactly, rather than the first of an
	// arbitrary ordering. The bucket start is floored to a multiple of the
	// interval so it lands on the candle's own opening time.
	rows, err := h.pool.Query(r.Context(), `
		SELECT (floor(extract(epoch FROM d.ts) / $2) * $2)::bigint AS bucket_time,
		       count(*) AS count,
		       bool_or(d.action LIKE 'opened%') AS opened,
		       mode() WITHIN GROUP (ORDER BY d.reason) AS dominant_reason,
		       count(DISTINCT d.reason) AS distinct_reasons
		FROM app.trade_decisions d `+where+`
		GROUP BY bucket_time
		ORDER BY bucket_time
		LIMIT $`+strconv.Itoa(len(args)),
		args...,
	)
	if err != nil {
		slog.Error("decisions.buckets.query", "err", err)
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}
	defer rows.Close()

	result := make([]decisionBucket, 0)
	for rows.Next() {
		var bucket decisionBucket
		var reason *string
		if err := rows.Scan(
			&bucket.Time, &bucket.Count, &bucket.Opened, &reason, &bucket.DistinctReasons,
		); err != nil {
			slog.Error("decisions.buckets.scan", "err", err)
			http.Error(w, "internal error", http.StatusInternalServerError)
			return
		}
		if reason != nil {
			bucket.DominantReason = *reason
		}
		result = append(result, bucket)
	}
	if err := rows.Err(); err != nil {
		slog.Error("decisions.buckets.rows", "err", err)
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}

	// One extra row was requested so this is knowable rather than assumed. It
	// reaches the response instead of being dropped, because a chart drawing an
	// incomplete picture has to be able to say so.
	truncated := len(result) > maxBuckets
	if truncated {
		result = result[:maxBuckets]
	}

	w.Header().Set("Content-Type", "application/json")
	if err := json.NewEncoder(w).Encode(bucketsResponse{
		BucketSeconds: bucketSeconds,
		Truncated:     truncated,
		Buckets:       result,
	}); err != nil {
		slog.Error("decisions.buckets.encode", "err", err)
	}
}
