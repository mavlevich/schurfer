package notifier

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"time"

	"github.com/google/uuid"
	"github.com/redis/go-redis/v9"
)

// Publish before changing the marker: a failed XADD leaves the transition
// retryable. One Redis script also prevents a restart or concurrent poller from
// claiming a transition without putting its envelope on the delivery stream.
// Existing numeric markers remain valid incident identities during rollout.
var publishIncidentScript = redis.NewScript(`
local incident = redis.call('GET', KEYS[1])
local opening = ARGV[1] == '1'
if (opening and incident) or (not opening and not incident) then
    return 0
end
if opening then incident = ARGV[2] end
local envelope = cjson.decode(ARGV[3])
envelope.dedup_key = envelope.dedup_key .. ':' .. incident
redis.call('XADD', KEYS[2], '*', 'data', cjson.encode(envelope))
if opening then
    if tonumber(ARGV[4]) > 0 then
        redis.call('SET', KEYS[1], incident, 'PX', ARGV[4])
    else
        redis.call('SET', KEYS[1], incident)
    end
else
    redis.call('DEL', KEYS[1])
end
return 1
`)

// publishIncident queues one unhealthy/recovery edge per incident. ttl is only
// used for immutable one-shot events (capture failures); ongoing health markers
// have no expiry, so a notifier restart retains their recovery identity.
func (n *Notifier) publishIncident(
	ctx context.Context, stateKey string, opening bool, ttl time.Duration,
	kind, severity, dedupPrefix, text string,
) error {
	envelope := newEnvelope("scanner", kind, severity, dedupPrefix, text, nil)
	raw, err := json.Marshal(envelope)
	if err != nil {
		return fmt.Errorf("marshal incident: %w", err)
	}
	identity := uuid.NewString()
	if ttl > 0 {
		// Immutable event identity survives marker expiry or Redis state loss.
		identity = stateKey
	}
	published, err := publishIncidentScript.Run(ctx, n.rdb,
		[]string{stateKey, StreamOutboxV1}, opening, identity, string(raw), ttl.Milliseconds(),
	).Int()
	if err != nil {
		return fmt.Errorf("publish incident: %w", err)
	}
	if published == 1 {
		slog.Info("notifier.incident.queued", "kind", kind, "state_key", stateKey)
	}
	return nil
}
