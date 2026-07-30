package health

import (
	"bufio"
	"math"
	"os"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
)

type SystemLoad struct {
	CapturedAtMS      int64    `json:"captured_at_ms"`
	CPUCount          int      `json:"cpu_count"`
	CPUUtilizationPct *float64 `json:"cpu_utilization_pct"`
	LoadPressurePct   float64  `json:"load_pressure_pct"`
	Load1M            float64  `json:"load_1m"`
	Load5M            float64  `json:"load_5m"`
	Load15M           float64  `json:"load_15m"`
	MemoryUsedBytes   uint64   `json:"memory_used_bytes"`
	MemoryTotalBytes  uint64   `json:"memory_total_bytes"`
	MemoryUsedPct     float64  `json:"memory_used_pct"`
	SwapUsedBytes     uint64   `json:"swap_used_bytes"`
	SwapTotalBytes    uint64   `json:"swap_total_bytes"`
	SwapUsedPct       float64  `json:"swap_used_pct"`
	DiskUsedBytes     uint64   `json:"disk_used_bytes"`
	DiskTotalBytes    uint64   `json:"disk_total_bytes"`
	DiskUsedPct       float64  `json:"disk_used_pct"`
	SystemUptimeSecs  float64  `json:"system_uptime_seconds"`
}

type procSnapshot struct {
	load1M           float64
	load5M           float64
	load15M          float64
	memoryUsedBytes  uint64
	memoryTotalBytes uint64
	swapUsedBytes    uint64
	swapTotalBytes   uint64
	uptimeSeconds    float64
}

type cpuTimes struct {
	total uint64
	idle  uint64
}

type systemSampler struct {
	mu          sync.Mutex
	procRoot    string
	diskRoot    string
	previous    *cpuTimes
	lastRead    time.Time
	minInterval time.Duration
	cached      *SystemLoad
}

func newSystemProbe(procRoot string, diskRoot string) func() *SystemLoad {
	sampler := &systemSampler{
		procRoot:    procRoot,
		diskRoot:    diskRoot,
		minInterval: 2 * time.Second,
	}
	return sampler.sample
}

func (s *systemSampler) sample() *SystemLoad {
	s.mu.Lock()
	defer s.mu.Unlock()

	now := time.Now()
	if s.cached != nil && now.Sub(s.lastRead) < s.minInterval {
		return s.cached
	}

	snapshot, ok := readProcSnapshot(s.procRoot)
	if !ok {
		return nil
	}
	currentCPU, ok := readCPUTimes(s.procRoot + "/stat")
	if !ok {
		return nil
	}
	var stats syscall.Statfs_t
	if err := syscall.Statfs(s.diskRoot, &stats); err != nil {
		return nil
	}

	var cpuUtilization *float64
	if s.previous != nil && currentCPU.total > s.previous.total &&
		currentCPU.idle >= s.previous.idle {
		totalDelta := currentCPU.total - s.previous.total
		idleDelta := currentCPU.idle - s.previous.idle
		if idleDelta <= totalDelta {
			value := float64(totalDelta-idleDelta) / float64(totalDelta) * 100
			cpuUtilization = &value
		}
	}
	s.previous = &currentCPU

	blockSize := uint64(stats.Bsize) // #nosec G115 -- filesystem block sizes are positive
	totalDisk := stats.Blocks * blockSize
	availableDisk := stats.Bavail * blockSize
	usedDisk := totalDisk - availableDisk
	cpuCount := runtime.NumCPU()
	result := &SystemLoad{
		CapturedAtMS:      now.UnixMilli(),
		CPUCount:          cpuCount,
		CPUUtilizationPct: cpuUtilization,
		LoadPressurePct:   snapshot.load1M / float64(cpuCount) * 100,
		Load1M:            snapshot.load1M,
		Load5M:            snapshot.load5M,
		Load15M:           snapshot.load15M,
		MemoryUsedBytes:   snapshot.memoryUsedBytes,
		MemoryTotalBytes:  snapshot.memoryTotalBytes,
		MemoryUsedPct:     percent(snapshot.memoryUsedBytes, snapshot.memoryTotalBytes),
		SwapUsedBytes:     snapshot.swapUsedBytes,
		SwapTotalBytes:    snapshot.swapTotalBytes,
		SwapUsedPct:       percent(snapshot.swapUsedBytes, snapshot.swapTotalBytes),
		DiskUsedBytes:     usedDisk,
		DiskTotalBytes:    totalDisk,
		DiskUsedPct:       percent(usedDisk, totalDisk),
		SystemUptimeSecs:  snapshot.uptimeSeconds,
	}
	s.lastRead = now
	s.cached = result
	return result
}

