package lsr

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"strconv"
	"strings"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/nats-io/nats.go"
)

type LongShortRatioReading struct {
	Symbol       string    `json:"Symbol"`
	Ratio        string    `json:"Ratio"`
	LongAccount  string    `json:"LongAccount"`
	ShortAccount string    `json:"ShortAccount"`
	EventAt      time.Time `json:"EventAt"`
}

func Subscribe(ctx context.Context, nc *nats.Conn, pool *pgxpool.Pool) error {
	sub, err := nc.SubscribeSync("binance.lsr.*")
	if err != nil {
		return fmt.Errorf("subscribe: %w", err)
	}

	go func() {
		for {
			select {
			case <-ctx.Done():
				return
			default:
			}
			msg, err := sub.NextMsg(time.Second)
			if err != nil {
				if err == nats.ErrTimeout {
					continue
				}
				slog.Error("nats.next_msg", "err", err)
				continue
			}

			var reading LongShortRatioReading
			if err := json.Unmarshal(msg.Data, &reading); err != nil {
				slog.Warn("lsr.decode_failed", "err", err)
				continue
			}

			base := strings.TrimSuffix(reading.Symbol, "USDT")
			ratio, _ := strconv.ParseFloat(reading.Ratio, 64)
			longAcc, _ := strconv.ParseFloat(reading.LongAccount, 64)
			shortAcc, _ := strconv.ParseFloat(reading.ShortAccount, 64)

			query := `
				INSERT INTO app.live_long_short_ratio (ts, base, exchange, ratio, long_account, short_account)
				VALUES ($1, $2, $3, $4, $5, $6)
				ON CONFLICT (exchange, base, ts) DO NOTHING
			`
			_, err = pool.Exec(ctx, query, reading.EventAt, base, "binance", ratio, longAcc, shortAcc)
			if err != nil {
				slog.Error("lsr.db_insert_failed", "symbol", reading.Symbol, "err", err)
			}
		}
	}()
	return nil
}
