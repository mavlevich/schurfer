package research

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func writeCheckpointFixture(t *testing.T, payload string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "research-checkpoints.json")
	if err := os.WriteFile(path, []byte(payload), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

func TestReadCheckpointOrchestratorAcceptsSanitizedSnapshot(t *testing.T) {
	digest := strings.Repeat("a", 64)
	path := writeCheckpointFixture(t, `{
		"version":"research_checkpoints_v1",
		"generated_at":"2026-08-06T19:00:00Z",
		"runner_state":"idle",
		"checkpoints":[{
			"key":"orderflow",
			"title":"Bybit order-flow discovery",
			"contract":"bybit_orderflow_pilot_v1",
			"due_at":"2026-08-06T18:15:00Z",
			"state":"discovery_ready",
			"next_attempt_at":"2026-08-07T19:00:00Z",
			"last_attempt_at":"2026-08-06T19:00:00Z",
			"last_success_at":"2026-08-06T19:00:00Z",
			"report_status":"discovery_ready",
			"verdict":"discovery_ready",
			"report_file":"orderflow-20260806T190000Z.json",
			"report_sha256":"`+digest+`",
			"error":null,
			"notified_state":"discovery_ready"
		}]
	}`)

	status := readCheckpointOrchestrator(path)

	if status == nil || len(status.Checkpoints) != 1 {
		t.Fatal("expected one valid checkpoint")
	}
	if status.Checkpoints[0].State != "discovery_ready" {
		t.Fatalf("state = %q", status.Checkpoints[0].State)
	}
}

func TestReadCheckpointOrchestratorFailsClosed(t *testing.T) {
	tests := []string{
		`{"version":"unknown","generated_at":"2026-08-06T19:00:00Z","runner_state":"idle","checkpoints":[{}]}`,
		`{"version":"research_checkpoints_v1","generated_at":"2026-08-06T19:00:00Z","runner_state":"idle","checkpoints":[]}`,
		`{"version":"research_checkpoints_v1","generated_at":"2026-08-06T19:00:00Z","runner_state":"idle","checkpoints":[{"key":"x","title":"x","contract":"x","due_at":"2026-08-06T18:15:00Z","state":"surprise"}]}`,
		`{"version":"research_checkpoints_v1","generated_at":"2026-08-06T19:00:00Z","runner_state":"idle","checkpoints":[{"key":"x","title":"x","contract":"x","due_at":"2026-08-06T18:15:00Z","state":"scheduled","report_file":"../secret"}]}`,
	}
	for _, payload := range tests {
		if status := readCheckpointOrchestrator(writeCheckpointFixture(t, payload)); status != nil {
			t.Fatalf("malformed snapshot was accepted: %s", payload)
		}
	}
}

func TestCheckpointSnapshotStalenessUsesHourlyRunnerBudget(t *testing.T) {
	now := time.Date(2026, time.August, 6, 22, 0, 0, 0, time.UTC)
	if !checkpointSnapshotIsStale(now, now.Add(-3*time.Hour-time.Second)) {
		t.Fatal("snapshot older than three hours must be stale")
	}
	if checkpointSnapshotIsStale(now, now.Add(-3*time.Hour)) {
		t.Fatal("snapshot at the three-hour boundary must remain fresh")
	}
	if !checkpointSnapshotIsStale(now, now.Add(5*time.Minute+time.Second)) {
		t.Fatal("snapshot implausibly far in the future must be stale")
	}
}