func readProcSnapshot(root string) (procSnapshot, bool) {
	load, err := os.ReadFile(root + "/loadavg") // #nosec G304 -- fixed /proc root or test fixture
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

	totalMemory, availableMemory, totalSwap, freeSwap, ok := readMemory(root + "/meminfo")
	if !ok || availableMemory > totalMemory {
		return procSnapshot{}, false
	}
	if freeSwap > totalSwap {
		return procSnapshot{}, false
	}
	uptimeRaw, err := os.ReadFile(root + "/uptime") // #nosec G304 -- fixed /proc root or test fixture
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
		swapUsedBytes:    totalSwap - freeSwap,
		swapTotalBytes:   totalSwap,
		uptimeSeconds:    uptime,
	}, true
}

func readMemory(path string) (uint64, uint64, uint64, uint64, bool) {
	file, err := os.Open(path) // #nosec G304 -- fixed /proc path or test fixture
	if err != nil {
		return 0, 0, 0, 0, false
	}
	defer func() { _ = file.Close() }()

	var totalKB uint64
	var availableKB uint64
	var swapTotalKB uint64
	var swapFreeKB uint64
	var foundTotal bool
	var foundAvailable bool
	var foundSwapTotal bool
	var foundSwapFree bool
	scanner := bufio.NewScanner(file)
	for scanner.Scan() {
		fields := strings.Fields(scanner.Text())
		if len(fields) < 2 {
			continue
		}
		value, err := strconv.ParseUint(fields[1], 10, 64)
		if err != nil {
			return 0, 0, 0, 0, false
		}
		switch fields[0] {
		case "MemTotal:":
			totalKB = value
			foundTotal = true
		case "MemAvailable:":
			availableKB = value
			foundAvailable = true
		case "SwapTotal:":
			swapTotalKB = value
			foundSwapTotal = true
		case "SwapFree:":
			swapFreeKB = value
			foundSwapFree = true
		}
	}
	if scanner.Err() != nil || !foundTotal || !foundAvailable || !foundSwapTotal ||
		!foundSwapFree || totalKB == 0 {
		return 0, 0, 0, 0, false
	}
	return totalKB * 1024, availableKB * 1024, swapTotalKB * 1024, swapFreeKB * 1024, true
}

func readCPUTimes(path string) (cpuTimes, bool) {
	raw, err := os.ReadFile(path) // #nosec G304 -- fixed /proc path or test fixture
	if err != nil {
		return cpuTimes{}, false
	}
	line, _, ok := strings.Cut(string(raw), "\n")
	if !ok || !strings.HasPrefix(line, "cpu ") {
		return cpuTimes{}, false
	}
	fields := strings.Fields(line)
	if len(fields) < 5 {
		return cpuTimes{}, false
	}
	values := make([]uint64, 0, len(fields)-1)
	for _, field := range fields[1:] {
		value, err := strconv.ParseUint(field, 10, 64)
		if err != nil {
			return cpuTimes{}, false
		}
		values = append(values, value)
	}
	var total uint64
	for _, value := range values {
		total += value
	}
	idle := values[3]
	if len(values) > 4 {
		idle += values[4]
	}
	if total == 0 || idle > total {
		return cpuTimes{}, false
	}
	return cpuTimes{total: total, idle: idle}, true
}

func percent(used uint64, total uint64) float64 {
	if total == 0 {
		return 0
	}
	return float64(used) / float64(total) * 100
}
