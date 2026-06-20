package execution

import (
	"net/http"
	"net/http/httputil"
	"net/url"
	"strings"
)

// Handler proxies /api/account/* to the execution service.
type Handler struct {
	proxy *httputil.ReverseProxy
}

func NewHandler(executionURL string) *Handler {
	target, err := url.Parse(executionURL)
	if err != nil {
		panic("invalid EXECUTION_URL: " + executionURL)
	}
	proxy := httputil.NewSingleHostReverseProxy(target)
	return &Handler{proxy: proxy}
}

// ServeHTTP strips the /api/account prefix before forwarding.
// /api/account/balance  →  GET /balance
// /api/account/order    →  POST /order
func (h *Handler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	r = r.Clone(r.Context())
	r.URL.Path = strings.TrimPrefix(r.URL.Path, "/api/account")
	if r.URL.Path == "" {
		r.URL.Path = "/"
	}
	r.URL.RawPath = ""
	h.proxy.ServeHTTP(w, r)
}
