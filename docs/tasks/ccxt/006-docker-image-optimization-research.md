# CCXT-006: Measure development image optimization opportunities

> Status: backlog; research only
> Depends on: CCXT-005 merged or closed
> Produces: measurements and either one focused proposal or a documented no-go

## Goal

Determine whether CCXT's multi-language development image can be made materially
smaller or faster to rebuild without reducing its supported toolchain or making the
Dockerfile harder to maintain.

This is not part of the Apple Silicon correctness fix. No upstream optimization
issue or pull request should be opened until measurements show a meaningful,
repeatable improvement.

## Current observations to verify

The root Dockerfile appears to contain several possible sources of unnecessary work:

- repeated `apt-get update` and package installation layers;
- package-index files that may remain in image layers;
- separate Python dependency installation layers;
- dependency installation after broad source copies, which may reduce warm-build
  cache reuse;
- npm, pip, apt, Composer, and SDK caches that may survive in the final development
  image;
- a universal image containing every language toolchain even when a contributor
  works on only one binding.

These are hypotheses, not confirmed defects. A development image intentionally
contains compilers and SDKs, so a large size alone is not evidence of waste.

## Baseline

Record on both `linux/arm64` and `linux/amd64` where practical:

- final compressed and unpacked image size;
- size contribution of each layer from `docker history`;
- cold build duration with an empty BuildKit cache;
- warm no-change rebuild duration;
- rebuild duration after changing one TypeScript source file;
- rebuild duration after changing package metadata;
- peak disk use during the build;
- whether the full `npm run build` still succeeds.

Store the exact commit, Docker version, BuildKit version, host architecture, and
commands with the results.

## Candidates to test independently

1. Consolidate compatible apt operations and remove package lists in the same layer.
2. Consolidate stable Python tooling installation where it improves layer reuse.
3. Reorder copies and dependency installation only if repository structure permits a
   narrower cache key.
4. Evaluate BuildKit cache mounts for apt, npm, and pip without making non-BuildKit
   builds incorrect.
5. Remove installer archives and disposable package-manager caches from their
   creating layers.
6. Evaluate specialized images only as a separate design proposal. Preserve the
   existing universal development image unless maintainers explicitly prefer a
   split.

Do not combine all candidates into one patch. Each accepted change must have a clear
cause, measured benefit, and regression test or reproducible verification.

## Required safeguards

- Preserve `linux/amd64` and `linux/arm64`.
- Preserve Node.js, Python, PHP, .NET, Java, and Go workflows expected by the current
  image.
- Preserve editable local package imports.
- Run CCXT's current official build and relevant Docker checks.
- Do not pin or upgrade unrelated dependencies merely to reduce layer count.
- Do not use an optimization that depends on undocumented Docker behavior.
- Do not claim faster builds based on one warm-cache run.

## Decision rule

Propose an upstream change only when it:

- has repeatable before-and-after measurements;
- improves at least one target metric materially;
- does not materially regress another target metric;
- remains readable and compatible with the documented workflow;
- can be reviewed as a focused change.

If the only meaningful reduction requires removing supported toolchains, document
the result and stop. A no-go conclusion is a valid outcome.

## References

- [CCXT Dockerfile](https://github.com/ccxt/ccxt/blob/master/Dockerfile)
- [CCXT contributing guide](https://github.com/ccxt/ccxt/blob/master/CONTRIBUTING.md)
- [Docker build cache documentation](https://docs.docker.com/build/cache/)
- [Dockerfile best practices](https://docs.docker.com/build/building/best-practices/)
