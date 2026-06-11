package auth_test

import (
	"testing"
	"time"

	"github.com/mavlevich/schurfer/api-gateway/internal/auth"
)

const testSecret = "test-secret-32-chars-long-enough" //nolint:gosec // gitleaks:allow

func TestNewToken_ValidateToken_RoundTrip(t *testing.T) {
	token, err := auth.NewToken(testSecret, time.Hour)
	if err != nil {
		t.Fatalf("NewToken: %v", err)
	}
	if token == "" {
		t.Fatal("expected non-empty token")
	}

	claims, err := auth.ValidateToken(token, testSecret)
	if err != nil {
		t.Fatalf("ValidateToken: %v", err)
	}
	if claims == nil {
		t.Fatal("expected non-nil claims")
	}
}

func TestValidateToken_WrongSecret(t *testing.T) {
	token, err := auth.NewToken(testSecret, time.Hour)
	if err != nil {
		t.Fatalf("NewToken: %v", err)
	}

	_, err = auth.ValidateToken(token, "wrong-secret")
	if err == nil {
		t.Fatal("expected error with wrong secret")
	}
}

func TestValidateToken_Expired(t *testing.T) {
	token, err := auth.NewToken(testSecret, -time.Second)
	if err != nil {
		t.Fatalf("NewToken: %v", err)
	}

	_, err = auth.ValidateToken(token, testSecret)
	if err == nil {
		t.Fatal("expected error for expired token")
	}
}
