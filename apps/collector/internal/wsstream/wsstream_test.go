package wsstream

import (
	"bytes"
	"errors"
	"net"
	"testing"
)

type failingReader struct{}

func (failingReader) Read([]byte) (int, error) {
	return 0, errors.New("read failed")
}

func TestNewSessionIDChangesEachCall(t *testing.T) {
	a, err := NewSessionID(bytes.NewReader([]byte{1, 2, 3, 4, 5, 6, 7, 8}))
	if err != nil {
		t.Fatal(err)
	}
	b, err := NewSessionID(bytes.NewReader([]byte{8, 7, 6, 5, 4, 3, 2, 1}))
	if err != nil {
		t.Fatal(err)
	}
	if a == "" || b == "" || a == b {
		t.Fatalf("a=%q b=%q, want two distinct non-empty ids", a, b)
	}
}

func TestNewSessionIDFailsClosedOnReadError(t *testing.T) {
	_, err := NewSessionID(failingReader{})
	if err == nil {
		t.Fatal("expected an error, got nil")
	}
}

type timeoutError struct{}

func (timeoutError) Error() string   { return "timeout" }
func (timeoutError) Timeout() bool   { return true }
func (timeoutError) Temporary() bool { return true }

var _ net.Error = timeoutError{}

func TestClassifyReadErrorWrapsTimeoutsAsErrReadTimeout(t *testing.T) {
	err := ClassifyReadError(timeoutError{})
	if !IsReadTimeout(err) {
		t.Fatalf("ClassifyReadError(timeout) = %v, want IsReadTimeout() = true", err)
	}
}

func TestClassifyReadErrorLeavesNonTimeoutsUnclassified(t *testing.T) {
	err := ClassifyReadError(errors.New("connection reset"))
	if IsReadTimeout(err) {
		t.Fatalf("ClassifyReadError(non-timeout) = %v, want IsReadTimeout() = false", err)
	}
}

func TestChunkSlice(t *testing.T) {
	got := ChunkSlice([]int{1, 2, 3, 4, 5}, 2)
	want := [][]int{{1, 2}, {3, 4}, {5}}
	if len(got) != len(want) {
		t.Fatalf("ChunkSlice() = %v, want %v", got, want)
	}
	for i := range want {
		if len(got[i]) != len(want[i]) {
			t.Fatalf("ChunkSlice()[%d] = %v, want %v", i, got[i], want[i])
		}
		for j := range want[i] {
			if got[i][j] != want[i][j] {
				t.Fatalf("ChunkSlice()[%d] = %v, want %v", i, got[i], want[i])
			}
		}
	}
}

func TestChunkSliceEmptyInput(t *testing.T) {
	if got := ChunkSlice([]int{}, 3); len(got) != 0 {
		t.Fatalf("ChunkSlice(empty) = %v, want no chunks", got)
	}
}

func TestFinitePositiveNumber(t *testing.T) {
	cases := []struct {
		value float64
		want  bool
	}{
		{1.5, true},
		{0, false},
		{-1, false},
	}
	for _, c := range cases {
		if got := FinitePositiveNumber(c.value); got != c.want {
			t.Errorf("FinitePositiveNumber(%v) = %v, want %v", c.value, got, c.want)
		}
	}
}

func TestNormalizeSymbol(t *testing.T) {
	cases := []struct {
		in, want string
	}{
		{"btcusdt", "BTCUSDT"},
		{"  BTCUSDT  ", "BTCUSDT"},
		{"BTCUSDT", "BTCUSDT"},
		{"", ""},
	}
	for _, c := range cases {
		if got := NormalizeSymbol(c.in); got != c.want {
			t.Errorf("NormalizeSymbol(%q) = %q, want %q", c.in, got, c.want)
		}
	}
}
