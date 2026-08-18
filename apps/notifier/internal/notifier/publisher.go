package notifier

import (
	"context"
	"encoding/json"
	"fmt"
	"time"

	"github.com/google/uuid"
	"github.com/redis/go-redis/v9"
)

// publishEnvelope constructs a v1 notification envelope and pushes it to Redis.
func (n *Notifier) publishEnvelope(
	ctx context.Context,
	producer string,
	kind string,
	severity string,
	dedupKey string,
	text string,
	metadata map[string]any,
) error {
	env := Envelope{
		SchemaVersion:  1,
		NotificationID: uuid.NewString(),
		DedupKey:       dedupKey,
		Producer:       producer,
		Kind:           kind,
		Severity:       severity,
		CreatedAt:      time.Now().UTC(),
		Payload: Payload{
			Text:     text,
			Metadata: metadata,
		},
	}

	b, err := json.Marshal(env)
	if err != nil {
		return fmt.Errorf("marshal envelope: %w", err)
	}

	err = n.rdb.XAdd(ctx, &redis.XAddArgs{
		Stream: StreamOutboxV1,
		Values: map[string]interface{}{
			"data": string(b),
		},
	}).Err()
	if err != nil {
		return fmt.Errorf("xadd envelope: %w", err)
	}

	return nil
}
