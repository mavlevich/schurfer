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
	"strings"
	"sync"
	"time"
)

// Sealed appends market records to gzip NDJSON segments: one segment per UTC day per
// run, named by the day and the run id, created new (never appended to) and mode 0600.
// Closing a segment writes its manifest (status "closed", line count, sha256).
//
// A run killed without closing (SIGKILL, OOM) leaves a segment without a trailer and
// without a manifest. On start, every such segment gets a manifest with status
// "unterminated", the lines that can still be read, and whether the stream was cut, so
// nothing is appended to a broken stream and nothing is silently lost.
//
// The content is never parsed or reported here; it is read back only to count lines.
type Sealed struct {
	dir      string
	contract string
	runID    string
	mu       sync.Mutex
	day      string
	file     *os.File
	gz       *gzip.Writer
	clock    func() time.Time
}

// NewSealed creates the directory (0700) and closes the segments a crashed run left.
func NewSealed(dir, contract, runID string) (*Sealed, error) {
	if runID == "" || strings.ContainsAny(runID, `/\`) {
		return nil, errors.New("sealed: a plain run id is required")
	}
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return nil, err
	}
	s := &Sealed{dir: dir, contract: contract, runID: runID, clock: time.Now}
	if err := s.recover(); err != nil {
		return nil, err
	}
	return s, nil
}

func (s *Sealed) segment(day string) string {
	return filepath.Join(s.dir, "sealed-"+day+"-"+s.runID+".ndjson.gz")
}

func manifestOf(segment string) string {
	return strings.TrimSuffix(segment, ".ndjson.gz") + ".manifest.json"
}

// recover writes an "unterminated" manifest for every segment without one.
func (s *Sealed) recover() error {
	segments, err := filepath.Glob(filepath.Join(s.dir, "sealed-*.ndjson.gz"))
	if err != nil {
		return err
	}
	for _, segment := range segments {
		if _, err := os.Stat(manifestOf(segment)); err == nil {
			continue
		}
		lines, cut := countLines(segment)
		if err := s.writeManifest(segment, "unterminated", lines, cut); err != nil {
			return err
		}
	}
	return nil
}

// Write appends one record to the run's segment of today and flushes it.
func (s *Sealed) Write(record any) error {
	line, err := json.Marshal(record)
	if err != nil {
		return err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	day := s.clock().UTC().Format("2006-01-02")
	if day != s.day {
		if err := s.closeLocked(); err != nil {
			return err
		}
		file, err := os.OpenFile(s.segment(day), os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o600)
		if err != nil {
			return fmt.Errorf("sealed segment: %w", err)
		}
		s.day, s.file, s.gz = day, file, gzip.NewWriter(file)
	}
	if _, err := s.gz.Write(append(line, '\n')); err != nil {
		return err
	}
	if err := s.gz.Flush(); err != nil {
		return err
	}
	return s.file.Sync()
}

// Close closes the open segment and writes its manifest.
func (s *Sealed) Close() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.closeLocked()
}

func (s *Sealed) closeLocked() error {
	if s.file == nil {
		return nil
	}
	segment := s.segment(s.day)
	err := errors.Join(s.gz.Close(), s.file.Close())
	s.file, s.gz, s.day = nil, nil, ""
	if err != nil {
		return err
	}
	lines, cut := countLines(segment)
	return s.writeManifest(segment, "closed", lines, cut)
}

func (s *Sealed) writeManifest(segment, status string, lines int64, cut bool) error {
	sum, err := sha256File(segment)
	if err != nil {
		return err
	}
	body, err := json.MarshalIndent(map[string]any{
		"segment": filepath.Base(segment), "status": status, "contract": s.contract,
		"lines": lines, "stream_cut": cut, "sha256": sum, "written_at": s.clock().UTC(),
		"written_by_run": s.runID,
	}, "", " ")
	if err != nil {
		return err
	}
	name := manifestOf(segment)
	tmp := name + ".tmp"
	if err := os.WriteFile(tmp, append(body, '\n'), 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, name)
}

// countLines counts the complete lines that can be read; cut reports a stream that
// ended without its gzip trailer or with a partial last line.
func countLines(path string) (lines int64, cut bool) {
	f, err := os.Open(path) //nolint:gosec // the probe's own sealed segment
	if err != nil {
		return 0, true
	}
	defer func() { _ = f.Close() }()
	gz, err := gzip.NewReader(f)
	if err != nil {
		return 0, true
	}
	defer func() { _ = gz.Close() }()
	buf := make([]byte, 32<<10)
	var last byte = '\n'
	for {
		n, err := gz.Read(buf)
		for _, b := range buf[:n] {
			if b == '\n' {
				lines++
			}
		}
		if n > 0 {
			last = buf[n-1]
		}
		if errors.Is(err, io.EOF) {
			return lines, last != '\n'
		}
		if err != nil {
			return lines, true
		}
	}
}

func sha256File(path string) (string, error) {
	f, err := os.Open(path) //nolint:gosec // the probe's own sealed segment
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
