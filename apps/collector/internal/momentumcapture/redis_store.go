package momentumcapture

import (
	"context"
	"errors"
	"fmt"
	"strconv"
	"strings"
	"time"

	"github.com/redis/go-redis/v9"
)

// HealthKeyPrefix is read by `make momentum-capture-health`/`make
// prod-momentum-capture-health`, the same redis-cli HGETALL pattern
// hotset.RedisStore/orderflow already use for their own health keys.
// HealthKey appends the exchange so multiple venues' momentum-capture
// processes (Bybit today, Binance once feat/binance-momentum-capture-v1
// activates) each get their own key -- before this, every venue shared
// the one unparameterized key, so a second venue's process would
// silently overwrite the first's snapshot instead of coexisting with it.
const HealthKeyPrefix = "market:momentumcapture:health"

func HealthKey(exchange string) string {
	return HealthKeyPrefix + ":" + exchange
}

// healthTTL bounds how long a stale key can be read as current: if the
// process dies, the key expires instead of serving a frozen last-known-good
// snapshot forever. Comfortably above healthInterval (cmd/momentumcapture)
// so a single missed publish doesn't flip the key to missing.
const healthTTL = 30 * time.Second

// missingSymbolsSample bounds how many symbol names StoreHealth writes into
// a single Redis hash field: SymbolsMissingTicker/Trades can hold the
// entire universe (all 735+ symbols) right after a fresh start, and
// redis-cli HGETALL is read by a human, not parsed by a dashboard yet. The
// _count field always carries the true total regardless of this cap.
const missingSymbolsSample = 20

type RedisStore struct {
	client *redis.Client
}

func NewRedisStore(client *redis.Client) (*RedisStore, error) {
	if client == nil {
		return nil, errors.New("redis client is required")
	}
	return &RedisStore{client: client}, nil
}

