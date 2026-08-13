# Scripts

Status: active operational support code.

This directory contains host telemetry, backup, research-checkpoint, and canary
checkpoint scripts. The production procedures and supported Make targets are
documented in [`docs/runbooks/README.md`](../../docs/runbooks/README.md). Scripts are
implementation details of those runbooks, not independent instructions.

Rules:

- never put credentials in command-line arguments or committed files;
- use durable systemd timers for scheduled work, not one-off crontab entries;
- make retries and notification delivery state explicit;
- keep production mutations behind guarded Make targets;
- update the runbook in the same PR when operational behavior changes.
