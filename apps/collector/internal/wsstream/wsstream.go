// Package wsstream provides small, venue-agnostic helpers shared by every
// momentum-capture source adapter (bybit, binance, ...): WebSocket read-
// liveness deadline management, read-timeout classification, a fresh-
// per-dial session id generator, a wire-symbol normalizer, and two small
// pure utilities (chunking, finite-positive-number validation). Most of
// this is WebSocket connection hygiene, not specific to any one venue's
// message format; NormalizeSymbol is the one exception (also used for
// plain REST lookups, e.g. binance's OpenInterest polling) but lives here
// too rather than getting its own package for one function. Kept in one
// place so a future fix does not have to be manually ported to each
// adapter separately. Extracted after a code-review finding on
// feat/binance-momentum-source-v1: these had already been copy-pasted
// verbatim from apps/collector/internal/bybit once.
//
// Convention for new adapters (also a code-review finding, before any
// third venue is added): call this package's exported functions directly,
// the way apps/collector/internal/binance does. bybit's own ws.go/
// trades.go instead keep thin same-named private wrapper functions that
// delegate here -- that indirection exists ONLY so bybit's pre-existing
// call sites and tests (which already referenced those private names
// before this package existed) did not need to change during the
// extraction. It is not a pattern to copy into a new adapter with no such
// legacy call sites to preserve.
package wsstream

import (
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"math"
	"net"
	"strings"
	"time"

	"github.com/gorilla/websocket"
)

var ErrReadTimeout = errors.New("websocket read timeout")

// NewSessionID returns a random identifier for one physical connection,
// read from source (crypto/rand.Reader in production, an injectable
// io.Reader in tests). Fails closed: a source read failure returns an
// error rather than a fixed fallback string, which would otherwise let
// every subsequent connection attempt silently share one non-unique value.
func NewSessionID(source io.Reader) (string, error) {
	var b [8]byte
	if _, err := io.ReadFull(source, b[:]); err != nil {
		return "", fmt.Errorf("read random bytes: %w", err)
	}
	return hex.EncodeToString(b[:]), nil
}

func RefreshReadDeadline(conn *websocket.Conn, timeout time.Duration) error {
	return conn.SetReadDeadline(time.Now().Add(timeout))
}

// ConfigureReadLiveness wires ping/pong control frames to also refresh the
// read deadline, so a peer that only sends control frames (no data) is not
// mistaken for a dead connection.
func ConfigureReadLiveness(conn *websocket.Conn, timeout time.Duration) error {
	if err := RefreshReadDeadline(conn, timeout); err != nil {
		return err
	}
	pingHandler := conn.PingHandler()
	conn.SetPingHandler(func(message string) error {
		if err := RefreshReadDeadline(conn, timeout); err != nil {
			return err
		}
		return pingHandler(message)
	})
	pongHandler := conn.PongHandler()
	conn.SetPongHandler(func(message string) error {
		if err := RefreshReadDeadline(conn, timeout); err != nil {
			return err
		}
		return pongHandler(message)
	})
	return nil
}

func IsReadTimeout(err error) bool {
	return errors.Is(err, ErrReadTimeout)
}

func ClassifyReadError(err error) error {
	var netErr net.Error
	if errors.As(err, &netErr) && netErr.Timeout() {
		return fmt.Errorf("read frame: %w: %w", ErrReadTimeout, err)
	}
	return fmt.Errorf("read frame: %w", err)
}

func ChunkSlice[T any](s []T, size int) [][]T {
	var out [][]T
	for i := 0; i < len(s); i += size {
		end := min(i+size, len(s))
		out = append(out, s[i:end])
	}
	return out
}

func FinitePositiveNumber(value float64) bool {
	return !math.IsNaN(value) && !math.IsInf(value, 0) && value > 0
}

// NormalizeSymbol is the one, shared definition of how a raw wire symbol
// becomes a canonical native market id -- both bybit and binance's own
// session-id maps (keyed by this same function) rely on it staying
// identical to what actually lands on their PublicTrade.Symbol, not two
// independently hand-copied transformations that could silently drift
// from each other.
func NormalizeSymbol(symbol string) string {
	return strings.ToUpper(strings.TrimSpace(symbol))
}
