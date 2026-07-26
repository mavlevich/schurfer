package notifier

import (
	"context"
	"encoding/json"
	"log/slog"

	"github.com/redis/go-redis/v9"
)

const maxAlertOutboxBatch = 100

func (n *Notifier) recordOrEnqueueAlert(ctx context.Context, delivery alertDelivery) {
	if err := n.recorder.Record(ctx, delivery); err == nil {
		return
	} else {
		raw, marshalErr := json.Marshal(delivery)
		if marshalErr != nil {
			slog.Error(
				"notifier.alert.measurement_encode_failed",
				"base", delivery.Base,
				"pump_event_id", delivery.EventID,
				"err", marshalErr,
			)
			return
		}
		if enqueueErr := n.rdb.RPush(ctx, redisKeyAlertOutbox, raw).Err(); enqueueErr != nil {
			slog.Error(
				"notifier.alert.measurement_lost",
				"base", delivery.Base,
				"pump_event_id", delivery.EventID,
				"database_err", err,
				"redis_err", enqueueErr,
			)
			return
		}
		slog.Warn(
			"notifier.alert.measurement_queued",
			"base", delivery.Base,
			"pump_event_id", delivery.EventID,
			"err", err,
		)
	}
}

func (n *Notifier) drainAlertOutbox(ctx context.Context) {
	if n.recorder == nil {
		return
	}
	for range maxAlertOutboxBatch {
		raw, err := n.rdb.LIndex(ctx, redisKeyAlertOutbox, 0).Bytes()
		if err == redis.Nil {
			return
		}
		if err != nil {
			slog.Warn("notifier.alert.outbox_read_failed", "err", err)
			return
		}

		var delivery alertDelivery
		if err := json.Unmarshal(raw, &delivery); err != nil {
			slog.Error("notifier.alert.outbox_invalid", "err", err)
			if moveErr := n.rdb.LMove(
				ctx,
				redisKeyAlertOutbox,
				redisKeyAlertDLQ,
				"LEFT",
				"RIGHT",
			).Err(); moveErr != nil {
				slog.Error("notifier.alert.outbox_dlq_failed", "err", moveErr)
				return
			}
			continue
		}
		if err := n.recorder.Record(ctx, delivery); err != nil {
			slog.Warn("notifier.alert.outbox_retry_failed", "err", err)
			return
		}
		if err := n.rdb.LPop(ctx, redisKeyAlertOutbox).Err(); err != nil {
			// The database insert is idempotent. Leaving the item queued is safer
			// than deleting an unknown item after a Redis acknowledgement failure.
			slog.Warn("notifier.alert.outbox_ack_failed", "err", err)
			return
		}
	}
}
