package health

import (
	"encoding/json"
	"net/http"
)

type Handler struct {
	checker *Checker
}

func NewHandler(checker *Checker) *Handler {
	return &Handler{checker: checker}
}

// Liveness confirms the process is alive. Always returns 200.
// Use this for Docker/k8s liveness probes.
func (h *Handler) Liveness(w http.ResponseWriter, _ *http.Request) {
	w.WriteHeader(http.StatusOK)
}

// Health checks infrastructure dependencies and returns a JSON report.
// Returns 200 when all critical deps (Postgres, Redis) are up, 503 otherwise.
// Use this for k8s readiness probes and the UI status page.
func (h *Handler) Health(w http.ResponseWriter, r *http.Request) {
	report := h.checker.Check(r.Context())

	code := http.StatusOK
	if report.Postgres != StatusUp || report.Redis != StatusUp {
		code = http.StatusServiceUnavailable
	}

	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(report)
}
