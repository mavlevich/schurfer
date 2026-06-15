package pumps

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"time"
)

type Candle struct {
	Time   int64   `json:"time"`
	Open   float64 `json:"open"`
	High   float64 `json:"high"`
	Low    float64 `json:"low"`
	Close  float64 `json:"close"`
	Volume float64 `json:"volume"`
}

// fetchOHLCV dispatches to the exchange-specific implementation.
func fetchOHLCV(ctx context.Context, exchange, base string, interval, limit int) ([]Candle, error) {
	switch exchange {
	case "binance":
		return fetchBinance(ctx, base, interval, limit)
	case "okx":
		return fetchOKX(ctx, base, interval, limit)
	case "gate":
		return fetchGate(ctx, base, interval, limit)
	default:
		return fetchBybit(ctx, base, interval, limit)
	}
}

func fetchBybit(ctx context.Context, base string, interval, limit int) ([]Candle, error) {
	url := fmt.Sprintf(
		"https://api.bybit.com/v5/market/kline?category=linear&symbol=%sUSDT&interval=%d&limit=%d",
		base, interval, limit,
	)
	raw, err := httpGet(ctx, url)
	if err != nil {
		return nil, err
	}
	candles, err := parseBybit(raw)
	if err != nil {
		return nil, fmt.Errorf("bybit/%s: %w", base, err)
	}
	return candles, nil
}

func parseBybit(raw []byte) ([]Candle, error) {
	var resp struct {
		RetCode int    `json:"retCode"`
		RetMsg  string `json:"retMsg"`
		Result  struct {
			List [][]string `json:"list"`
		} `json:"result"`
	}
	if err := json.Unmarshal(raw, &resp); err != nil {
		return nil, fmt.Errorf("json: %w", err)
	}
	if resp.RetCode != 0 {
		return nil, fmt.Errorf("exchange error %d: %s", resp.RetCode, resp.RetMsg)
	}
	candles := make([]Candle, 0, len(resp.Result.List))
	for i, row := range resp.Result.List {
		c, err := parseRow(row, 0, 1, 2, 3, 4, 5)
		if err != nil {
			return nil, fmt.Errorf("row %d: %w", i, err)
		}
		c.Time /= 1000 // ms → s
		candles = append(candles, c)
	}
	reverseCandles(candles) // Bybit returns newest first
	return candles, nil
}

func binanceInterval(minutes int) string {
	switch minutes {
	case 60:
		return "1h"
	case 120:
		return "2h"
	case 240:
		return "4h"
	case 360:
		return "6h"
	case 480:
		return "8h"
	case 720:
		return "12h"
	default:
		return fmt.Sprintf("%dm", minutes)
	}
}

func fetchBinance(ctx context.Context, base string, interval, limit int) ([]Candle, error) {
	ivStr := binanceInterval(interval)
	url := fmt.Sprintf(
		"https://fapi.binance.com/fapi/v1/klines?symbol=%sUSDT&interval=%s&limit=%d",
		base, ivStr, limit,
	)
	raw, err := httpGet(ctx, url)
	if err != nil {
		return nil, err
	}
	candles, err := parseBinance(raw)
	if err != nil {
		return nil, fmt.Errorf("binance/%s: %w", base, err)
	}
	return candles, nil
}

func parseBinance(raw []byte) ([]Candle, error) {
	// Binance returns an array on success, an object on error.
	var rows [][]json.RawMessage
	if err := json.Unmarshal(raw, &rows); err != nil {
		var errResp struct {
			Code int    `json:"code"`
			Msg  string `json:"msg"`
		}
		if json.Unmarshal(raw, &errResp) == nil && errResp.Code != 0 {
			return nil, fmt.Errorf("exchange error %d: %s", errResp.Code, errResp.Msg)
		}
		return nil, fmt.Errorf("json: %w", err)
	}
	candles := make([]Candle, 0, len(rows))
	for i, row := range rows {
		if len(row) < 6 {
			return nil, fmt.Errorf("row %d: short (%d fields)", i, len(row))
		}
		var ts int64
		if err := json.Unmarshal(row[0], &ts); err != nil {
			return nil, fmt.Errorf("row %d time: %w", i, err)
		}
		parseStr := func(idx int, name string) (float64, error) {
			var s string
			if err := json.Unmarshal(row[idx], &s); err != nil {
				return 0, fmt.Errorf("row %d %s: %w", i, name, err)
			}
			v, err := strconv.ParseFloat(s, 64)
			if err != nil {
				return 0, fmt.Errorf("row %d %s=%q: %w", i, name, s, err)
			}
			return v, nil
		}
		o, err := parseStr(1, "open")
		if err != nil {
			return nil, err
		}
		h, err := parseStr(2, "high")
		if err != nil {
			return nil, err
		}
		l, err := parseStr(3, "low")
		if err != nil {
			return nil, err
		}
		c, err := parseStr(4, "close")
		if err != nil {
			return nil, err
		}
		v, err := parseStr(5, "volume")
		if err != nil {
			return nil, err
		}
		candles = append(candles, Candle{Time: ts / 1000, Open: o, High: h, Low: l, Close: c, Volume: v})
	}
	return candles, nil
}

func okxInterval(minutes int) string {
	switch minutes {
	case 60:
		return "1H"
	case 120:
		return "2H"
	case 240:
		return "4H"
	case 360:
		return "6H"
	case 480:
		return "8H"
	case 720:
		return "12H"
	default:
		return fmt.Sprintf("%dm", minutes)
	}
}

