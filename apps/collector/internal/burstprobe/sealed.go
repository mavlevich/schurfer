package burstprobe

import (
	"compress/gzip"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sync"
	"time"
)

// Sealed appends market records to one gzip NDJSON file per UTC day (mode 0600) and,
// when a day is closed, writes its manifest: the line count and the file's sha256.
// Nothing in this package reads these files back. A restart on the same day appends a
// new gzip member to the day's file, which stays a valid gzip stream.
type Sealed struct {
	dir      string
	contract string
	mu       sync.Mutex
	day      string
	file     *os.File
	gz       *gzip.Writer
	clock    func() time.Time
}

// NewSealed creates the directory (0700) if needed.
func NewSealed(dir, contract string) (*Sealed, error) {
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return nil, err
	}
	return &Sealed{dir: dir, contract: contract, clock: time.Now}, nil
}

func (s *Sealed) path(day string) string {
	return filepath.Join(s.dir, "sealed-"+day+".ndjson.gz")
}

// Write appends one record to today's file and flushes it.
func (s *Sealed) Write(record any) error {
	line, err := json.Marshal(record)
	if err != nil {
		return err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	day := s.clock().UTC().Format("2006-01-02")
	if day != s.day {
		if err := s.closeDayLocked(); err != nil {
			return err
		}
		file, err := os.OpenFile(s.path(day), os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600)
		if err != nil {
			return err
		}
		s.day, s.file, s.gz = day, file, gzip.NewWriter(file)
	}
	if _, err := s.gz.Write(append(line, '\n')); err != nil {
		return err
	}
	return s.gz.Flush()
}

// Close closes the open day and writes its manifest.
func (s *Sealed) Close() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.closeDayLocked()
}

func (s *Sealed) closeDayLocked() error {
	if s.file == nil {
		return nil
	}
	day := s.day
	err := errors.Join(s.gz.Close(), s.file.Close())
	s.file, s.gz, s.day = nil, nil, ""
	if err != nil {
		return err
	}
	sum, err := sha256File(s.path(day))
	if err != nil {
		return err
	}
	lines, err := countLines(s.path(day)) // every gzip member, across restarts
	if err != nil {
		return err
	}
	manifest, err := json.MarshalIndent(map[string]any{
		"day": day, "contract": s.contract, "lines": lines, "sha256": sum,
		"closed_at": s.clock().UTC(),
	}, "", " ")
	if err != nil {
		return err
	}
	name := filepath.Join(s.dir, "sealed-"+day+".manifest.json")
	tmp := name + ".tmp"
	if err := os.WriteFile(tmp, append(manifest, '\n'), 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, name)
}

func countLines(path string) (int64, error) {
	f, err := os.Open(path) //nolint:gosec // the probe's own sealed file
	if err != nil {
		return 0, err
	}
	defer func() { _ = f.Close() }()
	gz, err := gzip.NewReader(f) // multistream: reads every member
	if err != nil {
		return 0, err
	}
	defer func() { _ = gz.Close() }()
	buf := make([]byte, 32<<10)
	var lines int64
	for {
		n, err := gz.Read(buf)
		for _, b := range buf[:n] {
			if b == '\n' {
				lines++
			}
		}
		if errors.Is(err, io.EOF) {
			return lines, nil
		}
		if err != nil {
			return lines, err
		}
	}
}

func sha256File(path string) (string, error) {
	f, err := os.Open(path) //nolint:gosec // the probe's own sealed file
	if err != nil {
		return "", err
	}
	defer func() { _ = f.Close() }()
	h := sha256.New()
	if _, err := io.Copy(h, f); err != nil {
		return "", fmt.Errorf("hash %s: %w", path, err)
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}
