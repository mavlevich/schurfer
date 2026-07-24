# CCXT-005: Restore the development image on Apple Silicon

> Status: issue confirmed; focused pull request ready
> Depends on: none
> Tracks: [ccxt/ccxt#29304](https://github.com/ccxt/ccxt/issues/29304)
> Produces: one root `Dockerfile` pull request

## Goal

Make CCXT's documented development Docker image build on both `linux/arm64` and
`linux/amd64` without changing exchange behavior or generated packages.

## What exposed the problem

Running the documented development-container flow on macOS with Apple Silicon
failed while building the root image:

```text
E: Unable to locate package dotnet-sdk-9.0
```

The Dockerfile installed Microsoft's Ubuntu package feed and then requested
`dotnet-sdk-9.0`. Microsoft documents that this feed supports x64, not Arm64. The
official `dotnet-install.sh` installer detects the target architecture and supports
both.

Removing that blocker exposed a second stale Dockerfile step. CCXT moved Python
package metadata from `python/setup.py` to the repository-root `pyproject.toml`, but
the image still ran the editable install from `python/`. The current build backend
also requires the setuptools version pinned by the root project.

Both causes were reproduced against CCXT 4.5.68 and independently confirmed in the
upstream issue.

## Focused implementation

- Install .NET 9 with Microsoft's official `dotnet-install.sh`.
- Preserve `/usr/share/dotnet` as `DOTNET_ROOT`.
- Remove the downloaded script after installation.
- Verify the SDK during the image build with `dotnet --list-sdks`.
- Upgrade pip and install the setuptools version required by `pyproject.toml`.
- Install the Python package from the repository root.
- Do not include Docker layer, cache, or image-size optimization in this fix.

## Validation completed

- Native `docker compose build ccxt` on Apple Silicon.
- Resulting container reports `aarch64`.
- .NET SDK 9.0.316 is available on `linux/arm64`.
- The same installer path provides .NET SDK 9.0.316 on `linux/amd64`.
- Python, Node.js, and PHP load CCXT 4.5.68.
- `npm run buildCSFast` succeeds with no errors.
- The full `npm run build` succeeds inside the resulting ARM64 image.
- `git diff --check` succeeds.
- The branch changes only the root `Dockerfile`.

## Acceptance criteria

- The development image builds natively on `linux/arm64`.
- The .NET SDK remains available on `linux/amd64`.
- The editable Python install uses current project metadata.
- The full CCXT build completes inside the image.
- No generated artifacts or unrelated Docker cleanup are committed.

## References

- [Upstream issue #29304](https://github.com/ccxt/ccxt/issues/29304)
- [CCXT contributing guide](https://github.com/ccxt/ccxt/blob/master/CONTRIBUTING.md)
- [Microsoft Ubuntu .NET installation documentation](https://learn.microsoft.com/en-us/dotnet/core/install/linux-ubuntu-install)
- [Microsoft dotnet-install script documentation](https://learn.microsoft.com/en-us/dotnet/core/tools/dotnet-install-script)
- [CCXT PR #28055](https://github.com/ccxt/ccxt/pull/28055)
- [CCXT PR #29093](https://github.com/ccxt/ccxt/pull/29093)
