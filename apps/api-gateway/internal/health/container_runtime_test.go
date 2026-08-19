package health

import (
	"os"
	"path/filepath"
	"testing"
)

func TestReadContainerRuntime(t *testing.T) {
	path := filepath.Join(t.TempDir(), "container-metrics.snapshot")
	snapshot := `SCHURFER_RUNTIME_METRICS_V1
1785245544572
[stats]
{"Name":"schurfer-api-gateway","CPUPerc":"2.50%","MemUsage":"105.8MiB / 384MiB","MemPerc":"27.55%","PIDs":"12"}
{"Name":"unrelated","CPUPerc":"99.00%","MemUsage":"1GiB / 2GiB","MemPerc":"50.00%","PIDs":"2"}
{"Name":"schurfer-collector","CPUPerc":"12.75%","MemUsage":"8.5MiB / 3.725GiB","MemPerc":"0.22%","PIDs":"8"}
[states]
{"name":"/schurfer-api-gateway","restart_count":1,"status":"running","health":"healthy","started_at":"2026-07-30T10:00:00Z","oom_killed":false}
{"name":"/schurfer-collector","restart_count":2,"status":"restarting","health":"none","started_at":"2026-07-30T11:00:00Z","oom_killed":true}
{"name":"/schurfer-orderflow-pilot","restart_count":0,"status":"exited","health":"none","started_at":"2026-07-30T09:00:00Z","oom_killed":false}
`
	if err := os.WriteFile(path, []byte(snapshot), 0o600); err != nil {
		t.Fatal(err)
	}

	got := readContainerRuntime(path)
	if got == nil {
		t.Fatal("expected runtime telemetry")
	}
	if got.CapturedAtMS != 1785245544572 || len(got.Containers) != 3 {
		t.Fatalf("unexpected snapshot: %+v", got)
	}
	if got.Containers[0].Name != "schurfer-collector" {
		t.Fatalf("expected CPU-descending ordering, got %+v", got.Containers)
	}
	if got.TotalCPUPercent != 15.25 {
		t.Fatalf("unexpected total CPU: %f", got.TotalCPUPercent)
	}
	collector := got.Containers[0]
	if !collector.OOMKilled || collector.Status != "restarting" {
		t.Fatalf("unexpected collector metric: %+v", collector)
	}
	api := got.Containers[1]
	if api.RestartCount != 1 || api.Health != "healthy" || api.MemoryLimitBytes != 384*1024*1024 {
		t.Fatalf("unexpected api-gateway metric: %+v", api)
	}
	if api.OOMKilled {
		t.Fatalf("expected api-gateway to not be OOM-killed: %+v", api)
	}
	stopped := got.Containers[2]
	if stopped.Name != "schurfer-orderflow-pilot" || stopped.Status != "exited" ||
		stopped.CPUPercent != 0 || stopped.OOMKilled {
		t.Fatalf("unexpected stopped-container metric: %+v", stopped)
	}
}

func TestReadContainerRuntimeFailsClosed(t *testing.T) {
	path := filepath.Join(t.TempDir(), "container-metrics.snapshot")
	if err := os.WriteFile(path, []byte("wrong\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if got := readContainerRuntime(path); got != nil {
		t.Fatalf("expected malformed snapshot to fail closed, got %+v", got)
	}
}
