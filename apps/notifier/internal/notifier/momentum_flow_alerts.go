package notifier

import (
	"context"
	"fmt"
	"log/slog"
	"time"
)

const (
	// momentumFlowLookback bounds every poll's own query window: wide enough
	// (relative to the notifier's own ~60s tick interval) that a single
	// slow tick or a brief Postgres hiccup can never let a row fall
	// between two polls unseen, small enough that the query itself stays
	// cheap regardless of how large these tables grow over the life of the
	// discovery window. Actual once-only delivery is enforced by the
	// per-row Redis SetNX dedup below, not by this window -- the window
	// only has to be a superset of "definitely not missed", overlap
	// across polls is expected and harmless.
	momentumFlowLookback               = 10 * time.Minute
	momentumFlowDBTimeout              = 3 * time.Second
	redisKeyMomentumFlowOpenSeenPfx    = "notifier:momentum_flow:open_seen:"
	redisKeyMomentumFlowOutcomeSeenPfx = "notifier:momentum_flow:outcome_seen:"
)

// momentumFlowPaperOpen is one momentum_flow_paper_v1 (or its Binance
// counterpart) probe that just opened a real (paper) long entry.
type momentumFlowPaperOpen struct {
	PaperID     string
	Symbol      string
	Exchange    string
	EntryVWAP   float64
	NotionalUSD float64
}

// momentumFlowPaperOutcome is one probe's own FINAL outcome -- the row at
// its own maximum outcome_horizons_minutes (240 for both the live Bybit
// and Binance contracts today, see momentum_flow_paper_contract.py's own
// OUTCOME_HORIZONS_MINUTES), not one of the five earlier intermediate
// horizon reads. Reporting only the final horizon (not all six) keeps
// this from being six times noisier than the entry alert it pairs with.
type momentumFlowPaperOutcome struct {
	PaperID        string
	Symbol         string
	Exchange       string
	HorizonMinutes int
	NetReturnPct   float64
	NetPnLUSD      float64
}

type momentumFlowReader interface {
	ReadNewMomentumFlowPaperOpens(context.Context) ([]momentumFlowPaperOpen, error)
	ReadNewMomentumFlowPaperOutcomes(context.Context) ([]momentumFlowPaperOutcome, error)
}

