package research

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"time"
)

const checkpointSnapshotVersion = "research_checkpoints_v1"
const checkpointSnapshotMaxAge = 3 * time.Hour

var checkpointStates = map[string]struct{}{
	"scheduled":                    {},
	"due":                          {},
	"collecting":                   {},
	"awaiting_complete_resolution": {},
	"directional":                  {},
	"decision_ready":               {},
	"discovery_ready":              {},
	"shadow_candidate":             {},
	"no_go":                        {},
	"boundary_only_ready":          {},
	"blocked_resources":            {},
	"error":                        {},
}

type CheckpointOrchestrator struct {
	Version     string               `json:"version"`
	GeneratedAt time.Time            `json:"generated_at"`
	RunnerState string               `json:"runner_state"`
	Stale       bool                 `json:"stale"`
	Checkpoints []ResearchCheckpoint `json:"checkpoints"`
}

type ResearchCheckpoint struct {
	Key           string     `json:"key"`
	Title         string     `json:"title"`
	Contract      string     `json:"contract"`
	DueAt         time.Time  `json:"due_at"`
	State         string     `json:"state"`
	NextAttemptAt *time.Time `json:"next_attempt_at"`
	LastAttemptAt *time.Time `json:"last_attempt_at"`
	LastSuccessAt *time.Time `json:"last_success_at"`
	ReportStatus  *string    `json:"report_status"`
	Verdict       *string    `json:"verdict"`
	ReportFile    *string    `json:"report_file"`
	ReportSHA256  *string    `json:"report_sha256"`
	Error         *string    `json:"error"`
	NotifiedState *string    `json:"-"`
	AlertError    *string    `json:"alert_error"`
}

func readCheckpointOrchestrator(path string) *CheckpointOrchestrator {
	if path == "" {
		return nil
	}
	data, err := os.ReadFile(path) // #nosec G304 -- operator-configured local snapshot path
	if err != nil {
		return nil
	}
	var status CheckpointOrchestrator
	if err := json.Unmarshal(data, &status); err != nil {
		return nil
	}
	if status.Version != checkpointSnapshotVersion || status.GeneratedAt.IsZero() ||
		status.RunnerState != "idle" || len(status.Checkpoints) == 0 {
		return nil
	}
	seen := make(map[string]struct{}, len(status.Checkpoints))
	for _, checkpoint := range status.Checkpoints {
		if checkpoint.Key == "" || checkpoint.Title == "" || checkpoint.Contract == "" ||
			checkpoint.State == "" || checkpoint.DueAt.IsZero() {
			return nil
		}
		if _, valid := checkpointStates[checkpoint.State]; !valid {
			return nil
		}
		if _, duplicate := seen[checkpoint.Key]; duplicate {
			return nil
		}
		seen[checkpoint.Key] = struct{}{}
		if checkpoint.ReportFile != nil &&
			(filepath.Base(*checkpoint.ReportFile) != *checkpoint.ReportFile || *checkpoint.ReportFile == ".") {
			return nil
		}
		if checkpoint.ReportSHA256 != nil {
			decoded, err := hex.DecodeString(*checkpoint.ReportSHA256)
			if err != nil || len(decoded) != sha256.Size {
				return nil
			}
		}
	}
	return &status
}

func checkpointSnapshotIsStale(now, generatedAt time.Time) bool {
	return generatedAt.After(now.Add(5*time.Minute)) || now.Sub(generatedAt) > checkpointSnapshotMaxAge
}
