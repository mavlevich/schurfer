# CCXT-007: Research .NET installer reproducibility and integrity

> Status: backlog; low-priority research
> Depends on: CCXT-005
> Produces: an upstream policy question, one focused hardening proposal, or a
> documented no-go

## Goal

Decide whether CCXT's development image should pin the .NET SDK patch version or
verify the installer more strongly than downloading the official script over HTTPS.

This is not a correctness regression in CCXT-005. The previous apt command also
tracked the latest available .NET 9 patch, and the restored install-script flow is
the official cross-architecture path for CI and development environments.

## Current behavior

The merged Dockerfile:

```dockerfile
RUN curl -fsSL https://dot.net/v1/dotnet-install.sh -o /tmp/dotnet-install.sh \
    && bash /tmp/dotnet-install.sh --channel 9.0 \
        --install-dir "${DOTNET_ROOT}" --no-path \
    && rm /tmp/dotnet-install.sh \
    && dotnet --list-sdks
```

This provides the current .NET 9 SDK for the detected architecture. It does not make
the selected patch version visible in source control, and the installer script itself
is not pinned to a content digest.

## Questions to answer before proposing code

- Does CCXT intentionally track the latest supported .NET 9 patch for development?
- Would pinning a patch create unwanted routine maintenance or delayed security
  updates?
- Does Microsoft publish a stable checksum or signed artifact for the installer
  script itself?
- Does the install script verify the downloaded SDK archive, and to what threat
  model?
- Would a pinned official .NET container stage provide stronger provenance without
  making the multi-language image larger or harder to build on ARM64?
- What pattern do current CCXT CI workflows and other language installers use?

## Candidate approaches

Evaluate independently:

1. Keep `--channel 9.0` and document the floating-patch policy.
2. Pin an exact SDK version and add a deliberate update process.
3. Pin or verify the installer script if Microsoft provides a maintained integrity
   mechanism.
4. Copy the SDK from an official multi-architecture .NET SDK image in a build stage.

Do not hand-maintain an unofficial checksum list or replace an accepted official flow
with a more fragile custom downloader.

## Acceptance criteria for an upstream proposal

- Maintainer intent for patch updates is known.
- ARM64 and AMD64 remain supported.
- The proposal materially improves reproducibility or integrity.
- Security updates are not silently frozen.
- The full CCXT build and C# build pass.
- The change is separate from image-size and cache optimization.

## No-go rule

If CCXT intentionally follows the latest .NET 9 patch and Microsoft exposes no
stable installer-verification mechanism that improves the current flow, document the
decision and do not submit a cosmetic PR.

## References

- [Merged Apple Silicon fix](https://github.com/ccxt/ccxt/pull/29305)
- [Microsoft dotnet-install script documentation](https://learn.microsoft.com/en-us/dotnet/core/tools/dotnet-install-script)
- [Official .NET container images](https://mcr.microsoft.com/product/dotnet/sdk/about)
- [CCXT contributing guide](https://github.com/ccxt/ccxt/blob/master/CONTRIBUTING.md)
