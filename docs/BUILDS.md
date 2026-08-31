# Builds & supported architectures

Images are built and published by GitHub Actions
([`.github/workflows/docker-publish.yml`](../.github/workflows/docker-publish.yml)).
`ace-laptimes/build-and-push.sh` still works for building locally.

## Triggers

| Event | Result |
|---|---|
| Push to `main` | Rebuilds only the changed services → `:latest`, `:main-<sha>` |
| Tag `v*` | Rebuilds **all three** services → `:X.Y.Z`, `:X.Y`, `:vX.Y.Z`, `:vX.Y`, `:latest` |
| Pull request | Builds only — never pushed |
| Manual (`workflow_dispatch`) | Build all, or a single service |

A version tag always rebuilds all three images so that a given version is a
coherent set, even if the tagged commit only touched one service.

Both bare and `v`-prefixed version tags are published (`1.2.0` and `v1.2.0`
point at the same image). `docker/metadata-action` strips the leading `v` from
semver patterns by default; the `v`-prefixed aliases are kept because releases
before v1.2.0 were published that way by `build-and-push.sh`.

## Required secrets

| Secret | Value |
|---|---|
| `DOCKERHUB_USERNAME` | Docker Hub account name |
| `DOCKERHUB_TOKEN` | Docker Hub **access token** with Read & Write scope |

Create the token at Docker Hub → Account Settings → Personal access tokens.
Use a token, not your account password, and don't grant Delete.

## Supported architectures

Every image is published as a multi-arch manifest for:

| Platform | Typical hardware |
|---|---|
| `linux/amd64` | Any x86-64 server, NAS, mini PC |
| `linux/arm64` | Raspberry Pi 3/4/5 on 64-bit Raspberry Pi OS, Apple Silicon, ARM VPS |

Docker picks the right one automatically — `docker compose pull` needs no
per-architecture configuration.

### 32-bit ARM (ARMv7 / armhf) is not built

The `node` base image used by the frontend stopped publishing a
`linux/arm/v7` manifest, so a three-platform build fails at the
`load metadata` step before anything is compiled. Rather than pin the
frontend to an old Node major to keep one legacy leg alive, 32-bit ARM was
dropped.

Raspberry Pi 2 has no 64-bit mode and is therefore no longer supported. A Pi
3, 4 or 5 running a 32-bit OS needs to be reinstalled with 64-bit Raspberry
Pi OS to use these images — the hardware itself is fine.

### ARMv6 (Pi Zero / Pi Zero W / Pi 1) is not supported

This is a **known limitation**, not an oversight. Two hard blockers:

1. **No base image.** The official `python:3.12-slim` image publishes
   `linux/arm/v5`, `arm/v7` and `arm64/v8` — but no `arm/v6`. Only the
   Alpine variant covers ARMv6.
2. **No prebuilt `bcrypt`.** On Alpine (musl) there is no ARMv6 wheel for
   `bcrypt`, which is a Rust extension since 4.0. It would have to be
   compiled from source under QEMU emulation on every build — slow enough to
   be impractical in CI, and fragile.

Even if both were solved, a Pi Zero's single 1 GHz ARMv6 core and 512 MB of
RAM would struggle to run Docker plus three containers.

**If you want to run this on ARMv6 hardware**, the realistic path is to skip
Docker and run the backend directly with a system Python and a
`bcrypt`-free password hash (`hashlib.scrypt` from the standard library
would need to replace `bcrypt` in `app.py`, with a migration for existing
password hashes). That is not currently implemented.

**Pi Zero 2 W is fine** — it is ARM Cortex-A53, so it runs the `arm64` image
on a 64-bit OS.

## Build cache

Each service uses its own GitHub Actions cache scope (`cache-from/to:
type=gha,scope=<service>`), so the three images don't evict each other from
the 10 GB per-repository cache budget.

## Tray app executable

[`.github/workflows/tray-release.yml`](../.github/workflows/tray-release.yml)
packages the Windows tray app with PyInstaller. On a `v*` tag it attaches
`ACELapTracker.exe` to the GitHub release; on pull requests it uploads the
exe as a workflow artifact. Users no longer need Python installed —
`start.bat` remains for running from source.
