# Abnormal-flow economic screen v1 - Formal Run

This directory contains the results of the formal returns run executed on the prospective evaluation window.

## Execution Environment

*   **Git Revision:** `47dca46e66c38ffcb56c3cc3283cdf4a51e420d2`
*   **Command Executed:**
    ```bash
    uv run python /tmp/run_formal5.py
    ```
    *(A scratch Python script was used to monkeypatch `FORMAL_RETURNS_RUN_ENABLED = True` and invoke `FormalReplay` on the `abnormal_flow_scan` decisions, as the CLI runner was not yet merged into `main` per PR rules).*
*   **Time:** 2026-09-23T04:57:05Z (Completion Time)
*   **Resources:** ~39 minutes, 1 process reading 30 million DuckDB Parquet rows.
*   **Input Hashes:**
    See `evaluation_manifest.json` for pinned artifacts.

## Artifacts

*   `contract.json`: The frozen decision rules and thresholds.
*   `evaluation_manifest.json`: The integrity gates (SHA hashes) enforcing exact dataset fidelity.
*   `formal_run_report.json`: The outcome-bearing results of the formal replay.