func fetchOKX(ctx context.Context, base string, interval, limit int) ([]Candle, error) {
	bar := okxInterval(interval)
	url := fmt.Sprintf(
		"https://www.okx.com/api/v5/market/candles?instId=%s-USDT-SWAP&bar=%s&limit=%d",
		base, bar, limit,
	)
	raw, err := httpGet(ctx, url)
	if err != nil {
		return nil, err
	}
	candles, err := parseOKX(raw)
	if err != nil {
		return nil, fmt.Errorf("okx/%s: %w", base, err)
	}
	return candles, nil
}

func parseOKX(raw []byte) ([]Candle, error) {
	var resp struct {
		Code string     `json:"code"`
		Msg  string     `json:"msg"`
		Data [][]string `json:"data"`
	}
	if err := json.Unmarshal(raw, &resp); err != nil {
		return nil, fmt.Errorf("json: %w", err)
	}
	if resp.Code != "0" {
		return nil, fmt.Errorf("exchange error %s: %s", resp.Code, resp.Msg)
	}
	candles := make([]Candle, 0, len(resp.Data))
	for i, row := range resp.Data {
		c, err := parseRow(row, 0, 1, 2, 3, 4, 5)
		if err != nil {
			return nil, fmt.Errorf("row %d: %w", i, err)
		}
		c.Time /= 1000 // ms → s
		candles = append(candles, c)
	}
	reverseCandles(candles) // OKX returns newest first
	return candles, nil
}

func fetchGate(ctx context.Context, base string, interval, limit int) ([]Candle, error) {
	ivStr := fmt.Sprintf("%dm", interval)
	if interval == 60 {
		ivStr = "1h"
	}
	url := fmt.Sprintf(
		"https://fx-api.gateio.ws/api/v4/futures/usdt/candlesticks?contract=%s_USDT&interval=%s&limit=%d",
		base, ivStr, limit,
	)
	raw, err := httpGet(ctx, url)
	if err != nil {
		return nil, err
	}
	candles, err := parseGate(raw)
	if err != nil {
		return nil, fmt.Errorf("gate/%s: %w", base, err)
	}
	return candles, nil
}

func parseGate(raw []byte) ([]Candle, error) {
	var rows []struct {
		T int64   `json:"t"`
		O string  `json:"o"`
		H string  `json:"h"`
		L string  `json:"l"`
		C string  `json:"c"`
		V float64 `json:"v"`
	}
	if err := json.Unmarshal(raw, &rows); err != nil {
		return nil, fmt.Errorf("json: %w", err)
	}
	candles := make([]Candle, 0, len(rows))
	for i, row := range rows {
		parseF := func(s, name string) (float64, error) {
			v, err := strconv.ParseFloat(s, 64)
			if err != nil {
				return 0, fmt.Errorf("row %d %s=%q: %w", i, name, s, err)
			}
			return v, nil
		}
		o, err := parseF(row.O, "open")
		if err != nil {
			return nil, err
		}
		h, err := parseF(row.H, "high")
		if err != nil {
			return nil, err
		}
		l, err := parseF(row.L, "low")
		if err != nil {
			return nil, err
		}
		c, err := parseF(row.C, "close")
		if err != nil {
			return nil, err
		}
		candles = append(candles, Candle{Time: row.T, Open: o, High: h, Low: l, Close: c, Volume: row.V})
	}
	return candles, nil
}

// parseRow extracts a Candle from a string slice given field indices.
// Returns an error if the row is too short or any value fails to parse.
func parseRow(row []string, tsIdx, oIdx, hIdx, lIdx, cIdx, vIdx int) (Candle, error) {
	need := max(tsIdx, oIdx, hIdx, lIdx, cIdx, vIdx) + 1
	if len(row) < need {
		return Candle{}, fmt.Errorf("short row: need %d fields, got %d", need, len(row))
	}
	ts, err := strconv.ParseInt(row[tsIdx], 10, 64)
	if err != nil {
		return Candle{}, fmt.Errorf("time=%q: %w", row[tsIdx], err)
	}
	parseF := func(s, name string) (float64, error) {
		v, err := strconv.ParseFloat(s, 64)
		if err != nil {
			return 0, fmt.Errorf("%s=%q: %w", name, s, err)
		}
		return v, nil
	}
	o, err := parseF(row[oIdx], "open")
	if err != nil {
		return Candle{}, err
	}
	h, err := parseF(row[hIdx], "high")
	if err != nil {
		return Candle{}, err
	}
	l, err := parseF(row[lIdx], "low")
	if err != nil {
		return Candle{}, err
	}
	c, err := parseF(row[cIdx], "close")
	if err != nil {
		return Candle{}, err
	}
	v, err := parseF(row[vIdx], "volume")
	if err != nil {
		return Candle{}, err
	}
	return Candle{Time: ts, Open: o, High: h, Low: l, Close: c, Volume: v}, nil
}

func reverseCandles(c []Candle) {
	for i, j := 0, len(c)-1; i < j; i, j = i+1, j-1 {
		c[i], c[j] = c[j], c[i]
	}
}

func httpGet(ctx context.Context, url string) ([]byte, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return nil, err
	}
	client := &http.Client{Timeout: 10 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 512))
		return nil, fmt.Errorf("HTTP %d: %s", resp.StatusCode, body)
	}
	return io.ReadAll(resp.Body)
}
