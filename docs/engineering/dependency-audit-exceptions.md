# Node dependency audit exceptions

Status: current. Owner of the review: the engineer who next touches `apps/web`
dependencies, or whoever sees the `Security scan` job fail, whichever comes first.

CI runs `pnpm audit --audit-level=high` (job `Security scan`, step "Node dependency
audit"). The only way to silence a finding is an entry in `pnpm.auditConfig.ignoreGhsas`
in the root [`package.json`](../../package.json). JSON has no comments, so every entry
must be listed here with its reason, reach, and a review date. An entry is allowed only
when the advisory lists no patched release; if a fix exists, bump it through
`pnpm.overrides` instead. Each entry names one GHSA id; a broader mute is not allowed.

| GHSA                  | Package     | Severity | Added      | Review by  |
| --------------------- | ----------- | -------- | ---------- | ---------- |
| `GHSA-vfj7-8cjw-p6xm` | `braces`    | high     | 2026-10-04 | 2026-12-18 |
| `GHSA-fc5q-39xc-vjpf` | `picomatch` | high     | 2026-11-18 | 2026-12-18 |

## GHSA-fc5q-39xc-vjpf: picomatch ReDoS through repeated extglobs

- Advisory: `picomatch <=4.0.7`, high, published 2026-10-19, "Patched versions"
  `<0.0.0` (no fixed release). `picomatch@4.0.7` is the latest release on npm
  (2026-08-24) and the lockfile already uses it, so a `pnpm.overrides` bump has no
  target. Checked 2026-11-18.
- Reach: build-time dev tooling of `apps/web` only (`tailwindcss>chokidar>anymatch`,
  `tailwindcss>sucrase>tinyglobby`, `vite`). The web image copies only the static
  `dist/` into nginx (`apps/web/Dockerfile`), so `picomatch` is not shipped or run in
  production.
- Exploit input: glob patterns come from our own `tailwind.config.ts` and Vite config,
  not from users, the network, or any service payload. The ReDoS needs a crafted
  pattern, so the worst case is a slow local or CI build.
- Removal: delete the entry when `picomatch` publishes a fixed release (bump it via
  `pnpm.overrides` and run `pnpm audit`), or when `vite` and `tailwindcss` stop
  depending on it. If neither has happened by the review date, re-check and extend the
  date here in a reviewed change.

## GHSA-vfj7-8cjw-p6xm: braces

Added in #487 on the same grounds (no patched release, reached only through
`tailwindcss>chokidar` and `micromatch` at CSS build time, patterns from our own
config, static `dist` shipped). Recorded here so both exceptions sit in one place.
Re-check whether a fixed `braces` exists by the review date.
