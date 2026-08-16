package health

import (
	"bufio"
	"encoding/json"
	"os"
	"strconv"
	"strings"
)

const diskUsageSnapshotVersion = "SCHURFER_DISK_USAGE_V1"

// postgresVolumeName is the actual Docker volume name docker-compose creates
// for the postgres service's own data mount (infra/docker/docker-compose.
// prod.yml's own postgres_data volume, prefixed by the Compose project name
// "docker" -- see docker inspect's own com.docker.compose.project label).
const postgresVolumeName = "docker_postgres_data"

// DiskUsage breaks down host disk usage into what a deploy-time cleanup
// decision actually needs to know: which of these are safe, reclaimable
// build artifacts (images, build cache) versus real data (Postgres,
// backups) that must never be pruned. Read from a host-side snapshot file
// (see infra/scripts/disk-usage.sh, same pattern as ContainerRuntime/
// readContainerRuntime) because none of this is visible from inside the
// api-gateway container itself: `docker system df` needs the host's own
// Docker socket, which api-gateway is deliberately never given (see this
// package's own SystemLoad, which reads /proc and statfs directly instead
// of shelling out to docker, for the same reason).
type DiskUsage struct {
	CapturedAtMS               int64  `json:"captured_at_ms"`
	ImagesBytes                uint64 `json:"images_bytes"`
	ImagesReclaimableBytes     uint64 `json:"images_reclaimable_bytes"`
	ContainersBytes            uint64 `json:"containers_bytes"`
	VolumesBytes               uint64 `json:"volumes_bytes"`
	BuildCacheBytes            uint64 `json:"build_cache_bytes"`
	BuildCacheReclaimableBytes uint64 `json:"build_cache_reclaimable_bytes"`
	PostgresDataBytes          uint64 `json:"postgres_data_bytes"`
	BackupsBytes               uint64 `json:"backups_bytes"`
}

type dockerDFTypeLine struct {
	Type        string `json:"Type"`
	Size        string `json:"Size"`
	Reclaimable string `json:"Reclaimable"`
}

type dockerVolumeLine struct {
	Name string `json:"Name"`
	Size string `json:"Size"`
}

// readDiskUsage parses infra/scripts/disk-usage.sh's own snapshot format:
// a version header, a captured-at timestamp, then [docker_summary] (one
// `docker system df --format '{{json .}}'` line per resource type),
// [docker_volumes] (one line holding `docker system df -v --format
// '{{json .Volumes}}'`'s own JSON array), and [extra] (plain key=value
// lines for numbers Docker itself doesn't report, like the backups
// directory's own size). Fails closed (returns nil) on any structural
// mismatch, matching readContainerRuntime's own convention -- a stale
// dashboard field is confusing, but a wrong one that looks current is
// worse for a cleanup decision.
func readDiskUsage(path string) *DiskUsage {
	file, err := os.Open(path) // #nosec G304 -- operator-configured local snapshot path
	if err != nil {
		return nil
	}
	defer func() { _ = file.Close() }()

	scanner := bufio.NewScanner(file)
	if !scanner.Scan() || scanner.Text() != diskUsageSnapshotVersion {
		return nil
	}
	if !scanner.Scan() {
		return nil
	}
	capturedAtMS, err := strconv.ParseInt(scanner.Text(), 10, 64)
	if err != nil || capturedAtMS <= 0 {
		return nil
	}
	if !scanner.Scan() || scanner.Text() != "[docker_summary]" {
		return nil
	}

	result := &DiskUsage{CapturedAtMS: capturedAtMS}
	section := "docker_summary"
	for scanner.Scan() {
		line := scanner.Text()
		switch line {
		case "[docker_volumes]":
			section = "docker_volumes"
			continue
		case "[extra]":
			section = "extra"
			continue
		}
		if strings.TrimSpace(line) == "" {
			continue
		}
		switch section {
		case "docker_summary":
			if !applyDockerSummaryLine(result, line) {
				return nil
			}
		case "docker_volumes":
			if !applyDockerVolumesLine(result, line) {
				return nil
			}
		case "extra":
			if !applyExtraLine(result, line) {
				return nil
			}
		default:
			return nil
		}
	}
	if scanner.Err() != nil {
		return nil
	}
	return result
}

func applyDockerSummaryLine(result *DiskUsage, line string) bool {
	var row dockerDFTypeLine
	if err := json.Unmarshal([]byte(line), &row); err != nil {
		return false
	}
	size, err := parseDockerBytes(row.Size)
	if err != nil {
		return false
	}
	reclaimable, err := parseReclaimableBytes(row.Reclaimable)
	if err != nil {
		return false
	}
	switch row.Type {
	case "Images":
		result.ImagesBytes = size
		result.ImagesReclaimableBytes = reclaimable
	case "Containers":
		result.ContainersBytes = size
	case "Local Volumes":
		result.VolumesBytes = size
	case "Build Cache":
		result.BuildCacheBytes = size
		result.BuildCacheReclaimableBytes = reclaimable
	default:
		return false
	}
	return true
}

func applyDockerVolumesLine(result *DiskUsage, line string) bool {
	var volumes []dockerVolumeLine
	if err := json.Unmarshal([]byte(line), &volumes); err != nil {
		return false
	}
	for _, volume := range volumes {
		if volume.Name != postgresVolumeName {
			continue
		}
		size, err := parseDockerBytes(volume.Size)
		if err != nil {
			return false
		}
		result.PostgresDataBytes = size
	}
	return true
}

func applyExtraLine(result *DiskUsage, line string) bool {
	key, value, ok := strings.Cut(line, "=")
	if !ok {
		return false
	}
	if key != "backups_bytes" {
		// Forward-compatible: a future extra field this reader doesn't know
		// about yet is ignored rather than failing the whole snapshot closed.
		return true
	}
	parsed, err := strconv.ParseUint(value, 10, 64)
	if err != nil {
		return false
	}
	result.BackupsBytes = parsed
	return true
}

// parseReclaimableBytes strips docker system df's own trailing "(NN%)"
// annotation ("751.7MB (25%)" -> "751.7MB") before reusing parseDockerBytes
// (container_runtime.go's own byte-unit parser, shared since both come from
// the same docker CLI output conventions).
func parseReclaimableBytes(raw string) (uint64, error) {
	trimmed := raw
	if idx := strings.Index(raw, " ("); idx != -1 {
		trimmed = raw[:idx]
	}
	return parseDockerBytes(trimmed)
}
