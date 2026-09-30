package testdburl

import (
	"errors"
	"net/url"
	"os"
	"regexp"
	"strconv"
)

const defaultURL = "postgres://schurfer:schurfer_dev@localhost:5432/schurfer"

var verifyDatabase = regexp.MustCompile(`^schurfer_verify_[0-9a-f]{12}$`)

// URL returns the CI database or a disposable loopback database for make verify.
// Ambient DATABASE_URL is ignored so it cannot redirect destructive tests.
func URL() (string, error) {
	raw := os.Getenv("SCHURFER_TEST_DATABASE_URL")
	if raw == "" {
		return defaultURL, nil
	}
	u, err := url.Parse(raw)
	if err != nil || u.Opaque != "" || u.RawQuery != "" || u.Fragment != "" || u.User == nil {
		return "", errors.New("invalid disposable test database URL")
	}
	port, err := strconv.Atoi(u.Port())
	if err != nil || port < 1 || port > 65535 ||
		(u.Scheme != "postgres" && u.Scheme != "postgresql") ||
		u.Hostname() != "127.0.0.1" || u.User.Username() != "schurfer" || len(u.Path) < 2 ||
		!verifyDatabase.MatchString(u.Path[1:]) {
		return "", errors.New("configured test database is not disposable loopback PostgreSQL")
	}
	return raw, nil
}
