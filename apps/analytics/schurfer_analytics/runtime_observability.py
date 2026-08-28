"""Low-overhead runtime diagnostics for bounded analytics reports."""

from __future__ import annotations

import resource
import sys


def peak_rss_mib() -> float:
    """Return the process peak RSS in MiB on Linux and macOS."""
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    rss_bytes = raw if sys.platform == "darwin" else raw * 1024
    return rss_bytes / (1024 * 1024)


def log_report_phase(report: str, phase: str, **counts: int) -> None:
    """Write sanitized phase and peak-memory telemetry to stderr."""
    dimensions = " ".join(f"{key}={value}" for key, value in sorted(counts.items()))
    suffix = f" {dimensions}" if dimensions else ""
    sys.stderr.write(
        f"research_report_phase report={report} phase={phase} "
        f"peak_rss_mib={peak_rss_mib():.1f}{suffix}\n"
    )
