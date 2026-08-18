package notifier

import (
	"context"
	"encoding/json"
	"fmt"
	"github.com/redis/go-redis/v9"
	"log/slog"
	"strconv"
	"strings"
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

// momentumFlowPaperOpen is one momentum_flow_paper_v1 (or a sibling contract
// -- Binance's own venue expansion, or the lev3 sizing expansion, see
// momentum_flow_paper_contract.py) probe that just opened a real (paper)
// long entry. Leverage/NotionalUSD come from the specific sibling contract
// that opened this probe (joined via its own paper_version's contract_json),
// not hardcoded, so the alert stays accurate as more sibling contracts are
// added. Return60mPct/OIGrowth60mPct/BuyImbalance15m are the triggering
// WATCH decision's own feature snapshot (LEFT JOINed on watch_id, so a
// pruned/archived evaluation row degrades this alert to omitting the
// signal line rather than failing to claim/send it at all).
type momentumFlowPaperOpen struct {
	PaperID         string
	Symbol          string
	Exchange        string
	EntryVWAP       float64
	NotionalUSD     float64
	Leverage        int
	WatchDecisionAt time.Time
	EntryAt         time.Time
	Return60mPct    *float64
	OIGrowth60mPct  *float64
	BuyImbalance15m *float64
}

// momentumFlowPaperOutcome is one probe's own FINAL outcome -- the row at
// its own maximum outcome_horizons_minutes (240 for both the live Bybit
// and Binance contracts today, see momentum_flow_paper_contract.py's own
// OUTCOME_HORIZONS_MINUTES), not one of the five earlier intermediate
// horizon reads. Reporting only the final horizon (not all six) keeps
// this from being six times noisier than the entry alert it pairs with.
// ExitAt is nullable: a max-hold exit that could not get a clean quote
// before its deadline becomes exit_unresolved (position_status <> 'closed'),
// which the schema's own momentum_flow_paper_exit_shape CHECK requires
// exit_at to be NULL for -- see docs/research/momentum-flow-paper-v1.md's
// own "Point-in-time and failure rules".
type momentumFlowPaperOutcome struct {
	PaperID        string
	Symbol         string
	Exchange       string
	HorizonMinutes int
	NetReturnPct   float64
	NetPnLUSD      float64
	EntryAt        time.Time
	ExitAt         *time.Time
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
		SELECT p.paper_id, p.symbol, p.exchange, p.entry_vwap, p.entry_filled_notional_usd,
		       coalesce((r.contract_json->>'leverage')::int, 1),
		       p.watch_decision_at, p.entry_at,
		       e.price_return_60m_pct, e.oi_growth_60m_pct, e.buy_imbalance_15m
		FROM app.momentum_flow_paper_probes p
		JOIN app.momentum_flow_paper_runs r ON r.paper_version = p.paper_version
		LEFT JOIN timeseries.momentum_flow_watch_evaluations_1m e
		  ON e.watch_version = p.watch_version AND e.exchange = p.exchange
		  AND e.market_type = p.market_type AND e.symbol = p.symbol AND e.watch_id = p.watch_id
		WHERE p.entry_status = 'opened'
		  AND p.entry_at >= now() - (interval '1 second' * $1::double precision)`,
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
			&open.Leverage, &open.WatchDecisionAt, &open.EntryAt,
			&open.Return60mPct, &open.OIGrowth60mPct, &open.BuyImbalance15m,
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
		       o.net_return_pct, o.net_pnl_usd, p.entry_at, p.exit_at
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
			&outcome.NetReturnPct, &outcome.NetPnLUSD, &outcome.EntryAt, &outcome.ExitAt,
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
	n.maybeSendPaperTradesSummary(ctx)
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
	message := formatMomentumFlowOpenMessage(open)
	if err := n.publishEnvelope(ctx, "scanner", "momentum.flow.alert", "trade", "momentum_flow_"+strconv.FormatInt(time.Now().UnixNano(), 10), message, nil); err != nil {
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
	message := formatMomentumFlowOutcomeMessage(outcome)
	if err := n.publishEnvelope(ctx, "scanner", "momentum.flow.alert", "trade", "momentum_flow_"+strconv.FormatInt(time.Now().UnixNano(), 10), message, nil); err != nil {
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

// formatMomentumFlowOpenMessage names the strategy that opened this probe
// and why (the triggering WATCH signal's own feature snapshot), instead of a
// generic "research probe" label that says nothing about either -- and
// states real capital at risk explicitly rather than leaving a reader to
// infer margin from a bare notional + leverage pair. No em/en-dash: plain
// ASCII "·" separators and "->"-free "→" for direction, matching this
// codebase's own existing pump-alert and execution-notify conventions.
func formatMomentumFlowOpenMessage(open momentumFlowPaperOpen) string {
	return fmt.Sprintf(
		"🔭 MOMENTUM-FLOW LONG · %s (%s)\nEntry %s · %s\n%s\nDetected %s · opened %s (+%s)",
		open.Symbol, open.Exchange, formatPrice(open.EntryVWAP), momentumFlowSizeLine(open.NotionalUSD, open.Leverage),
		momentumFlowSignalLine(open.Return60mPct, open.OIGrowth60mPct, open.BuyImbalance15m),
		formatUTC(open.WatchDecisionAt), formatUTC(open.EntryAt), open.EntryAt.Sub(open.WatchDecisionAt).Round(time.Second),
	)
}

// formatMomentumFlowOutcomeMessage mirrors formatMomentumFlowOpenMessage's
// own naming and separator conventions for the paired close alert.
func formatMomentumFlowOutcomeMessage(outcome momentumFlowPaperOutcome) string {
	icon := "🟢"
	if outcome.NetReturnPct < 0 {
		icon = "🔴"
	}
	return fmt.Sprintf(
		"%s MOMENTUM-FLOW LONG CLOSED · %s (%s)\n%dmin result: %+.2f%% (%s)\n%s",
		icon, outcome.Symbol, outcome.Exchange, outcome.HorizonMinutes,
		outcome.NetReturnPct, formatSignedUSD(outcome.NetPnLUSD),
		momentumFlowTimingLine(outcome.EntryAt, outcome.ExitAt),
	)
}

// formatUTC gives a readable, unambiguous timestamp for a Telegram audience
// that reads alerts from multiple timezones -- always UTC, always labeled,
// never a bare local-feeling "HH:MM" that silently means different things
// to different readers.
func formatUTC(t time.Time) string {
	return t.UTC().Format("15:04:05 UTC")
}

// momentumFlowSizeLine names the real capital committed the same way
// regardless of which sibling paper contract opened this probe: leverage=1
// (FROZEN_PAPER_CONTRACT) reads as its own notional with "no leverage";
// leverage>1 (e.g. LEVERAGED_PAPER_CONTRACT) also states the margin that
// notional implies, so a reader never has to do the division themselves to
// see how much real capital is actually at risk.
func momentumFlowSizeLine(notionalUSD float64, leverage int) string {
	if leverage <= 1 {
		return fmt.Sprintf("$%.0f notional, no leverage", notionalUSD)
	}
	margin := notionalUSD / float64(leverage)
	return fmt.Sprintf("$%.0f notional, %dx leverage ($%.0f margin)", notionalUSD, leverage, margin)
}

// momentumFlowSignalLine reports the triggering WATCH decision's own feature
// snapshot -- the actual reason this symbol qualified -- rather than a bare
// "research probe" label that says nothing about why. Any feature missing
// (a pruned/archived evaluation row, see the LEFT JOIN in
// ReadNewMomentumFlowPaperOpens) is simply omitted from the line rather than
// shown as a fabricated zero.
func momentumFlowSignalLine(return60mPct, oiGrowth60mPct, buyImbalance15m *float64) string {
	parts := make([]string, 0, 3)
	if return60mPct != nil {
		parts = append(parts, fmt.Sprintf("60m return %+.1f%%", *return60mPct))
	}
	if oiGrowth60mPct != nil {
		parts = append(parts, fmt.Sprintf("OI growth 60m %+.1f%%", *oiGrowth60mPct))
	}
	if buyImbalance15m != nil {
		parts = append(parts, fmt.Sprintf("buy imbalance 15m %+.2f", *buyImbalance15m))
	}
	if len(parts) == 0 {
		return "Signal: unavailable"
	}
	return "Signal: " + strings.Join(parts, " · ")
}

// momentumFlowTimingLine reports when the position opened and (if resolved
// with a clean quote) closed. exitAt is nil for exit_unresolved outcomes --
// a max-hold exit that could not get a clean quote before its deadline, see
// momentumFlowPaperOutcome's own doc comment -- in which case this only
// reports the open time rather than fabricating a close time.
func momentumFlowTimingLine(entryAt time.Time, exitAt *time.Time) string {
	if exitAt == nil {
		return fmt.Sprintf("Opened %s", formatUTC(entryAt))
	}
	return fmt.Sprintf("Opened %s → closed %s", formatUTC(entryAt), formatUTC(*exitAt))
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

var extractSummaryScript = redis.NewScript(`
local list_key = KEYS[1]
local time_key = KEYS[2]
local current_time = tonumber(ARGV[1])
local interval = tonumber(ARGV[2])

local last_time = tonumber(redis.call('GET', time_key) or '0')

if (current_time - last_time) < interval then
    return {}
end

local items = redis.call('LRANGE', list_key, 0, -1)
if #items > 0 then
    redis.call('DEL', list_key)
end
redis.call('SET', time_key, current_time)

return items
`)

func (n *Notifier) maybeSendPaperTradesSummary(ctx context.Context) {
	const summaryInterval = int64(4 * 3600)
	res, err := extractSummaryScript.Run(ctx, n.rdb,
		[]string{"notifier:paper_trades_summary_list", "notifier:paper_trades_summary_last"},
		time.Now().Unix(), summaryInterval,
	).StringSlice()

	if err != nil || len(res) == 0 {
		return
	}

	type summary struct {
		Count  int
		SumPnL float64
	}
	totals := make(map[string]*summary)

	for _, raw := range res {
		var o momentumFlowPaperOutcome
		if err := json.Unmarshal([]byte(raw), &o); err == nil {
			if totals[o.Exchange] == nil {
				totals[o.Exchange] = &summary{}
			}
			totals[o.Exchange].Count++
			totals[o.Exchange].SumPnL += o.NetReturnPct
		}
	}

	msg := "📝 **Paper Trades Summary (Last 4h)**\n\n"
	for ex, s := range totals {
		msg += fmt.Sprintf("• **%s**: %d trades, %+.2f%% net\n", ex, s.Count, s.SumPnL)
	}

	_ = n.publishEnvelope(ctx,
		"scanner",
		"momentum.flow.summary",
		"trade",
		"momentum_flow_summary_"+strconv.FormatInt(time.Now().UnixNano(), 10),
		msg,
		nil,
	)
}
