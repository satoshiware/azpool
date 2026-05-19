# ADR: azpool/templar as canonical Template Provider source

## Status

Accepted

## Context

The AZCOIN SV2 Template Provider was originally developed in a standalone Git repository (`satoshiware/azcoin-template-provider` on GitHub). That repository is archived, removed, or otherwise unavailable and must not be treated as a reliable build or install dependency.

The preserved implementation now lives in this repository at `azpool/templar`.

## Decision

1. **`azpool/templar` is the sole canonical source** for the AZCOIN Template Provider in this project.
2. **No future builds, installs, or documentation** in the allowed deploy/runbook paths may depend on cloning, fetching, or referencing the old standalone remote.
3. **Runtime configuration and secrets remain outside Git** under `/etc/azcoin-super/templar`.
4. **The installed production binary path remains** `/opt/azcoin-super/bin/azcoin-template-provider`.
5. **systemd runs the installed binary**, not a working-tree or checkout path.
6. **Rollback of the installed binary** uses backups under `/opt/azcoin-super/releases/template-provider`.

## Consequences

- Build support nodes with `deploy/scripts/build-support-node.sh` (runs `cargo build --release` in `templar/`).
- Install with `deploy/scripts/install-support-node.sh`, which backs up any existing installed binary before overwrite.
- Operators with a local clone of the old standalone repository should archive it with `git bundle` per `docs/runbooks/template-provider-repo-migration.md` and migrate workflows to `azpool/templar`.

## Non-goals

- Changing Template Provider runtime/protocol logic, wallet/RPC behavior, or payout behavior.
- Committing live `/etc/azcoin-super/templar` configuration or secrets into Git.
