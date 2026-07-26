package notifier

type exchange struct {
	Exchange          string   `json:"exchange"`
	ChangePct         float64  `json:"change_pct"`
	Price             string   `json:"price"`
	High24h           string   `json:"high_24h"`
	VolumeUSD         *float64 `json:"volume_24h_usd"`
	VolumeSource      string   `json:"volume_24h_source"`
	TickerTimestamp   *int64   `json:"ticker_timestamp_ms"`
	ScannerObservedAt *int64   `json:"observed_at_ms"`
}

type pump struct {
	Base         string     `json:"base"`
	PumpEventID  int64      `json:"pump_event_id"`
	MaxChangePct float64    `json:"max_change_pct"`
	Exchanges    []exchange `json:"exchanges"`
}

type payload struct {
	Ts            int64    `json:"ts"` // backward-compatible scan publish time
	PublishedAtMS int64    `json:"published_at_ms"`
	Scanned       []string `json:"scanned"`
	Pumps         []pump   `json:"pumps"`
}
