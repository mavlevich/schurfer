package liquidationcapture

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/redis/go-redis/v9"
)

const healthTTL = 30 * time.Second

func HealthKey(exchange string) string {
	return "market:liquidationcapture:health:" + exchange
}

type Health struct {
	Exchange             string
	Status               string
	CoverageKind         CoverageKind
	ProcessSessionID     string
	UniverseVersion      string
	StartedAt            time.Time
	UpdatedAt            time.Time
	LastEventAt          time.Time
	LastPersistAt        time.Time
	SubscribedSymbols    int
	ConnectedConnections int
	ExpectedConnections  int
	DataLossDetected     bool
	Source               SourceStats
	Writer               WriterStats
	Evaluated            EvaluatedHealth
}

type RedisStore struct{ client *redis.Client }

func NewRedisStore(client *redis.Client) (*RedisStore, error) {
	if client == nil {
		return nil, errors.New("redis client is required")
	}
	return &RedisStore{client: client}, nil
}

func (store *RedisStore) StoreHealth(ctx context.Context, health Health) error {
	if health.Exchange == "" {
		return errors.New("liquidation health exchange is required")
	}
	values := map[string]any{
		"schema_version":                    2,
		"capture_version":                   CaptureVersion,
		"exchange":                          health.Exchange,
		"status":                            health.Evaluated.Status,
		"reason_codes":                      health.Evaluated.ReasonCodes,
		"coverage_kind":                     string(health.CoverageKind),
		"process_session_id":                health.ProcessSessionID,
		"universe_version":                  health.UniverseVersion,
		"started_at_ms":                     unixMilliOrZero(health.StartedAt),
		"updated_at_ms":                     unixMilliOrZero(health.UpdatedAt),
		"last_event_at_ms":                  unixMilliOrZero(health.LastEventAt),
		"last_persist_at_ms":                unixMilliOrZero(health.LastPersistAt),
		"last_heartbeat_bucket_ms":          health.Evaluated.LastHeartbeatBucketMs,
		"last_complete_heartbeat_bucket_ms": health.Evaluated.LastCompleteHeartbeatBucketMs,
		"consecutive_incomplete_minutes":    health.Evaluated.ConsecutiveIncompleteMinutes,
		"subscribed_symbols":                health.SubscribedSymbols,
		"connected_connections":             health.ConnectedConnections,
		"expected_connections":              health.ExpectedConnections,
		"reconnects_window":                 health.Evaluated.ReconnectsWindow,
		"read_timeouts_window":              health.Evaluated.ReadTimeoutsWindow,
		"writer_queue_depth":                health.Writer.QueueDepth,
		"writer_queue_utilization":          health.Evaluated.WriterQueueUtilization,
		"writer_queue_drops_delta":          health.Evaluated.WriterQueueDropsDelta,
		"persist_errors_delta":              health.Evaluated.PersistErrorsDelta,
		"payload_hash_mismatch_total":       health.Writer.PayloadHashMismatchTotal,
		"status_changed_at_ms":              unixMilliOrZero(health.UpdatedAt), // Ideally from state

		"data_loss_detected":               health.DataLossDetected,
		"events_accepted_total":            health.Source.EventsAcceptedTotal,
		"events_invalid_total":             health.Source.EventsInvalidTotal,
		"events_out_of_scope_total":        health.Source.EventsOutOfScopeTotal,
		"scope_tag_missing_accepted_total": health.Source.ScopeTagMissingAcceptedTotal,
		"reconnect_total":                  health.Source.ReconnectTotal,
		"read_timeout_total":               health.Source.ReadTimeoutTotal,
		"writer_queue_peak":                health.Writer.QueuePeak,
		"writer_queue_drops_total":         health.Writer.QueueDropsTotal,
		"events_persisted_total":           health.Writer.EventsPersistedTotal,
		"duplicate_events_total":           health.Writer.DuplicateEventsTotal,
		"persist_errors_total":             health.Writer.PersistErrorsTotal,
	}
	pipe := store.client.Pipeline()
	pipe.HSet(ctx, HealthKey(health.Exchange), values)
	pipe.Expire(ctx, HealthKey(health.Exchange), healthTTL)
	if _, err := pipe.Exec(ctx); err != nil {
		return fmt.Errorf("store liquidation capture health: %w", err)
	}
	return nil
}

func unixMilliOrZero(value time.Time) int64 {
	if value.IsZero() {
		return 0
	}
	return value.UnixMilli()
}