func (r *postgresAlertRecorder) ReadNewMomentumFlowPaperOpens(
	ctx context.Context,
) ([]momentumFlowPaperOpen, error) {
	readCtx, cancel := context.WithTimeout(ctx, momentumFlowDBTimeout)
	defer cancel()

	rows, err := r.pool.Query(readCtx, `
		SELECT paper_id, symbol, exchange, entry_vwap, entry_filled_notional_usd
		FROM app.momentum_flow_paper_probes
		WHERE entry_status = 'opened'
		  AND entry_at >= now() - (interval '1 second' * $1::double precision)`,
		momentumFlowLookback.Seconds(),
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var opens []momentumFlowPaperOpen
	for rows.Next() {
		var open momentumFlowPaperOpen
		if err := rows.Scan(
			&open.PaperID, &open.Symbol, &open.Exchange, &open.EntryVWAP, &open.NotionalUSD,
		); err != nil {
			return nil, err
		}
		opens = append(opens, open)
	}
	return opens, rows.Err()
}

// ReadNewMomentumFlowPaperOutcomes finds each probe's own FINAL outcome
// row -- the one whose horizon_minutes equals that SAME probe's own
// maximum horizon_minutes, computed per-probe rather than hardcoding 240
// here, so this keeps working correctly even if a future contract change
// (a colleague's own doc comment on PaperContract.max_hold_minutes) picks
// a different final horizon.
func (r *postgresAlertRecorder) ReadNewMomentumFlowPaperOutcomes(
	ctx context.Context,
) ([]momentumFlowPaperOutcome, error) {
	readCtx, cancel := context.WithTimeout(ctx, momentumFlowDBTimeout)
	defer cancel()

	rows, err := r.pool.Query(readCtx, `
		SELECT o.paper_id, p.symbol, p.exchange, o.horizon_minutes,
		       o.net_return_pct, o.net_pnl_usd
		FROM app.momentum_flow_paper_outcomes o
		JOIN app.momentum_flow_paper_probes p ON p.paper_id = o.paper_id
		WHERE o.status = 'complete'
		  AND o.updated_at >= now() - (interval '1 second' * $1::double precision)
		  AND o.horizon_minutes = (
		      SELECT max(o2.horizon_minutes)
		      FROM app.momentum_flow_paper_outcomes o2
		      WHERE o2.paper_id = o.paper_id
		  )`,
		momentumFlowLookback.Seconds(),
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var outcomes []momentumFlowPaperOutcome
	for rows.Next() {
		var outcome momentumFlowPaperOutcome
		if err := rows.Scan(
			&outcome.PaperID, &outcome.Symbol, &outcome.Exchange, &outcome.HorizonMinutes,
			&outcome.NetReturnPct, &outcome.NetPnLUSD,
		); err != nil {
			return nil, err
		}
		outcomes = append(outcomes, outcome)
	}
	return outcomes, rows.Err()
}

func (n *Notifier) reportMomentumFlow(ctx context.Context) {
	if n.momentumFlow == nil {
		return
	}
	opens, err := n.momentumFlow.ReadNewMomentumFlowPaperOpens(ctx)
	if err != nil {
		slog.Warn("notifier.momentum_flow.opens_read_failed", "err", err)
	}
	for _, open := range opens {
		n.reportMomentumFlowPaperOpen(ctx, open)
	}

	outcomes, err := n.momentumFlow.ReadNewMomentumFlowPaperOutcomes(ctx)
	if err != nil {
		slog.Warn("notifier.momentum_flow.outcomes_read_failed", "err", err)
	}
	for _, outcome := range outcomes {
		n.reportMomentumFlowPaperOutcome(ctx, outcome)
	}
}

func (n *Notifier) reportMomentumFlowPaperOpen(ctx context.Context, open momentumFlowPaperOpen) {
	key := redisKeyMomentumFlowOpenSeenPfx + open.PaperID
	claimed, err := n.rdb.SetNX(ctx, key, time.Now().Unix(), seenTTL).Result()
	if err != nil {
		slog.Warn("notifier.momentum_flow.open_claim_failed", "err", err)
		return
	}
	if !claimed {
		return
	}
	message := fmt.Sprintf(
		"🔭 WATCH→PAPER: LONG %s (%s)\nEntry VWAP: %s\nSize: $%.0f — research probe, not a live position",
		open.Symbol, open.Exchange, formatPrice(open.EntryVWAP), open.NotionalUSD,
	)
	if err := sendMessage(ctx, message, n.cfg.BotToken, n.cfg.ChatID); err != nil {
		slog.Warn("notifier.momentum_flow.open_alert_failed", "err", err)
		if delErr := n.rdb.Del(ctx, key).Err(); delErr != nil {
			slog.Warn("notifier.momentum_flow.open_claim_release_failed", "err", delErr)
		}
		return
	}
	slog.Info("notifier.momentum_flow.opened", "symbol", open.Symbol, "exchange", open.Exchange)
}

func (n *Notifier) reportMomentumFlowPaperOutcome(
	ctx context.Context, outcome momentumFlowPaperOutcome,
) {
	key := redisKeyMomentumFlowOutcomeSeenPfx + outcome.PaperID
	claimed, err := n.rdb.SetNX(ctx, key, time.Now().Unix(), seenTTL).Result()
	if err != nil {
		slog.Warn("notifier.momentum_flow.outcome_claim_failed", "err", err)
		return
	}
	if !claimed {
		return
	}
	icon := "🟢"
	if outcome.NetReturnPct < 0 {
		icon = "🔴"
	}
	message := fmt.Sprintf(
		"%s PAPER closed: LONG %s (%s)\n%dmin result: %+.2f%% (%s) — research probe, not a live position",
		icon, outcome.Symbol, outcome.Exchange, outcome.HorizonMinutes,
		outcome.NetReturnPct, formatSignedUSD(outcome.NetPnLUSD),
	)
	if err := sendMessage(ctx, message, n.cfg.BotToken, n.cfg.ChatID); err != nil {
		slog.Warn("notifier.momentum_flow.outcome_alert_failed", "err", err)
		if delErr := n.rdb.Del(ctx, key).Err(); delErr != nil {
			slog.Warn("notifier.momentum_flow.outcome_claim_release_failed", "err", delErr)
		}
		return
	}
	slog.Info(
		"notifier.momentum_flow.closed",
		"symbol", outcome.Symbol, "exchange", outcome.Exchange, "net_return_pct", outcome.NetReturnPct,
	)
}

func formatPrice(price float64) string {
	if price >= 1 {
		return fmt.Sprintf("%.4f", price)
	}
	return fmt.Sprintf("%.8f", price)
}

// formatSignedUSD matches the web frontend's own dollar convention
// (apps/web/src/pages/trades/TradesPage.tsx's own fmtUsd: sign then "$"
// then the magnitude, e.g. "+$3.46"/"-$1.88"), not Go's own %+.2f$ shape
// (which would put the sign and "$" on the wrong sides of each other).
func formatSignedUSD(amount float64) string {
	sign := "+"
	if amount < 0 {
		sign = "-"
		amount = -amount
	}
	return fmt.Sprintf("%s$%.2f", sign, amount)
}
