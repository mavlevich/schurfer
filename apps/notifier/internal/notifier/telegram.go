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

// sendMessage sends a plain-text message. Used for operational alerts (like a
// stale scanner) where MarkdownV2 escaping would only get in the way.
func sendMessage(ctx context.Context, text, botToken, chatID string) error {
	return postMessage(ctx, botToken, map[string]string{
		"chat_id": chatID,
		"text":    text,
	})
}

func postMessage(ctx context.Context, botToken string, fields map[string]string) error {
	body, _ := json.Marshal(fields)

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
	high24h := high24hPct(p.Exchanges)

	exParts := make([]string, 0, len(p.Exchanges))
	for i, e := range p.Exchanges {
		if i >= 4 {
			break
		}
		exParts = append(exParts, fmt.Sprintf("%s %s", escapeMD(e.Exchange), escapeMD(fmt.Sprintf("+%.1f%%", e.ChangePct))))
	}

	line2 := fmt.Sprintf("vol %s", escapeMD(formatTotalVolume(p.Exchanges)))
	if high24h > p.MaxChangePct {
		line2 += fmt.Sprintf(" · 24h high %s", escapeMD(fmt.Sprintf("+%.1f%%", high24h)))
	}

	return fmt.Sprintf("%s *%s* %s\n%s\n%s",
		pumpEmoji(p.MaxChangePct),
		escapeMD(p.Base), escapeMD(fmt.Sprintf("+%.1f%%", p.MaxChangePct)),
		line2,
		strings.Join(exParts, " · "),
	)
}

func formatTotalVolume(exchanges []exchange) string {
	total := 0.0
	known := 0
	incomplete := false
	for _, e := range exchanges {
		if e.VolumeUSD == nil || math.IsNaN(*e.VolumeUSD) || math.IsInf(*e.VolumeUSD, 0) || *e.VolumeUSD <= 0 {
			incomplete = true
			continue
		}
		total += *e.VolumeUSD
		known++
	}
	if known == 0 {
		return "n/a"
	}
	formatted := fmtVol(total)
	if incomplete {
		return formatted + "+"
	}
	return formatted
}

// pumpEmoji gives a quick-read intensity marker so the size of a pump is legible
// at a glance without parsing the number.
func pumpEmoji(pct float64) string {
	switch {
	case pct >= 100:
		return "🌋"
	case pct >= 60:
		return "🔥"
	default:
		return "🚀"
	}
}

func high24hPct(exchanges []exchange) float64 {
	best := 0.0
	for _, e := range exchanges {
		pct := exchangeHigh24hPct(e)
		if pct > best {
			best = pct
		}
	}
	return math.Round(best*100) / 100
}

func exchangeHigh24hPct(e exchange) float64 {
	price, err1 := strconv.ParseFloat(e.Price, 64)
	high, err2 := strconv.ParseFloat(e.High24h, 64)
	if err1 != nil || err2 != nil || price <= 0 || high <= 0 || e.ChangePct <= -100 {
		return 0
	}
	open := price / (1 + e.ChangePct/100)
	return math.Round(((high/open)-1)*10_000) / 100
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
