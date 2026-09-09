package decisions

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/jackc/pgx/v5"
)

func serveBuckets(q pgxPool, target string) *httptest.ResponseRecorder {
	h := &Handler{pool: q}
	req := httptest.NewRequest(http.MethodGet, target, nil)
	w := httptest.NewRecorder()
	h.Buckets(w, req)
	return w
}

func bucketRowVals(time int64, count int, opened bool, reason string, distinct int) []any {
	return []any{time, count, opened, &reason, distinct}
}

func decodeBuckets(t *testing.T, w *httptest.ResponseRecorder) bucketsResponse {
	t.Helper()
	var got bucketsResponse
	if err := json.Unmarshal(w.Body.Bytes(), &got); err != nil {
		t.Fatalf("decode: %v (body %q)", err, w.Body.String())
	}
	return got
}

func TestBucketsRequiresABase(t *testing.T) {
	// Bucketing every token's decisions together produces a chart-shaped answer
	// to a question nobody asked, so this is required rather than defaulted.
	w := serveBuckets(&stubQuerier{}, "/api/decisions/buckets")
	if w.Code != http.StatusBadRequest {
		t.Fatalf("want 400, got %d", w.Code)
	}
}

func TestBucketsRejectsAnUnparseableBound(t *testing.T) {
	// Ignoring it would silently widen the window to everything and return a
	// plausible answer to a different question, which the caller cannot detect.
	for _, target := range []string{
		"/api/decisions/buckets?base=CZ&since=yesterday",
		"/api/decisions/buckets?base=CZ&until=2026-13-45",
	} {
		w := serveBuckets(&stubQuerier{}, target)
		if w.Code != http.StatusBadRequest {
			t.Errorf("%s: want 400, got %d", target, w.Code)
		}
	}
}

func TestBucketsRejectsAnInvertedWindow(t *testing.T) {
	w := serveBuckets(
		&stubQuerier{},
		"/api/decisions/buckets?base=CZ&since=2026-09-08T00:00:00Z&until=2026-09-07T00:00:00Z",
	)
	if w.Code != http.StatusBadRequest {
		t.Fatalf("want 400, got %d", w.Code)
	}
}

func TestBucketsRejectsAnIntervalNoCandleHas(t *testing.T) {
	for _, target := range []string{
		"/api/decisions/buckets?base=CZ&bucket_seconds=30",
		"/api/decisions/buckets?base=CZ&bucket_seconds=0",
		"/api/decisions/buckets?base=CZ&bucket_seconds=999999999",
		"/api/decisions/buckets?base=CZ&bucket_seconds=fifteen",
	} {
		w := serveBuckets(&stubQuerier{}, target)
		if w.Code != http.StatusBadRequest {
			t.Errorf("%s: want 400, got %d", target, w.Code)
		}
	}
}

func TestBucketsPassesTheWindowAndIntervalToSQL(t *testing.T) {
	var capturedArgs []any
	var capturedSQL string
	q := &stubQuerier{
		onQuery: func(_ context.Context, sql string, args ...any) (pgx.Rows, error) {
			capturedSQL = sql
			capturedArgs = args
			return &stubRows{}, nil
		},
	}
	w := serveBuckets(
		&stubQuerier{onQuery: q.onQuery},
		"/api/decisions/buckets?base=CZ&bucket_seconds=900"+
			"&since=2026-09-07T00:00:00Z&until=2026-09-08T00:00:00Z",
	)
	if w.Code != http.StatusOK {
		t.Fatalf("want 200, got %d (%s)", w.Code, w.Body.String())
	}
	// base, bucket_seconds, since, until, limit
	if len(capturedArgs) != 5 {
		t.Fatalf("want 5 args, got %d: %v", len(capturedArgs), capturedArgs)
	}
	if capturedArgs[0] != "CZ" {
		t.Errorf("want base CZ, got %v", capturedArgs[0])
	}
	if capturedArgs[1] != 900 {
		t.Errorf("want bucket_seconds 900, got %v", capturedArgs[1])
	}
	if !strings.Contains(capturedSQL, "d.ts >= $3") {
		t.Errorf("since not bound in SQL: %s", capturedSQL)
	}
	if !strings.Contains(capturedSQL, "d.ts < $4") {
		t.Errorf("until not bound in SQL: %s", capturedSQL)
	}
	// The whole point of the endpoint: one row per candle, aggregated in SQL.
	if !strings.Contains(capturedSQL, "GROUP BY bucket_time") {
		t.Errorf("not aggregated server-side: %s", capturedSQL)
	}
	if !strings.Contains(capturedSQL, "mode() WITHIN GROUP") {
		t.Errorf("dominant reason is not the most frequent one: %s", capturedSQL)
	}
}

func TestBucketsOmitsAnAbsentBound(t *testing.T) {
	var capturedArgs []any
	q := &stubQuerier{
		onQuery: func(_ context.Context, _ string, args ...any) (pgx.Rows, error) {
			capturedArgs = args
			return &stubRows{}, nil
		},
	}
	serveBuckets(q, "/api/decisions/buckets?base=CZ")
	// base, bucket_seconds, limit
	if len(capturedArgs) != 3 {
		t.Fatalf("want 3 args with no bounds, got %d: %v", len(capturedArgs), capturedArgs)
	}
}

