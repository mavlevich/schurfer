package notifier

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"time"
)

const (
	redisKeySourceLeadHealthAlerted = "notifier:source_lead_health_alerted"
	redisKeySourceLeadFailureSeen   = "notifier:source_lead_failure_seen:"
	redisKeySourceLeadQuietAlerted  = "notifier:source_lead_quiet_alerted"
	// Longest gap between captures since 2026-09-03 was 5.35 h (p99 2.1 h), so
	// 6 h without any capture while the scanner is fresh is a real warning.
	sourceLeadQuietAfter    = 6 * time.Hour
	sourceLeadHealthTimeout = 2 * time.Second
)

type sourceLeadHealth struct {
	StaleCollecting      int
	CriticalAbandonedIDs []int64
	// Seconds since the newest capture row of any status; nil when none exist.
	LastCaptureAgeSeconds *float64
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
			), '{}'::bigint[]),
			extract(epoch FROM now() - max(created_at))::float8
		FROM app.source_lead_captures
		WHERE capture_version = 'source_lead_prospective_capture_v1'`,
	).Scan(&health.StaleCollecting, &health.CriticalAbandonedIDs, &health.LastCaptureAgeSeconds)
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
	n.reportSourceLeadQuiet(ctx, health.LastCaptureAgeSeconds)
}

// reportSourceLeadQuiet warns once when no capture row of any status has been
// created for sourceLeadQuietAfter while the scanner itself is fresh: the
// scanner-down case already has its own critical alert, so this catches the
// silent one (for example capture claims failing with only a log warning).
func (n *Notifier) reportSourceLeadQuiet(ctx context.Context, lastCaptureAgeSeconds *float64) {
	quiet := lastCaptureAgeSeconds != nil &&
		time.Duration(*lastCaptureAgeSeconds*float64(time.Second)) > sourceLeadQuietAfter
	if quiet && !n.scannerFresh(ctx) {
		return
	}
	if quiet {
		claimed, err := n.rdb.SetNX(ctx, redisKeySourceLeadQuietAlerted, time.Now().Unix(), 0).Result()
		if err != nil || !claimed {
			return
		}
		message := fmt.Sprintf(
			"🟠 Schurfer source-lead: no new captures for %.1f h while the scanner is running",
			*lastCaptureAgeSeconds/3600,
		)
		if err := n.publishEnvelope(ctx, "scanner", "source.quiet", "warning", "source_quiet_"+
			fmt.Sprintf("%d", time.Now().Unix()), message, nil); err != nil {
			slog.Warn("notifier.source_lead.quiet_alert_failed", "err", err)
			_ = n.rdb.Del(ctx, redisKeySourceLeadQuietAlerted).Err()
		}
		return
	}
	removed, err := n.rdb.Del(ctx, redisKeySourceLeadQuietAlerted).Result()
	if err != nil || removed == 0 {
		return
	}
	if err := n.publishEnvelope(ctx, "scanner", "source.quiet_recovered", "info",
		"source_quiet_recovered_"+fmt.Sprintf("%d", time.Now().Unix()),
		"🟢 Schurfer source-lead captures resumed", nil); err != nil {
		slog.Warn("notifier.source_lead.quiet_recovery_failed", "err", err)
		_ = n.rdb.Set(ctx, redisKeySourceLeadQuietAlerted, time.Now().Unix(), 0).Err()
	}
}

// scannerFresh reports whether pumps:latest carries a scan newer than StaleAfter.
func (n *Notifier) scannerFresh(ctx context.Context) bool {
	raw, err := n.rdb.Get(ctx, redisKeyPumps).Bytes()
	if err != nil {
		return false
	}
	var p payload
	if json.Unmarshal(raw, &p) != nil || p.Ts == 0 {
		return false
	}
	age := time.Since(time.UnixMilli(p.Ts))
	return age >= -5*time.Second && age <= n.cfg.StaleAfter
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
