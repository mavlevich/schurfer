package ws

import (
	"context"
	"encoding/json"
	"log/slog"
	"net/http"
	"time"

	"github.com/gorilla/websocket"
	"github.com/mavlevich/schurfer/api-gateway/internal/health"
)

var upgrader = websocket.Upgrader{
	ReadBufferSize:  1024,
	WriteBufferSize: 1024,
	CheckOrigin: func(_ *http.Request) bool {
		// Origin validated at network level (Tailscale).
		// JWT cookie validated by auth middleware before reaching here.
		return true
	},
}

type statusMessage struct {
	Type string        `json:"type"`
	Data health.Report `json:"data"`
}

type Handler struct {
	checker  *health.Checker
	interval time.Duration
}

func NewHandler(checker *health.Checker, interval time.Duration) *Handler {
	return &Handler{checker: checker, interval: interval}
}

func (h *Handler) Status(w http.ResponseWriter, r *http.Request) {
	conn, err := upgrader.Upgrade(w, r, nil)
	if err != nil {
		slog.Error("websocket upgrade failed", "err", err)
		return
	}
	defer func() { _ = conn.Close() }()

	ctx, cancel := context.WithCancel(r.Context())
	defer cancel()

	// Cancel context when client disconnects.
	go func() {
		for {
			if _, _, err := conn.ReadMessage(); err != nil {
				cancel()
				return
			}
		}
	}()

	ticker := time.NewTicker(h.interval)
	defer ticker.Stop()

	// Send immediately on connect.
	if err := h.push(ctx, conn); err != nil {
		return
	}

	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			if err := h.push(ctx, conn); err != nil {
				return
			}
		}
	}
}

func (h *Handler) push(ctx context.Context, conn *websocket.Conn) error {
	report := h.checker.Check(ctx)
	msg := statusMessage{Type: "status", Data: report}

	data, err := json.Marshal(msg)
	if err != nil {
		return err
	}
	if err := conn.WriteMessage(websocket.TextMessage, data); err != nil {
		slog.Info("websocket client disconnected", "err", err)
		return err
	}
	return nil
}
