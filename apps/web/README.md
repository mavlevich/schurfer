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
