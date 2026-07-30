package health

import (
	"bufio"
	"math"
	"os"
	"runtime"
	"strconv"
	"strings"
	"syscall"
	"time"
)

type SystemLoad struct {
	CapturedAtMS     int64   `json:"captured_at_ms"`
	CPUCount         int     `json:"cpu_count"`
	Load1M           float64 `json:"load_1m"`
	Load5M           float64 `json:"load_5m"`
	Load15M          float64 `json:"load_15m"`
	MemoryUsedBytes  uint64  `json:"memory_used_bytes"`
	MemoryTotalBytes uint64  `json:"memory_total_bytes"`
	MemoryUsedPct    float64 `json:"memory_used_pct"`
	DiskUsedBytes    uint64  `json:"disk_used_bytes"`
	DiskTotalBytes   uint64  `json:"disk_total_bytes"`
	DiskUsedPct      float64 `json:"disk_used_pct"`
	SystemUptimeSecs float64 `json:"system_uptime_seconds"`
}

type procSnapshot struct {
	load1M           float64
	load5M           float64
	load15M          float64
	memoryUsedBytes  uint64
	memoryTotalBytes uint64
	uptimeSeconds    float64
}

func readSystemLoad() *SystemLoad {
	snapshot, ok := readProcSnapshot("/proc")
	if !ok {
		return nil
	}
	var stats syscall.Statfs_t
	if err := syscall.Statfs("/", &stats); err != nil {
		return nil
	}
	blockSize := uint64(stats.Bsize) // #nosec G115 -- filesystem block sizes are positive
	totalDisk := stats.Blocks * blockSize
	availableDisk := stats.Bavail * blockSize
	usedDisk := totalDisk - availableDisk
	return &SystemLoad{
		CapturedAtMS:     time.Now().UnixMilli(),
		CPUCount:         runtime.NumCPU(),
		Load1M:           snapshot.load1M,
		Load5M:           snapshot.load5M,
		Load15M:          snapshot.load15M,
		MemoryUsedBytes:  snapshot.memoryUsedBytes,
		MemoryTotalBytes: snapshot.memoryTotalBytes,
		MemoryUsedPct:    percent(snapshot.memoryUsedBytes, snapshot.memoryTotalBytes),
		DiskUsedBytes:    usedDisk,
		DiskTotalBytes:   totalDisk,
		DiskUsedPct:      percent(usedDisk, totalDisk),
		SystemUptimeSecs: snapshot.uptimeSeconds,
	}
}

func readProcSnapshot(root string) (procSnapshot, bool) {
	load, err := os.ReadFile(root + "/loadavg")
	if err != nil {
		return procSnapshot{}, false
	}
	loadFields := strings.Fields(string(load))
	if len(loadFields) < 3 {
		return procSnapshot{}, false
	}
	load1M, err1 := strconv.ParseFloat(loadFields[0], 64)
	load5M, err5 := strconv.ParseFloat(loadFields[1], 64)
	load15M, err15 := strconv.ParseFloat(loadFields[2], 64)
	if err1 != nil || err5 != nil || err15 != nil {
		return procSnapshot{}, false
	}

	totalMemory, availableMemory, ok := readMemory(root + "/meminfo")
	if !ok || availableMemory > totalMemory {
		return procSnapshot{}, false
	}
	uptimeRaw, err := os.ReadFile(root + "/uptime")
	if err != nil {
		return procSnapshot{}, false
	}
	uptimeFields := strings.Fields(string(uptimeRaw))
	if len(uptimeFields) == 0 {
		return procSnapshot{}, false
	}
	uptime, err := strconv.ParseFloat(uptimeFields[0], 64)
	if err != nil || math.IsNaN(uptime) || math.IsInf(uptime, 0) || uptime < 0 {
		return procSnapshot{}, false
	}
	return procSnapshot{
		load1M:           load1M,
		load5M:           load5M,
		load15M:          load15M,
		memoryUsedBytes:  totalMemory - availableMemory,
		memoryTotalBytes: totalMemory,
		uptimeSeconds:    uptime,
	}, true
}

func readMemory(path string) (uint64, uint64, bool) {
	file, err := os.Open(path)
	if err != nil {
		return 0, 0, false
	}
	defer func() { _ = file.Close() }()

	var totalKB uint64
	var availableKB uint64
	var foundTotal bool
	var foundAvailable bool
	scanner := bufio.NewScanner(file)
	for scanner.Scan() {
		fields := strings.Fields(scanner.Text())
		if len(fields) < 2 {
			continue
		}
		value, err := strconv.ParseUint(fields[1], 10, 64)
		if err != nil {
			return 0, 0, false
		}
		switch fields[0] {
		case "MemTotal:":
			totalKB = value
			foundTotal = true
		case "MemAvailable:":
			availableKB = value
			foundAvailable = true
		}
	}
	if scanner.Err() != nil || !foundTotal || !foundAvailable || totalKB == 0 {
		return 0, 0, false
	}
	return totalKB * 1024, availableKB * 1024, true
}

func percent(used uint64, total uint64) float64 {
	if total == 0 {
		return 0
	}
	return float64(used) / float64(total) * 100
}
