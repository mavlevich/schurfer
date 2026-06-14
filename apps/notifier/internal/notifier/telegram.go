package notifier

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"math"
	"net/http"
	"regexp"
	"strconv"
	"strings"
	"time"
)

var _telegramAPI = "https://api.telegram.org/bot%s/sendMessage"

func sendAlert(ctx context.Context, p pump, botToken, chatID string) error {
	text := formatAlert(p)
	body, _ := json.Marshal(map[string]string{
		"chat_id":    chatID,
		"text":       text,
		"parse_mode": "MarkdownV2",
	})

	url := fmt.Sprintf(_telegramAPI, botToken)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")

	client := &http.Client{Timeout: 10 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return err
	}
	defer func() { _ = resp.Body.Close() }()

	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("telegram: status %d", resp.StatusCode)
	}
	return nil
}

var mdv2Special = regexp.MustCompile(`[_*\[\]()~` + "`" + `>#+=|{}.!\\-]`)

func escapeMD(s string) string {
	return mdv2Special.ReplaceAllStringFunc(s, func(c string) string { return `\` + c })
}

func formatAlert(p pump) string {
	totalVol := 0.0
	for _, e := range p.Exchanges {
		totalVol += e.VolumeUSD
	}

	peak := peakPct(p.Exchanges)

	exParts := make([]string, 0, len(p.Exchanges))
	for i, e := range p.Exchanges {
		if i >= 4 {
			break
		}
		exParts = append(exParts, fmt.Sprintf("%s %s", escapeMD(e.Exchange), escapeMD(fmt.Sprintf("+%.1f%%", e.ChangePct))))
	}

	line2 := fmt.Sprintf("vol %s", escapeMD(fmtVol(totalVol)))
	if peak > p.MaxChangePct {
		line2 += fmt.Sprintf(" · peak %s", escapeMD(fmt.Sprintf("+%.1f%%", peak)))
	}

	return fmt.Sprintf("*%s* %s\n%s\n%s",
		escapeMD(p.Base), escapeMD(fmt.Sprintf("+%.1f%%", p.MaxChangePct)),
		line2,
		strings.Join(exParts, " · "),
	)
}

func peakPct(exchanges []exchange) float64 {
	best := 0.0
	for _, e := range exchanges {
		price, err1 := strconv.ParseFloat(e.Price, 64)
		high, err2 := strconv.ParseFloat(e.High24h, 64)
		if err1 != nil || err2 != nil || price <= 0 || high <= 0 || e.ChangePct <= -100 {
			continue
		}
		open := price / (1 + e.ChangePct/100)
		pct := (high/open - 1) * 100
		if pct > best {
			best = pct
		}
	}
	return math.Round(best*100) / 100
}

func fmtVol(n float64) string {
	switch {
	case n >= 1e9:
		return fmt.Sprintf("$%.1fB", n/1e9)
	case n >= 1e6:
		return fmt.Sprintf("$%.0fM", n/1e6)
	default:
		return fmt.Sprintf("$%.0f", n)
	}
}
