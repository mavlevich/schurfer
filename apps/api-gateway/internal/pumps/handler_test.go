package pumps

import "testing"

func ptr(v int64) *int64 { return &v }

func TestAggregateOI(t *testing.T) {
	cases := []struct {
		name         string
		snapshots    []oiSnapshotEntry
		firstSeenAt  *int64
		wantCurrent  float64
		wantBaseline float64
		wantDeltaNil bool
		wantDeltaPct float64
	}{
		{
			name:         "no snapshots",
			snapshots:    nil,
			firstSeenAt:  ptr(100),
			wantCurrent:  0,
			wantBaseline: 0,
			wantDeltaNil: true,
		},
		{
			name: "single snapshot per exchange — delta is zero, not nil",
			snapshots: []oiSnapshotEntry{
				{Exchange: "binance", OiUSD: 1000, TS: 100},
				{Exchange: "bybit", OiUSD: 500, TS: 100},
			},
			firstSeenAt:  ptr(100),
			wantCurrent:  1500,
			wantBaseline: 1500,
			wantDeltaPct: 0,
		},
		{
			name: "OI grows — positive delta across exchanges",
			snapshots: []oiSnapshotEntry{
				{Exchange: "binance", OiUSD: 1000, TS: 100},
				{Exchange: "binance", OiUSD: 1200, TS: 200},
				{Exchange: "bybit", OiUSD: 500, TS: 100},
				{Exchange: "bybit", OiUSD: 600, TS: 200},
			},
			firstSeenAt:  ptr(100),
			wantCurrent:  1800,
			wantBaseline: 1500,
			wantDeltaPct: 20,
		},
		{
			name: "OI declines — negative delta (divergence signal)",
			snapshots: []oiSnapshotEntry{
				{Exchange: "binance", OiUSD: 1000, TS: 100},
				{Exchange: "binance", OiUSD: 800, TS: 200},
			},
			firstSeenAt:  ptr(100),
			wantCurrent:  800,
			wantBaseline: 1000,
			wantDeltaPct: -20,
		},
		{
			name: "no open/closed episode (firstSeenAt nil) — baseline is earliest snapshot",
			snapshots: []oiSnapshotEntry{
				{Exchange: "binance", OiUSD: 1000, TS: 100},
				{Exchange: "binance", OiUSD: 1100, TS: 200},
			},
			firstSeenAt:  nil,
			wantCurrent:  1100,
			wantBaseline: 1000,
			wantDeltaPct: 10,
		},
		{
			name: "snapshot exists only before episode start — exchange excluded from baseline",
			// A late-joining exchange's first row is after firstSeenAt, so it
			// correctly becomes both baseline and current for that exchange.
			snapshots: []oiSnapshotEntry{
				{Exchange: "okx", OiUSD: 5000, TS: 50}, // before firstSeenAt — would be wrong as baseline
				{Exchange: "okx", OiUSD: 6000, TS: 150},
			},
			firstSeenAt:  ptr(100),
			wantCurrent:  6000,
			wantBaseline: 6000,
			wantDeltaPct: 0,
		},
		{
			name: "new exchange joins mid-episode — only contributes to current, not baseline",
			snapshots: []oiSnapshotEntry{
				{Exchange: "binance", OiUSD: 1000, TS: 100},
				{Exchange: "binance", OiUSD: 1000, TS: 200},
				{Exchange: "bybit", OiUSD: 300, TS: 200}, // joined after t=100, still counted
			},
			firstSeenAt:  ptr(100),
			wantCurrent:  1300,
			wantBaseline: 1300,
			wantDeltaPct: 0,
		},
		{
			name: "zero baseline — delta stays nil instead of dividing by zero",
			snapshots: []oiSnapshotEntry{
				{Exchange: "binance", OiUSD: 0, TS: 100},
				{Exchange: "binance", OiUSD: 500, TS: 200},
			},
			firstSeenAt:  ptr(100),
			wantCurrent:  500,
			wantBaseline: 0,
			wantDeltaNil: true,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			current, baseline, deltaPct := aggregateOI(tc.snapshots, tc.firstSeenAt)

			if current != tc.wantCurrent {
				t.Errorf("current = %v, want %v", current, tc.wantCurrent)
			}
			if baseline != tc.wantBaseline {
				t.Errorf("baseline = %v, want %v", baseline, tc.wantBaseline)
			}
			if tc.wantDeltaNil {
				if deltaPct != nil {
					t.Errorf("deltaPct = %v, want nil", *deltaPct)
				}
				return
			}
			if deltaPct == nil {
				t.Fatalf("deltaPct = nil, want %v", tc.wantDeltaPct)
			}
			if *deltaPct != tc.wantDeltaPct {
				t.Errorf("deltaPct = %v, want %v", *deltaPct, tc.wantDeltaPct)
			}
		})
	}
}
