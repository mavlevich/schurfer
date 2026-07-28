package hotset

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strconv"
	"strings"
	"time"

	"github.com/redis/go-redis/v9"
)

const (
	MeasurementKey   = "pumps:measurement"
	HealthKey        = "market:hotset:health"
	WatchKey         = "market:hotset:bybit"
	watchMetadataKey = "market:hotset:bybit:metadata"
	streamPrefix     = "market:hot:bars:"
)

type RedisStore struct {
	client       *redis.Client
	streamMaxLen int64
	streamTTL    time.Duration
}

type ActivationSnapshot struct {
	Activations []Activation
	Status      string
	Candidates  int
	Unmapped    int
}

type watchMetadata struct {
	Base        string `json:"base"`
	PumpEventID int64  `json:"pump_event_id"`
	Reason      string `json:"reason"`
}

type Health struct {
	UpdatedAt             time.Time
	LastEventAt           time.Time
	EventsTotal           uint64
	InvalidTotal          uint64
	OutOfOrderTotal       uint64
	BarsPersistedTotal    uint64
	PersistErrorsTotal    uint64
	NATSDroppedTotal      uint64
	PendingDroppedTotal   uint64
	ObservedSymbols       int
	HotSymbols            int
	EventRate             float64
	LastLag               time.Duration
	MaxLag                time.Duration
	WindowMaxLag          time.Duration
	PumpFeedStatus        string
	MeasurementCandidates int
	UnmappedCandidates    int
}

func NewRedisStore(client *redis.Client, streamMaxLen int64, streamTTL time.Duration) (*RedisStore, error) {
	if client == nil {
		return nil, errors.New("redis client is required")
	}
	if streamMaxLen <= 0 {
		return nil, errors.New("stream max length must be positive")
	}
	if streamTTL <= 0 {
		return nil, errors.New("stream TTL must be positive")
	}
	return &RedisStore{client: client, streamMaxLen: streamMaxLen, streamTTL: streamTTL}, nil
}

func (store *RedisStore) Activations(ctx context.Context) (ActivationSnapshot, error) {
	raw, err := store.client.Get(ctx, MeasurementKey).Bytes()
	if err != nil {
		if errors.Is(err, redis.Nil) {
			return ActivationSnapshot{Status: "missing"}, nil
		}
		return ActivationSnapshot{Status: "error"}, fmt.Errorf("read measurement feed: %w", err)
	}
	var feed struct {
		Pumps []struct {
			Base        string `json:"base"`
			PumpEventID int64  `json:"pump_event_id"`
			Exchanges   []struct {
				Exchange string `json:"exchange"`
				MarketID string `json:"market_id"`
			} `json:"exchanges"`
		} `json:"pumps"`
	}
	if err := json.Unmarshal(raw, &feed); err != nil {
		return ActivationSnapshot{Status: "invalid"}, fmt.Errorf("decode measurement feed: %w", err)
	}
	activations := make([]Activation, 0, len(feed.Pumps))
	candidates := 0
	unmapped := 0
	for _, pump := range feed.Pumps {
		if strings.TrimSpace(pump.Base) == "" || pump.PumpEventID <= 0 {
			continue
		}
		candidates++
		symbol := ""
		for _, venue := range pump.Exchanges {
			if strings.EqualFold(venue.Exchange, "bybit") && strings.TrimSpace(venue.MarketID) != "" {
				symbol = venue.MarketID
				break
			}
		}
		if symbol == "" {
			unmapped++
			continue
		}
		activations = append(activations, Activation{
			Symbol:      normalizeSymbol(symbol),
			Base:        pump.Base,
			PumpEventID: pump.PumpEventID,
			Reason:      "measurement_feed",
		})
	}
	return ActivationSnapshot{
		Activations: activations,
		Status:      "ok",
		Candidates:  candidates,
		Unmapped:    unmapped,
	}, nil
}

func (store *RedisStore) RefreshActivations(
	ctx context.Context,
	activations []Activation,
	expiresAt time.Time,
) error {
	if len(activations) == 0 {
		return nil
	}
	if expiresAt.IsZero() {
		return errors.New("activation expiry is required")
	}
	pipe := store.client.Pipeline()
	for _, activation := range activations {
		symbol := normalizeSymbol(activation.Symbol)
		base := strings.TrimSpace(activation.Base)
		reason := strings.TrimSpace(activation.Reason)
		if symbol == "" || base == "" || activation.PumpEventID <= 0 || reason == "" {
			return errors.New("activation identity is incomplete")
		}
		metadata, err := json.Marshal(watchMetadata{
			Base:        base,
			PumpEventID: activation.PumpEventID,
			Reason:      reason,
		})
		if err != nil {
			return fmt.Errorf("encode activation metadata: %w", err)
		}
		pipe.ZAdd(ctx, WatchKey, redis.Z{
			Score:  float64(expiresAt.UnixMilli()),
			Member: symbol,
		})
		pipe.HSet(ctx, watchMetadataKey, symbol, metadata)
	}
	if _, err := pipe.Exec(ctx); err != nil {
		return fmt.Errorf("refresh activation registry: %w", err)
	}
	return nil
}

