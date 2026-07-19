# Contributing

## Branching

GitHub Flow: `main` is always deployable, every change goes through a feature branch + PR.

```
main
  └── feat/docker-compose-dev
  └── fix/nats-reconnect
  └── chore/ci-setup
  └── docs/update-roadmap
```

### Branch naming

```
feat/short-description    - new functionality
fix/what-is-broken        - bug fix
chore/infra-or-tooling    - CI, config, refactoring
docs/what-documented      - documentation only
test/what-is-tested       - test-only changes
```

### Rules

- `main` is protected - no direct pushes
- Every change → branch → PR → squash merge → delete branch
- PR runs CI before merge
- Self-review is fine (solo project), but PR history is kept for traceability

## Commits

[Conventional Commits](https://www.conventionalcommits.org/):

```
feat: add bybit ws collector
fix: handle nats reconnection on timeout
chore: add docker-compose for local dev
docs: update roadmap with sprint 2 plan
test: add pump detector threshold tests
refactor: extract nats publisher from collector
```

Scope is optional but encouraged for multi-service changes:

```
feat(collector): add funding rate stream
fix(telegram): handle callback timeout
chore(ci): add arm64 docker build
```

## Pull requests

- Keep PRs focused - one feature/fix per PR
- Use the PR template (`.github/pull_request_template.md`)
- Squash merge to keep `main` history clean
- Delete branch after merge

## Code style

- Python: `ruff` (format + lint), configured in `pyproject.toml`
- Go: `gofmt` + `go vet`
- TypeScript: `prettier` + `eslint`
- Pre-commit hooks enforce formatting on commit
