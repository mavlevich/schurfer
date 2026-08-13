# web

React + Vite + TypeScript dashboard.
Communicates with api-gateway via REST and WebSocket.

Authenticated pages use the shared `PageShell` width and spacing contract:

- `narrow` for focused account controls;
- `content` for operational status;
- `wide` for scanners, journals, and research.

The Research page surfaces collection progress without running heavy reports in the
request path. Proxy counters are marked as estimates and never presented as a
strategy verdict. It also shows input-scope diagnostics and bounded metadata for the
latest successful HYP-008/HYP-010 report; full episode and market-path payloads are
not exposed through the web application.

Routes are loaded lazily so the charting bundle is fetched only for token-detail
pages instead of increasing the initial payload for every dashboard page.

The reviewed target information architecture, shared design contract, canonical
token workspace, chart-event plan, Research readiness performance model, and bounded
delivery sequence live in
[`docs/architecture/web-ui-evolution-v1.md`](../../docs/architecture/web-ui-evolution-v1.md).
It is an incremental plan, not authorization for a frontend rewrite or for delaying
capture, strategy, and execution work.