func TestBucketsReturnsOneRowPerCandle(t *testing.T) {
	q := &stubQuerier{
		onQuery: func(_ context.Context, _ string, _ ...any) (pgx.Rows, error) {
			return &stubRows{cols: [][]any{
				bucketRowVals(1757200000, 14, false, "pump_below_entry_floor", 2),
				bucketRowVals(1757200900, 1, true, "entered", 1),
			}}, nil
		},
	}
	w := serveBuckets(q, "/api/decisions/buckets?base=CZ&bucket_seconds=900")
	if w.Code != http.StatusOK {
		t.Fatalf("want 200, got %d", w.Code)
	}
	got := decodeBuckets(t, w)
	if got.BucketSeconds != 900 {
		t.Errorf("want bucket_seconds 900 echoed back, got %d", got.BucketSeconds)
	}
	if len(got.Buckets) != 2 {
		t.Fatalf("want 2 buckets, got %d", len(got.Buckets))
	}
	if got.Buckets[0].Count != 14 || got.Buckets[0].DominantReason != "pump_below_entry_floor" {
		t.Errorf("first bucket wrong: %+v", got.Buckets[0])
	}
	if got.Buckets[0].DistinctReasons != 2 {
		t.Errorf("want 2 distinct reasons so the caller can say '+1 other', got %d",
			got.Buckets[0].DistinctReasons)
	}
	if !got.Buckets[1].Opened {
		t.Errorf("second bucket should be marked opened: %+v", got.Buckets[1])
	}
	if got.Truncated {
		t.Error("two buckets is not truncated")
	}
}

func TestBucketsEmptyIsAnEmptyArrayNotNull(t *testing.T) {
	// A null would make the caller's `buckets.map` throw, and the caller cannot
	// distinguish "no decisions" from "request failed".
	w := serveBuckets(&stubQuerier{}, "/api/decisions/buckets?base=CZ")
	if !strings.Contains(w.Body.String(), `"buckets":[]`) {
		t.Errorf("want an empty array, got %s", w.Body.String())
	}
}

func TestBucketsReportsTruncationRatherThanHidingIt(t *testing.T) {
	// The defect this endpoint replaces was exactly this shape: the chart got a
	// capped page and said nothing. A cap that is reached has to reach the
	// response, so the chart can say it is drawing an incomplete picture.
	rows := make([][]any, 0, maxBuckets+1)
	for i := range maxBuckets + 1 {
		rows = append(rows, bucketRowVals(int64(i)*60, 1, false, "skipped", 1))
	}
	q := &stubQuerier{
		onQuery: func(_ context.Context, _ string, _ ...any) (pgx.Rows, error) {
			return &stubRows{cols: rows}, nil
		},
	}
	w := serveBuckets(q, "/api/decisions/buckets?base=CZ")
	got := decodeBuckets(t, w)
	if !got.Truncated {
		t.Error("want truncated=true when the cap is exceeded")
	}
	if len(got.Buckets) != maxBuckets {
		t.Errorf("want the response capped at %d, got %d", maxBuckets, len(got.Buckets))
	}
}

func TestBucketsAskForOneMoreThanTheCapSoTruncationIsKnown(t *testing.T) {
	var capturedArgs []any
	q := &stubQuerier{
		onQuery: func(_ context.Context, _ string, args ...any) (pgx.Rows, error) {
			capturedArgs = args
			return &stubRows{}, nil
		},
	}
	serveBuckets(q, "/api/decisions/buckets?base=CZ")
	limit := capturedArgs[len(capturedArgs)-1]
	if limit != maxBuckets+1 {
		t.Errorf("want a limit of %d so the extra row proves truncation, got %v",
			maxBuckets+1, limit)
	}
}

func TestBucketsQueryFailureIsFiveHundred(t *testing.T) {
	q := &stubQuerier{
		onQuery: func(_ context.Context, _ string, _ ...any) (pgx.Rows, error) {
			return nil, fmt.Errorf("connection refused")
		},
	}
	w := serveBuckets(q, "/api/decisions/buckets?base=CZ")
	if w.Code != http.StatusInternalServerError {
		t.Fatalf("want 500, got %d", w.Code)
	}
}

func TestBucketsToleratesANullDominantReason(t *testing.T) {
	// mode() returns NULL when every reason in the group is NULL, which older
	// rows can be. An empty string is the honest rendering; a panic is not.
	q := &stubQuerier{
		onQuery: func(_ context.Context, _ string, _ ...any) (pgx.Rows, error) {
			return &stubRows{cols: [][]any{{int64(1757200000), 3, false, nil, 0}}}, nil
		},
	}
	w := serveBuckets(q, "/api/decisions/buckets?base=CZ")
	if w.Code != http.StatusOK {
		t.Fatalf("want 200, got %d (%s)", w.Code, w.Body.String())
	}
	got := decodeBuckets(t, w)
	if got.Buckets[0].DominantReason != "" {
		t.Errorf("want an empty reason, got %q", got.Buckets[0].DominantReason)
	}
}
