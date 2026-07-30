package orderflow

import (
	"bytes"
	"compress/gzip"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"time"
)

var ErrStorageBudget = errors.New("order-flow storage budget reached")

type FileStore struct {
	root      string
	maxBytes  int64
	retention time.Duration
	sizeBytes int64
}

type encodedBatch struct {
	path    string
	payload []byte
}

func NewFileStore(root string, maxBytes int64, retention time.Duration) (*FileStore, error) {
	root = strings.TrimSpace(root)
	if root == "" {
		return nil, errors.New("order-flow storage root is required")
	}
	if maxBytes <= 0 {
		return nil, errors.New("order-flow storage budget must be positive")
	}
	if retention <= 0 {
		return nil, errors.New("order-flow retention must be positive")
	}
	if err := os.MkdirAll(root, 0o750); err != nil {
		return nil, fmt.Errorf("create order-flow storage root: %w", err)
	}
	sizeBytes, err := directorySize(root)
	if err != nil {
		return nil, err
	}
	return &FileStore{
		root:      root,
		maxBytes:  maxBytes,
		retention: retention,
		sizeBytes: sizeBytes,
	}, nil
}

func (store *FileStore) Append(records []Record) (int64, error) {
	if len(records) == 0 {
		return 0, nil
	}
	grouped := make(map[string][]Record)
	for _, record := range records {
		path, err := store.path(record)
		if err != nil {
			return 0, err
		}
		grouped[path] = append(grouped[path], record)
	}

	paths := make([]string, 0, len(grouped))
	for path := range grouped {
		paths = append(paths, path)
	}
	slices.Sort(paths)
	encoded := make([]encodedBatch, 0, len(paths))
	var requiredBytes int64
	for _, path := range paths {
		batch := grouped[path]
		payload, err := encodeGZIP(batch)
		if err != nil {
			return 0, err
		}
		requiredBytes += int64(len(payload))
		encoded = append(encoded, encodedBatch{path: path, payload: payload})
	}
	if store.sizeBytes+requiredBytes > store.maxBytes {
		return 0, ErrStorageBudget
	}

	var totalWritten int64
	for _, batch := range encoded {
		path := batch.path
		payload := batch.payload
		if err := os.MkdirAll(filepath.Dir(path), 0o750); err != nil {
			return totalWritten, fmt.Errorf("create order-flow event directory: %w", err)
		}
		// #nosec G304 -- every relative component is validated by safeComponent.
		file, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o600)
		if err != nil {
			return totalWritten, fmt.Errorf("open order-flow segment: %w", err)
		}
		written, writeErr := file.Write(payload)
		syncErr := file.Sync()
		closeErr := file.Close()
		store.sizeBytes += int64(written)
		totalWritten += int64(written)
		switch {
		case writeErr != nil:
			return totalWritten, fmt.Errorf("write order-flow segment: %w", writeErr)
		case syncErr != nil:
			return totalWritten, fmt.Errorf("sync order-flow segment: %w", syncErr)
		case closeErr != nil:
			return totalWritten, fmt.Errorf("close order-flow segment: %w", closeErr)
		case written != len(payload):
			return totalWritten, ioErrShortWrite(written, len(payload))
		}
	}
	return totalWritten, nil
}

func (store *FileStore) Prune(now time.Time) (int64, error) {
	if now.IsZero() {
		return 0, errors.New("prune timestamp is required")
	}
	entries, err := os.ReadDir(store.root)
	if err != nil {
		return 0, fmt.Errorf("read order-flow storage root: %w", err)
	}
	cutoff := now.UTC().Add(-store.retention)
	var removed int64
	for _, entry := range entries {
		if !entry.IsDir() {
			continue
		}
		day, parseErr := time.Parse("2006-01-02", entry.Name())
		if parseErr != nil || !day.Before(cutoff.Truncate(24*time.Hour)) {
			continue
		}
		path := filepath.Join(store.root, entry.Name())
		size, sizeErr := directorySize(path)
		if sizeErr != nil {
			return removed, sizeErr
		}
		if removeErr := os.RemoveAll(path); removeErr != nil {
			return removed, fmt.Errorf("prune order-flow day %s: %w", entry.Name(), removeErr)
		}
		removed += size
		store.sizeBytes = max(store.sizeBytes-size, 0)
	}
	return removed, nil
}

func (store *FileStore) SizeBytes() int64 {
	return store.sizeBytes
}

func (store *FileStore) path(record Record) (string, error) {
	if record.ContractVersion != ContractVersion ||
		record.PumpEventID <= 0 || record.FirstObservedAtMS <= 0 {
		return "", errors.New("order-flow record identity is incomplete")
	}
	role := safeComponent(record.Role)
	symbol := safeComponent(record.ObservedSymbol)
	if role == "" || symbol == "" {
		return "", errors.New("order-flow record path is unsafe")
	}
	day := time.UnixMilli(record.FirstObservedAtMS).UTC().Format("2006-01-02")
	eventDirectory := "event-" + strconv.FormatInt(record.PumpEventID, 10)
	return filepath.Join(store.root, day, eventDirectory, role+"-"+symbol+".jsonl.gz"), nil
}

func encodeGZIP(records []Record) ([]byte, error) {
	var buffer bytes.Buffer
	writer, err := gzip.NewWriterLevel(&buffer, gzip.BestSpeed)
	if err != nil {
		return nil, fmt.Errorf("create order-flow compressor: %w", err)
	}
	encoder := json.NewEncoder(writer)
	for _, record := range records {
		if err := encoder.Encode(record); err != nil {
			_ = writer.Close()
			return nil, fmt.Errorf("encode order-flow record: %w", err)
		}
	}
	if err := writer.Close(); err != nil {
		return nil, fmt.Errorf("close order-flow compressor: %w", err)
	}
	return buffer.Bytes(), nil
}

func directorySize(root string) (int64, error) {
	var total int64
	err := filepath.WalkDir(root, func(path string, entry fs.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		if entry.IsDir() {
			return nil
		}
		info, err := entry.Info()
		if err != nil {
			return err
		}
		total += info.Size()
		return nil
	})
	if err != nil {
		return 0, fmt.Errorf("measure order-flow storage: %w", err)
	}
	return total, nil
}

func safeComponent(value string) string {
	value = strings.TrimSpace(value)
	if value == "" {
		return ""
	}
	var builder strings.Builder
	for _, character := range value {
		switch {
		case character >= 'A' && character <= 'Z':
			builder.WriteRune(character)
		case character >= 'a' && character <= 'z':
			builder.WriteRune(character)
		case character >= '0' && character <= '9':
			builder.WriteRune(character)
		case character == '-', character == '_', character == '.':
			builder.WriteRune(character)
		default:
			return ""
		}
	}
	return builder.String()
}

func ioErrShortWrite(written, expected int) error {
	return fmt.Errorf("short order-flow write: wrote %d of %d bytes", written, expected)
}