// StoreHealth publishes a Health snapshot to HealthKey(health.Exchange),
// replacing whatever was there before (HSet on an existing key does not
// remove fields that are absent from values, but Health is built fresh
// from scratch and in full on every call, so every field this schema
// defines is always present). Fails closed if Exchange is unset: silently
// falling back to some default would recreate exactly the shared-key
// masking risk HealthKey's own per-exchange scoping exists to prevent.
func (store *RedisStore) StoreHealth(ctx context.Context, health Health) error {
	if health.Exchange == "" {
		return errors.New("store momentum-capture health: Exchange is required")
	}
	values := map[string]any{
		"schema_version": 2,
		"exchange":       health.Exchange,
		"status":         health.Status,
		"started_at_ms":  unixMilliOrZero(health.StartedAt),
		"updated_at_ms":  unixMilliOrZero(health.UpdatedAt),

		"last_bar_at_ms":     unixMilliOrZero(health.LastBarAt),
		"last_persist_at_ms": unixMilliOrZero(health.LastPersistAt),

		"universe_snapshot_at_ms":  unixMilliOrZero(health.UniverseSnapshotAt),
		"universe_age_seconds":     strconv.FormatFloat(health.UniverseAgeSeconds, 'f', 1, 64),
		"subscribed_symbols":       health.SubscribedSymbols,
		"current_exchange_symbols": health.CurrentExchangeSymbols,
		"frozen_universe_hash":     health.FrozenUniverseHash,
		"live_universe_hash":       health.LiveUniverseHash,
		"universe_stale":           health.UniverseStale,
		"ready_symbols":            health.ReadySymbols,

		"added_since_start_count":    len(health.AddedSinceStart),
		"added_since_start_sample":   sampleJoin(health.AddedSinceStart, missingSymbolsSample),
		"removed_since_start_count":  len(health.RemovedSinceStart),
		"removed_since_start_sample": sampleJoin(health.RemovedSinceStart, missingSymbolsSample),

		"symbols_missing_ticker_count":  len(health.SymbolsMissingTicker),
		"symbols_missing_ticker_sample": sampleJoin(health.SymbolsMissingTicker, missingSymbolsSample),
		"symbols_missing_trades_count":  len(health.SymbolsMissingTrades),
		"symbols_missing_trades_sample": sampleJoin(health.SymbolsMissingTrades, missingSymbolsSample),

		"catalog_items_total":           health.CatalogItemsTotal,
		"crypto_perpetuals_included":    health.CryptoPerpetualsIncluded,
		"standard_crypto_included":      health.StandardCryptoIncluded,
		"innovation_crypto_included":    health.InnovationCryptoIncluded,
		"dated_futures_excluded":        health.DatedFuturesExcluded,
		"stock_perpetuals_excluded":     health.StockPerpetualsExcluded,
		"commodity_perpetuals_excluded": health.CommodityPerpetualsExcluded,
		"unknown_contract_excluded":     health.UnknownContractExcluded,
		"unknown_symbol_type_excluded":  health.UnknownSymbolTypeExcluded,
		"invalid_instrument_excluded":   health.InvalidInstrumentExcluded,
		"non_usdt_excluded":             health.NonUSDTExcluded,
		"non_trading_excluded":          health.NonTradingExcluded,

		"input_queue_depth":        health.InputQueueDepth,
		"input_queue_peak":         health.InputQueuePeak,
		"input_queue_drops_total":  health.InputQueueDropsTotal,
		"bars_completed_total":     health.BarsCompletedTotal,
		"late_events_total":        health.LateEventsTotal,
		"ticker_gap_total":         health.TickerGapTotal,
		"last_discontinuity_at_ms": unixMilliOrZero(health.LastDiscontinuityAt),
		"last_discontinuity_for":   health.LastDiscontinuityFor,

		"trade_reconnect_total":    health.TradeReconnectTotal,
		"trade_read_timeout_total": health.TradeReadTimeoutTotal,

		"nats_disconnect_total":    health.NATSDisconnectTotal,
		"nats_reconnect_total":     health.NATSReconnectTotal,
		"nats_slow_consumer_total": health.NATSSlowConsumerTotal,
		"nats_dropped_total":       health.NATSDroppedTotal,

		"writer_queue_depth":          health.WriterQueueDepth,
		"writer_queue_peak":           health.WriterQueuePeak,
		"writer_queue_drops_total":    health.WriterQueueDropsTotal,
		"bars_persisted_total":        health.BarsPersistedTotal,
		"persist_errors_total":        health.PersistErrorsTotal,
		"persist_retries_total":       health.PersistRetriesTotal,
		"rows_written_total":          health.RowsWrittenTotal,
		"payload_hash_mismatch_total": health.PayloadHashMismatchTotal,
		"projected_bytes_per_day":     strconv.FormatFloat(health.ProjectedBytesPerDay, 'f', 0, 64),

		"trade_lag_max_ms":  health.TradeLagMaxMs,
		"ticker_lag_max_ms": health.TickerLagMaxMs,

		"trade_receive_to_handle_count":   health.TradeReceiveToHandleCount,
		"trade_receive_to_handle_p95_us":  health.TradeReceiveToHandleP95Us,
		"trade_receive_to_handle_p99_us":  health.TradeReceiveToHandleP99Us,
		"trade_receive_to_handle_max_us":  health.TradeReceiveToHandleMaxUs,
		"trade_handler_count":             health.TradeHandlerCount,
		"trade_handler_p95_us":            health.TradeHandlerP95Us,
		"trade_handler_p99_us":            health.TradeHandlerP99Us,
		"trade_handler_max_us":            health.TradeHandlerMaxUs,
		"ticker_receive_to_handle_count":  health.TickerReceiveToHandleCount,
		"ticker_receive_to_handle_p95_us": health.TickerReceiveToHandleP95Us,
		"ticker_receive_to_handle_p99_us": health.TickerReceiveToHandleP99Us,
		"ticker_receive_to_handle_max_us": health.TickerReceiveToHandleMaxUs,
		"ticker_handler_count":            health.TickerHandlerCount,
		"ticker_handler_p95_us":           health.TickerHandlerP95Us,
		"ticker_handler_p99_us":           health.TickerHandlerP99Us,
		"ticker_handler_max_us":           health.TickerHandlerMaxUs,
		"flush_count":                     health.FlushCount,
		"flush_p95_us":                    health.FlushP95Us,
		"flush_p99_us":                    health.FlushP99Us,
		"flush_max_us":                    health.FlushMaxUs,
		"health_publish_count":            health.HealthPublishCount,
		"health_publish_p95_us":           health.HealthPublishP95Us,
		"health_publish_p99_us":           health.HealthPublishP99Us,
		"health_publish_max_us":           health.HealthPublishMaxUs,
	}
	key := HealthKey(health.Exchange)
	pipe := store.client.Pipeline()
	pipe.HSet(ctx, key, values)
	pipe.Expire(ctx, key, healthTTL)
	if _, err := pipe.Exec(ctx); err != nil {
		return fmt.Errorf("store momentum-capture health: %w", err)
	}
	return nil
}

func unixMilliOrZero(t time.Time) int64 {
	if t.IsZero() {
		return 0
	}
	return t.UnixMilli()
}

func sampleJoin(items []string, max int) string {
	if len(items) > max {
		items = items[:max]
	}
	return strings.Join(items, ",")
}
