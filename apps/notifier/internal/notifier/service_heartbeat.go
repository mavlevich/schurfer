package notifier

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"strconv"
	"strings"
	"time"
)

// Service heartbeats: a worker writes a Redis hash with `generated_at` (RFC 3339)
// and `status` every tick; this monitor alerts once when a configured hash is
// missing, older than its limit, or reports a non-ok status for longer than that
// limit, and sends one recovery after it has been healthy for
// serviceHeartbeatRecoveryHold. `notifier:maintenance:<name>` (set with a TTL by
// `make prod-deploy` for the services it restarts) suppresses both, so a deploy
// does not page anyone and an unrelated service keeps alerting.
//
// Configured by SERVICE_HEARTBEATS="name=redis_key@max_age_seconds,...".

const serviceHeartbeatRecoveryHold = 60 * time.Second

type serviceHeartbeat struct {
	Name   string
	Key    string
	MaxAge time.Duration
}

func parseServiceHeartbeats(raw string) []serviceHeartbeat {
	items := strings.Split(raw, ",")
	specs := make([]serviceHeartbeat, 0, len(items))
	seen := map[string]struct{}{}
	for _, item := range items {
		name, rest, ok := strings.Cut(strings.TrimSpace(item), "=")
		if !ok {
			continue
		}
		key, rawAge, ok := strings.Cut(rest, "@")
		seconds, err := strconv.Atoi(strings.TrimSpace(rawAge))
		name, key = strings.TrimSpace(name), strings.TrimSpace(key)
		if !ok || err != nil || seconds <= 0 || name == "" || key == "" {
			slog.Warn("notifier.heartbeat.invalid_spec", "spec", item)
			continue
		}
		if _, dup := seen[name]; dup {
			continue
		}
		seen[name] = struct{}{}
		specs = append(specs, serviceHeartbeat{Name: name, Key: key, MaxAge: time.Duration(seconds) * time.Second})
	}
	return specs
}

func heartbeatStateKey(name, field string) string {
	return "notifier:heartbeat:" + name + ":" + field
}

func maintenanceKey(name string) string {
	return "notifier:maintenance:" + name
}

// heartbeatProblem returns "" when healthy, otherwise a short reason.
func heartbeatProblem(fields map[string]string, maxAge time.Duration, now time.Time) string {
	if len(fields) == 0 {
		return "heartbeat missing"
	}
	generatedAt, err := time.Parse(time.RFC3339Nano, fields["generated_at"])
	if err != nil {
		return "heartbeat has no valid generated_at"
	}
	age := now.Sub(generatedAt)
	if age > maxAge {
		return fmt.Sprintf("heartbeat %ds old", int(age.Seconds()))
	}
	if status := fields["status"]; status != "ok" && status != "starting" {
		if last := fields["last_error"]; last != "" {
			return fmt.Sprintf("status=%s (%s)", status, truncate(last, 160))
		}
		return "status=" + status
	}
	return ""
}

func truncate(value string, limit int) string {
	if len(value) <= limit {
		return value
	}
	return value[:limit] + "..."
}

func (n *Notifier) reportServiceHeartbeats(ctx context.Context) {
	for _, spec := range n.heartbeats {
		n.checkServiceHeartbeat(ctx, spec, time.Now().UTC())
	}
}

func (n *Notifier) checkServiceHeartbeat(ctx context.Context, spec serviceHeartbeat, now time.Time) {
	inMaintenance, err := n.rdb.Exists(ctx, maintenanceKey(spec.Name)).Result()
	if err != nil {
		slog.Warn("notifier.heartbeat.maintenance_read_failed", "service", spec.Name, "err", err)
		return
	}
	if inMaintenance > 0 {
		return
	}
	fields, err := n.rdb.HGetAll(ctx, spec.Key).Result()
	if err != nil {
		slog.Warn("notifier.heartbeat.read_failed", "service", spec.Name, "err", err)
		return
	}
	unhealthySince := heartbeatStateKey(spec.Name, "unhealthy_since")
	healthySince := heartbeatStateKey(spec.Name, "healthy_since")
	alerted := heartbeatStateKey(spec.Name, "alerted")

	if problem := heartbeatProblem(fields, spec.MaxAge, now); problem != "" {
		_ = n.rdb.Del(ctx, healthySince).Err()
		if _, setErr := n.rdb.SetNX(ctx, unhealthySince, now.Unix(), 0).Result(); setErr != nil {
			slog.Warn("notifier.heartbeat.mark_failed", "service", spec.Name, "err", setErr)
			return
		}
		since, getErr := n.rdb.Get(ctx, unhealthySince).Int64()
		if getErr != nil || now.Unix()-since < int64(spec.MaxAge.Seconds()) {
			return // debounce: must stay unhealthy for one full limit
		}
		claimed, claimErr := n.rdb.SetNX(ctx, alerted, now.Unix(), 0).Result()
		if claimErr != nil || !claimed {
			return
		}
		message := fmt.Sprintf("🔴 Schurfer %s unhealthy: %s", spec.Name, problem)
		if pubErr := n.publishEnvelope(ctx, "notifier", "service.unhealthy", "critical",
			"service_unhealthy_"+spec.Name+"_"+strconv.FormatInt(since, 10), message,
			map[string]any{"service": spec.Name, "reason": problem}); pubErr != nil {
			slog.Warn("notifier.heartbeat.alert_failed", "service", spec.Name, "err", pubErr)
			_ = n.rdb.Del(ctx, alerted).Err()
			return
		}
		slog.Warn("notifier.heartbeat.unhealthy", "service", spec.Name, "reason", problem)
		return
	}

	_ = n.rdb.Del(ctx, unhealthySince).Err()
	isAlerted, err := n.rdb.Exists(ctx, alerted).Result()
	if err != nil || isAlerted == 0 {
		_ = n.rdb.Del(ctx, healthySince).Err()
		return
	}
	if _, setErr := n.rdb.SetNX(ctx, healthySince, now.Unix(), 0).Result(); setErr != nil {
		return
	}
	since, err := n.rdb.Get(ctx, healthySince).Int64()
	if err != nil || now.Unix()-since < int64(serviceHeartbeatRecoveryHold.Seconds()) {
		return
	}
	if pubErr := n.publishEnvelope(ctx, "notifier", "service.recovered", "info",
		"service_recovered_"+spec.Name+"_"+strconv.FormatInt(now.Unix(), 10),
		fmt.Sprintf("🟢 Schurfer %s recovered", spec.Name),
		map[string]any{"service": spec.Name}); pubErr != nil {
		slog.Warn("notifier.heartbeat.recovery_failed", "service", spec.Name, "err", pubErr)
		return
	}
	_ = n.rdb.Del(ctx, alerted, healthySince).Err()
	slog.Info("notifier.heartbeat.recovered", "service", spec.Name)
}

func serviceHeartbeatsFromEnv() []serviceHeartbeat {
	return parseServiceHeartbeats(os.Getenv("SERVICE_HEARTBEATS"))
}