func (store *RedisStore) ActiveActivations(ctx context.Context, now time.Time) ([]Activation, error) {
	if now.IsZero() {
		return nil, errors.New("activation query time is required")
	}
	cutoff := strconv.FormatInt(now.UnixMilli(), 10)
	expired, err := store.client.ZRangeByScore(ctx, WatchKey, &redis.ZRangeBy{
		Min: "-inf",
		Max: cutoff,
	}).Result()
	if err != nil {
		return nil, fmt.Errorf("read expired activation registry: %w", err)
	}
	if len(expired) > 0 {
		pipe := store.client.Pipeline()
		members := make([]any, len(expired))
		fields := make([]string, len(expired))
		for index, symbol := range expired {
			members[index] = symbol
			fields[index] = symbol
		}
		pipe.ZRem(ctx, WatchKey, members...)
		pipe.HDel(ctx, watchMetadataKey, fields...)
		if _, err := pipe.Exec(ctx); err != nil {
			return nil, fmt.Errorf("remove expired activation registry: %w", err)
		}
	}

	active, err := store.client.ZRangeByScoreWithScores(ctx, WatchKey, &redis.ZRangeBy{
		Min: "(" + cutoff,
		Max: "+inf",
	}).Result()
	if err != nil {
		return nil, fmt.Errorf("read active activation registry: %w", err)
	}
	activations := make([]Activation, 0, len(active))
	for _, item := range active {
		symbol, ok := item.Member.(string)
		if !ok || symbol == "" {
			continue
		}
		raw, err := store.client.HGet(ctx, watchMetadataKey, symbol).Bytes()
		if errors.Is(err, redis.Nil) {
			continue
		}
		if err != nil {
			return nil, fmt.Errorf("read activation metadata: %w", err)
		}
		var metadata watchMetadata
		if err := json.Unmarshal(raw, &metadata); err != nil {
			return nil, fmt.Errorf("decode activation metadata: %w", err)
		}
		activations = append(activations, Activation{
			Symbol:      symbol,
			Base:        metadata.Base,
			PumpEventID: metadata.PumpEventID,
			Reason:      metadata.Reason,
			ExpiresAt:   time.UnixMilli(int64(item.Score)),
		})
	}
	return activations, nil
}

func (store *RedisStore) StoreBars(ctx context.Context, bars []Bar) error {
	if len(bars) == 0 {
		return nil
	}
	pipe := store.client.Pipeline()
	touched := make(map[string]struct{})
	for _, bar := range bars {
		key := streamPrefix + bar.Exchange + ":" + bar.Symbol
		touched[key] = struct{}{}
		values := map[string]any{
			"schema_version":      bar.SchemaVersion,
			"exchange":            bar.Exchange,
			"symbol":              bar.Symbol,
			"base":                bar.Base,
			"pump_event_id":       bar.PumpEventID,
			"activation":          bar.Activation,
			"bucket_start_ms":     bar.BucketStart.UnixMilli(),
			"first_event_at_ms":   bar.FirstEventAt.UnixMilli(),
			"last_event_at_ms":    bar.LastEventAt.UnixMilli(),
			"last_received_at_ms": bar.LastReceivedAt.UnixMilli(),
			"open":                formatFloat(bar.Open),
			"high":                formatFloat(bar.High),
			"low":                 formatFloat(bar.Low),
			"close":               formatFloat(bar.Close),
			"event_count":         bar.EventCount,
			"max_lag_ms":          bar.MaxLag.Milliseconds(),
		}
		addOptional(values, "bid", bar.Bid)
		addOptional(values, "ask", bar.Ask)
		addOptional(values, "volume_delta_24h", bar.VolumeDelta24h)
		addOptional(values, "turnover_delta_24h", bar.TurnoverDelta24h)
		pipe.XAdd(ctx, &redis.XAddArgs{
			Stream: key,
			MaxLen: store.streamMaxLen,
			Approx: true,
			Values: values,
		})
	}
	for key := range touched {
		pipe.Expire(ctx, key, store.streamTTL)
	}
	if _, err := pipe.Exec(ctx); err != nil {
		return fmt.Errorf("persist hot bars: %w", err)
	}
	return nil
}

func (store *RedisStore) StoreHealth(ctx context.Context, health Health) error {
	values := map[string]any{
		"schema_version":         1,
		"updated_at_ms":          health.UpdatedAt.UnixMilli(),
		"last_event_at_ms":       unixMilliOrZero(health.LastEventAt),
		"events_total":           health.EventsTotal,
		"invalid_total":          health.InvalidTotal,
		"out_of_order_total":     health.OutOfOrderTotal,
		"bars_persisted_total":   health.BarsPersistedTotal,
		"persist_errors_total":   health.PersistErrorsTotal,
		"nats_dropped_total":     health.NATSDroppedTotal,
		"pending_dropped_total":  health.PendingDroppedTotal,
		"observed_symbols":       health.ObservedSymbols,
		"hot_symbols":            health.HotSymbols,
		"event_rate_per_sec":     strconv.FormatFloat(health.EventRate, 'f', 2, 64),
		"last_lag_ms":            health.LastLag.Milliseconds(),
		"max_lag_ms":             health.MaxLag.Milliseconds(),
		"window_max_lag_ms":      health.WindowMaxLag.Milliseconds(),
		"pump_feed_status":       health.PumpFeedStatus,
		"measurement_candidates": health.MeasurementCandidates,
		"unmapped_candidates":    health.UnmappedCandidates,
	}
	pipe := store.client.Pipeline()
	pipe.HSet(ctx, HealthKey, values)
	pipe.Expire(ctx, HealthKey, 30*time.Second)
	if _, err := pipe.Exec(ctx); err != nil {
		return fmt.Errorf("store hot-set health: %w", err)
	}
	return nil
}

func addOptional(values map[string]any, key string, value *float64) {
	if value == nil {
		return
	}
	values[key] = formatFloat(*value)
}

func formatFloat(value float64) string {
	return strconv.FormatFloat(value, 'g', -1, 64)
}

func unixMilliOrZero(value time.Time) int64 {
	if value.IsZero() {
		return 0
	}
	return value.UnixMilli()
}
