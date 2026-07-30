package orderflow

import (
	"bufio"
	"compress/gzip"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestFileStoreAppendsValidGZIPMembersAndTracksSize(t *testing.T) {
	t.Parallel()
	root := t.TempDir()
	store, err := NewFileStore(root, 1<<20, 14*24*time.Hour)
	if err != nil {
		t.Fatalf("new store: %v", err)
	}
	record := sampleRecord()
	for range 2 {
		if _, err := store.Append([]Record{record}); err != nil {
			t.Fatalf("append: %v", err)
		}
	}
	if store.SizeBytes() <= 0 {
		t.Fatal("expected non-zero storage size")
	}

	path := filepath.Join(root, "2023-11-14", "event-42", "event-BTCUSDT.jsonl.gz")
	// #nosec G304 -- path is constructed under t.TempDir.
	file, err := os.Open(path)
	if err != nil {
		t.Fatalf("open segment: %v", err)
	}
	defer func() { _ = file.Close() }()
	reader, err := gzip.NewReader(file)
	if err != nil {
		t.Fatalf("gzip reader: %v", err)
	}
	defer func() { _ = reader.Close() }()
	scanner := bufio.NewScanner(reader)
	rows := 0
	for scanner.Scan() {
		var decoded Record
		if err := json.Unmarshal(scanner.Bytes(), &decoded); err != nil {
			t.Fatalf("decode record: %v", err)
		}
		rows++
	}
	if err := scanner.Err(); err != nil {
		t.Fatalf("scan: %v", err)
	}
	if rows != 2 {
		t.Fatalf("expected 2 rows, got %d", rows)
	}
}

func TestFileStoreFailsClosedAtBudget(t *testing.T) {
	t.Parallel()
	store, err := NewFileStore(t.TempDir(), 1, 14*24*time.Hour)
	if err != nil {
		t.Fatalf("new store: %v", err)
	}
	if _, err := store.Append([]Record{sampleRecord()}); !errors.Is(err, ErrStorageBudget) {
		t.Fatalf("expected storage budget error, got %v", err)
	}
}

func TestFileStoreDoesNotPartiallyWriteMultiPathBatchAtBudget(t *testing.T) {
	t.Parallel()
	first := sampleRecord()
	second := sampleRecord()
	second.Role = "control"
	second.ObservedSymbol = "ETHUSDT"
	firstPayload, err := encodeGZIP([]Record{first})
	if err != nil {
		t.Fatalf("encode first: %v", err)
	}
	secondPayload, err := encodeGZIP([]Record{second})
	if err != nil {
		t.Fatalf("encode second: %v", err)
	}
	root := t.TempDir()
	store, err := NewFileStore(
		root,
		int64(len(firstPayload)+len(secondPayload)-1),
		14*24*time.Hour,
	)
	if err != nil {
		t.Fatalf("new store: %v", err)
	}
	if _, err := store.Append([]Record{first, second}); !errors.Is(err, ErrStorageBudget) {
		t.Fatalf("expected storage budget error, got %v", err)
	}
	entries, err := os.ReadDir(root)
	if err != nil {
		t.Fatalf("read root: %v", err)
	}
	if len(entries) != 0 || store.SizeBytes() != 0 {
		t.Fatalf("budget failure wrote partial data: entries=%d size=%d", len(entries), store.SizeBytes())
	}
}

func TestFileStorePrunesExpiredDayDirectories(t *testing.T) {
	t.Parallel()
	root := t.TempDir()
	store, err := NewFileStore(root, 1<<20, 24*time.Hour)
	if err != nil {
		t.Fatalf("new store: %v", err)
	}
	if _, err := store.Append([]Record{sampleRecord()}); err != nil {
		t.Fatalf("append: %v", err)
	}
	removed, err := store.Prune(time.Date(2023, 11, 17, 0, 0, 0, 0, time.UTC))
	if err != nil {
		t.Fatalf("prune: %v", err)
	}
	if removed <= 0 || store.SizeBytes() != 0 {
		t.Fatalf("unexpected prune result: removed=%d size=%d", removed, store.SizeBytes())
	}
}

func sampleRecord() Record {
	activation := time.UnixMilli(1_700_000_000_000).UTC()
	return Record{
		ContractVersion:   ContractVersion,
		PumpEventID:       42,
		EventBase:         "BTC",
		EventSymbol:       "BTCUSDT",
		ObservedSymbol:    "BTCUSDT",
		Role:              "event",
		FirstObservedAtMS: activation.UnixMilli(),
		CaptureExpiresMS:  activation.Add(time.Hour).UnixMilli(),
		Bucket: Bucket{
			SchemaVersion: 1,
			Exchange:      "bybit",
			Symbol:        "BTCUSDT",
			BucketStartMS: activation.Add(-time.Second).UnixMilli(),
			Open:          100,
			High:          101,
			Low:           99,
			Close:         100.5,
			BuyNotional:   1000,
			SellNotional:  900,
		},
	}
}
