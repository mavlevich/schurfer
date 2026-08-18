package notifier

import (
	"context"
	"fmt"
	"log/slog"
	"time"
)

const (
	redisKeySourceLeadHealthAlerted = "notifier:source_lead_health_alerted"
	redisKeySourceLeadFailureSeen   = "notifier:source_lead_failure_seen:"
	sourceLeadHealthTimeout         = 2 * time.Second
)

type sourceLeadHealth struct {
	StaleCollecting      int
	CriticalAbandonedIDs []int64
}

type sourceLeadHealthReader interface {
	ReadSourceLeadHealth(context.Context) (sourceLeadHealth, error)
}

func (r *postgresAlertRecorder) ReadSourceLeadHealth(
	ctx context.Context,
) (sourceLeadHealth, error) {
	readCtx, cancel := context.WithTimeout(ctx, sourceLeadHealthTimeout)
	defer cancel()

	var health sourceLeadHealth
	err := r.pool.QueryRow(readCtx, `
		SELECT
			count(*) FILTER (
				WHERE status = 'collecting'
				  AND capture_started_at < now() - interval '10 minutes'
			),
			coalesce(array_agg(id ORDER BY id) FILTER (
				WHERE status = 'abandoned'
				  AND capture_completed_at >= now() - interval '24 hours'
				  AND (
					error = 'capture_queue_full'
					OR error = 'capture_worker_shutdown_timeout'
					OR error LIKE 'capture_worker_failed:%'
				  )
			), '{}'::bigint[])
		FROM app.source_lead_captures
		WHERE capture_version = 'source_lead_prospective_capture_v1'`,
	).Scan(&health.StaleCollecting, &health.CriticalAbandonedIDs)
	return health, err
}

func (n *Notifier) reportSourceLeadHealth(ctx context.Context) {
	if n.sourceLeadHealth == nil {
		return
	}
	health, err := n.sourceLeadHealth.ReadSourceLeadHealth(ctx)
	if err != nil {
		slog.Warn("notifier.source_lead.health_read_failed", "err", err)
		return
	}
	for _, captureID := range health.CriticalAbandonedIDs {
		n.reportSourceLeadCriticalFailure(ctx, captureID)
	}
	n.reportSourceLeadStaleHealth(ctx, health.StaleCollecting)
}

func (n *Notifier) reportSourceLeadStaleHealth(ctx context.Context, staleCollecting int) {
	if staleCollecting > 0 {
		claimed, claimErr := n.rdb.SetNX(
			ctx,
			redisKeySourceLeadHealthAlerted,
			time.Now().Unix(),
			0,
		).Result()
		if claimErr != nil {
			slog.Warn("notifier.source_lead.claim_failed", "err", claimErr)
			return
		}
		if !claimed {
			return
		}
		message := fmt.Sprintf(
			"🔴 Schurfer source-lead capture unhealthy: stale_collecting=%d",
			staleCollecting,
		)
		if sendErr := n.publishEnvelope(ctx, "scanner", "source.unhealthy", "critical", "source_unhealthy", message, nil); sendErr != nil {
			slog.Warn("notifier.source_lead.alert_failed", "err", sendErr)
			if delErr := n.rdb.Del(ctx, redisKeySourceLeadHealthAlerted).Err(); delErr != nil {
				slog.Warn("notifier.source_lead.claim_release_failed", "err", delErr)
			}
			return
		}
		slog.Warn(
			"notifier.source_lead.unhealthy",
			"stale", staleCollecting,
		)
		return
	}

	removed, err := n.rdb.Del(ctx, redisKeySourceLeadHealthAlerted).Result()
	if err != nil {
		slog.Warn("notifier.source_lead.clear_failed", "err", err)
		return
	}
	if removed == 0 {
		return
	}
	if err := n.publishEnvelope(
		ctx,
		"scanner",
		"source.recovered",
		"info",
		"source_recovered_"+fmt.Sprintf("%d", time.Now().Unix()),
		"🟢 Schurfer source-lead capture recovered",
		nil,
	); err != nil {
		slog.Warn("notifier.source_lead.recovery_failed", "err", err)
		if setErr := n.rdb.Set(
			ctx,
			redisKeySourceLeadHealthAlerted,
			time.Now().Unix(),
			0,
		).Err(); setErr != nil {
			slog.Warn("notifier.source_lead.flag_restore_failed", "err", setErr)
		}
		return
	}
	slog.Info("notifier.source_lead.recovered")
}

func (n *Notifier) reportSourceLeadCriticalFailure(ctx context.Context, captureID int64) {
	if captureID <= 0 {
		return
	}
	key := fmt.Sprintf("%s%d", redisKeySourceLeadFailureSeen, captureID)
	claimed, err := n.rdb.SetNX(ctx, key, time.Now().Unix(), seenTTL).Result()
	if err != nil {
		slog.Warn("notifier.source_lead.failure_claim_failed", "err", err)
		return
	}
	if !claimed {
		return
	}
	message := fmt.Sprintf(
		"🔴 Schurfer source-lead capture failed: capture_id=%d",
		captureID,
	)
	if err := n.publishEnvelope(ctx, "scanner", "source.unhealthy", "critical", "source_unhealthy", message, nil); err != nil {
		slog.Warn("notifier.source_lead.failure_alert_failed", "err", err)
		if delErr := n.rdb.Del(ctx, key).Err(); delErr != nil {
			slog.Warn("notifier.source_lead.failure_claim_release_failed", "err", delErr)
		}
		return
	}
	slog.Warn("notifier.source_lead.capture_failed", "capture_id", captureID)
}
