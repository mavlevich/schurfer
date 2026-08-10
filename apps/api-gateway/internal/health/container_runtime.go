package health

import (
	"bufio"
	"encoding/json"
	"errors"
	"math"
	"os"
	"sort"
	"strconv"
	"strings"
)

const runtimeSnapshotVersion = "SCHURFER_RUNTIME_METRICS_V1"

type ContainerRuntime struct {
	CapturedAtMS         int64             `json:"captured_at_ms"`
	TotalCPUPercent      float64           `json:"total_cpu_percent"`
	TotalMemoryUsedBytes uint64            `json:"total_memory_used_bytes"`
	Containers           []ContainerMetric `json:"containers"`
}

type ContainerMetric struct {
	Name             string  `json:"name"`
	CPUPercent       float64 `json:"cpu_percent"`
	MemoryUsedBytes  uint64  `json:"memory_used_bytes"`
	MemoryLimitBytes uint64  `json:"memory_limit_bytes"`
	MemoryUsedPct    float64 `json:"memory_used_pct"`
	PIDs             int64   `json:"pids"`
	Status           string  `json:"status"`
	Health           string  `json:"health"`
	RestartCount     int64   `json:"restart_count"`
	StartedAt        string  `json:"started_at"`
	// OOMKilled distinguishes a container the kernel killed for memory from
	// any other "exited" reason (a clean stop, an intentional retirement).
	// Status/Health alone cannot tell these apart once the container is no
	// longer running.
	OOMKilled bool `json:"oom_killed"`
}

type dockerStatsLine struct {
	Name     string `json:"Name"`
	CPUPerc  string `json:"CPUPerc"`
	MemUsage string `json:"MemUsage"`
	MemPerc  string `json:"MemPerc"`
	PIDs     string `json:"PIDs"`
}

type dockerStateLine struct {
	Name         string `json:"name"`
	RestartCount int64  `json:"restart_count"`
	Status       string `json:"status"`
	Health       string `json:"health"`
	StartedAt    string `json:"started_at"`
	OOMKilled    bool   `json:"oom_killed"`
}

func readContainerRuntime(path string) *ContainerRuntime {
	file, err := os.Open(path) // #nosec G304 -- operator-configured local snapshot path
	if err != nil {
		return nil
	}
	defer func() { _ = file.Close() }()

	scanner := bufio.NewScanner(file)
	if !scanner.Scan() || scanner.Text() != runtimeSnapshotVersion {
		return nil
	}
	if !scanner.Scan() {
		return nil
	}
	capturedAtMS, err := strconv.ParseInt(scanner.Text(), 10, 64)
	if err != nil || capturedAtMS <= 0 {
		return nil
	}
	if !scanner.Scan() || scanner.Text() != "[stats]" {
		return nil
	}

	stats := make(map[string]dockerStatsLine)
	states := make(map[string]dockerStateLine)
	section := "stats"
	for scanner.Scan() {
		line := scanner.Text()
		if line == "[states]" {
			section = "states"
			continue
		}
		if strings.TrimSpace(line) == "" {
			continue
		}
		switch section {
		case "stats":
			var value dockerStatsLine
			if err := json.Unmarshal([]byte(line), &value); err != nil {
				return nil
			}
			name := normalizeContainerName(value.Name)
			if strings.HasPrefix(name, "schurfer-") {
				stats[name] = value
			}
		case "states":
			var value dockerStateLine
			if err := json.Unmarshal([]byte(line), &value); err != nil {
				return nil
			}
			name := normalizeContainerName(value.Name)
			if strings.HasPrefix(name, "schurfer-") {
				states[name] = value
			}
		default:
			return nil
		}
	}
	if scanner.Err() != nil || section != "states" {
		return nil
	}

	names := make(map[string]struct{}, len(stats)+len(states))
	for name := range stats {
		names[name] = struct{}{}
	}
	for name := range states {
		names[name] = struct{}{}
	}

	result := &ContainerRuntime{CapturedAtMS: capturedAtMS}
	for name := range names {
		metric, err := buildContainerMetric(name, stats[name], states[name])
		if err != nil {
			return nil
		}
		result.TotalCPUPercent += metric.CPUPercent
		result.TotalMemoryUsedBytes += metric.MemoryUsedBytes
		result.Containers = append(result.Containers, metric)
	}
	sort.Slice(result.Containers, func(i, j int) bool {
		if result.Containers[i].CPUPercent == result.Containers[j].CPUPercent {
			return result.Containers[i].Name < result.Containers[j].Name
		}
		return result.Containers[i].CPUPercent > result.Containers[j].CPUPercent
	})
	return result
}

