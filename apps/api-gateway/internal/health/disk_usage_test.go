package health

import (
	"os"
	"path/filepath"
	"testing"
)

func TestReadDiskUsage(t *testing.T) {
	path := filepath.Join(t.TempDir(), "disk-usage.snapshot")
	snapshot := `SCHURFER_DISK_USAGE_V1
1785245544572
[docker_summary]
{"Active":"20","Reclaimable":"751.7MB (25%)","Size":"2.898GB","TotalCount":"22","Type":"Images"}
{"Active":"19","Reclaimable":"0B (0%)","Size":"250.6MB","TotalCount":"20","Type":"Containers"}
{"Active":"6","Reclaimable":"0B (0%)","Size":"8.835GB","TotalCount":"6","Type":"Local Volumes"}
{"Active":"0","Reclaimable":"15.56GB","Size":"15.56GB","TotalCount":"403","Type":"Build Cache"}
[docker_volumes]
[{"Name":"docker_caddy_data","Size":"6.169kB"},{"Name":"docker_postgres_data","Size":"8.215GB"}]
[extra]
backups_bytes=15032385536
`
	if err := os.WriteFile(path, []byte(snapshot), 0o600); err != nil {
		t.Fatal(err)
	}

	got := readDiskUsage(path)
	if got == nil {
		t.Fatal("expected disk usage telemetry")
	}
	if got.CapturedAtMS != 1785245544572 {
		t.Fatalf("unexpected captured_at_ms: %d", got.CapturedAtMS)
	}
	if got.ImagesBytes != 2_898_000_000 || got.ImagesReclaimableBytes != 751_700_000 {
		t.Fatalf("unexpected images: %+v", got)
	}
	if got.ContainersBytes != 250_600_000 {
		t.Fatalf("unexpected containers bytes: %d", got.ContainersBytes)
	}
	if got.VolumesBytes != 8_835_000_000 {
		t.Fatalf("unexpected volumes bytes: %d", got.VolumesBytes)
	}
	if got.BuildCacheBytes != 15_560_000_000 || got.BuildCacheReclaimableBytes != 15_560_000_000 {
		t.Fatalf("unexpected build cache: %+v", got)
	}
	if got.PostgresDataBytes != 8_215_000_000 {
		t.Fatalf("unexpected postgres data bytes: %d, want the docker_postgres_data row only", got.PostgresDataBytes)
	}
	if got.BackupsBytes != 15_032_385_536 {
		t.Fatalf("unexpected backups bytes: %d", got.BackupsBytes)
	}
}

// TestReadDiskUsageIgnoresUnrelatedVolumes is a regression: the
// [docker_volumes] section lists every volume on the host (caddy_data,
// redis_data, nats_data, ...), not just Postgres's own -- only the row
// named postgresVolumeName may ever set PostgresDataBytes.
func TestReadDiskUsageIgnoresUnrelatedVolumes(t *testing.T) {
	path := filepath.Join(t.TempDir(), "disk-usage.snapshot")
	snapshot := `SCHURFER_DISK_USAGE_V1
1785245544572
[docker_summary]
{"Active":"0","Reclaimable":"0B (0%)","Size":"0B","TotalCount":"0","Type":"Images"}
{"Active":"0","Reclaimable":"0B (0%)","Size":"0B","TotalCount":"0","Type":"Containers"}
{"Active":"0","Reclaimable":"0B (0%)","Size":"0B","TotalCount":"0","Type":"Local Volumes"}
{"Active":"0","Reclaimable":"0B (0%)","Size":"0B","TotalCount":"0","Type":"Build Cache"}
[docker_volumes]
[{"Name":"docker_caddy_data","Size":"6.169kB"},{"Name":"docker_redis_data","Size":"52.94MB"}]
[extra]
backups_bytes=0
`
	if err := os.WriteFile(path, []byte(snapshot), 0o600); err != nil {
		t.Fatal(err)
	}

	got := readDiskUsage(path)
	if got == nil {
		t.Fatal("expected disk usage telemetry")
	}
	if got.PostgresDataBytes != 0 {
		t.Fatalf("expected 0 (no docker_postgres_data row present), got %d", got.PostgresDataBytes)
	}
}

func TestReadDiskUsageFailsClosed(t *testing.T) {
	path := filepath.Join(t.TempDir(), "disk-usage.snapshot")
	if err := os.WriteFile(path, []byte("wrong\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if got := readDiskUsage(path); got != nil {
		t.Fatalf("expected malformed snapshot to fail closed, got %+v", got)
	}
}

func TestReadDiskUsageFailsClosedOnUnknownType(t *testing.T) {
	path := filepath.Join(t.TempDir(), "disk-usage.snapshot")
	snapshot := `SCHURFER_DISK_USAGE_V1
1785245544572
[docker_summary]
{"Active":"0","Reclaimable":"0B (0%)","Size":"0B","TotalCount":"0","Type":"Something New Docker Added"}
[docker_volumes]
[]
[extra]
backups_bytes=0
`
	if err := os.WriteFile(path, []byte(snapshot), 0o600); err != nil {
		t.Fatal(err)
	}
	if got := readDiskUsage(path); got != nil {
		t.Fatalf("expected unknown docker df type to fail closed, got %+v", got)
	}
}

func TestParseReclaimableBytes(t *testing.T) {
	cases := map[string]uint64{
		"751.7MB (25%)": 751_700_000,
		"0B (0%)":       0,
		"15.56GB":       15_560_000_000,
	}
	for raw, want := range cases {
		got, err := parseReclaimableBytes(raw)
		if err != nil {
			t.Fatalf("parseReclaimableBytes(%q) error: %v", raw, err)
		}
		if got != want {
			t.Errorf("parseReclaimableBytes(%q) = %d, want %d", raw, got, want)
		}
	}
}
