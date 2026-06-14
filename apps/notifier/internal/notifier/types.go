package notifier

type exchange struct {
	Exchange  string  `json:"exchange"`
	ChangePct float64 `json:"change_pct"`
	Price     string  `json:"price"`
	High24h   string  `json:"high_24h"`
	VolumeUSD float64 `json:"volume_24h_usd"`
}

type pump struct {
	Base         string     `json:"base"`
	MaxChangePct float64    `json:"max_change_pct"`
	Exchanges    []exchange `json:"exchanges"`
}

type payload struct {
	Scanned []string `json:"scanned"`
	Pumps   []pump   `json:"pumps"`
}