func buildContainerMetric(
	name string,
	stats dockerStatsLine,
	state dockerStateLine,
) (ContainerMetric, error) {
	metric := ContainerMetric{
		Name:         name,
		Status:       state.Status,
		Health:       state.Health,
		RestartCount: state.RestartCount,
		StartedAt:    state.StartedAt,
		OOMKilled:    state.OOMKilled,
	}
	if stats.Name == "" {
		return metric, nil
	}

	cpu, err := parsePercent(stats.CPUPerc)
	if err != nil {
		return ContainerMetric{}, err
	}
	memoryParts := strings.Split(stats.MemUsage, "/")
	if len(memoryParts) != 2 {
		return ContainerMetric{}, errors.New("invalid Docker memory usage")
	}
	memoryUsed, err := parseDockerBytes(memoryParts[0])
	if err != nil {
		return ContainerMetric{}, err
	}
	memoryLimit, err := parseDockerBytes(memoryParts[1])
	if err != nil {
		return ContainerMetric{}, err
	}
	memoryPct, err := parsePercent(stats.MemPerc)
	if err != nil {
		return ContainerMetric{}, err
	}
	pids, err := strconv.ParseInt(strings.TrimSpace(stats.PIDs), 10, 64)
	if err != nil || pids < 0 {
		return ContainerMetric{}, errors.New("invalid Docker PID count")
	}
	metric.CPUPercent = cpu
	metric.MemoryUsedBytes = memoryUsed
	metric.MemoryLimitBytes = memoryLimit
	metric.MemoryUsedPct = memoryPct
	metric.PIDs = pids
	return metric, nil
}

func parsePercent(raw string) (float64, error) {
	value, err := strconv.ParseFloat(strings.TrimSuffix(strings.TrimSpace(raw), "%"), 64)
	if err != nil || math.IsNaN(value) || math.IsInf(value, 0) || value < 0 {
		return 0, errors.New("invalid Docker percentage")
	}
	return value, nil
}

func parseDockerBytes(raw string) (uint64, error) {
	value := strings.TrimSpace(raw)
	units := []struct {
		suffix     string
		multiplier float64
	}{
		{"GiB", 1024 * 1024 * 1024},
		{"MiB", 1024 * 1024},
		{"KiB", 1024},
		{"GB", 1000 * 1000 * 1000},
		{"MB", 1000 * 1000},
		{"kB", 1000},
		{"B", 1},
	}
	for _, unit := range units {
		if !strings.HasSuffix(value, unit.suffix) {
			continue
		}
		number := strings.TrimSpace(strings.TrimSuffix(value, unit.suffix))
		parsed, err := strconv.ParseFloat(number, 64)
		bytes := parsed * unit.multiplier
		if err != nil || math.IsNaN(bytes) || math.IsInf(bytes, 0) || bytes < 0 ||
			bytes > math.MaxUint64 {
			return 0, errors.New("invalid Docker byte value")
		}
		return uint64(bytes), nil
	}
	return 0, errors.New("unknown Docker byte unit")
}

func normalizeContainerName(value string) string {
	return strings.TrimPrefix(strings.TrimSpace(value), "/")
}
