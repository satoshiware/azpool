# Template Provider — build and install (support node)

Canonical source: **`templar/`** in this repository.

The archived standalone `azcoin-template-provider` GitHub repository is **not** required.

## Paths

| Item | Path |
|------|------|
| Source | `templar/` |
| Build artifact | `templar/target/release/azcoin-template-provider` |
| Installed binary | `/opt/azcoin-super/bin/azcoin-template-provider` |
| Runtime config | `/etc/azcoin-super/templar/azcoin-template-provider.toml` |
| Runtime log file | `/var/log/templar/templar.log` (from `log_file` in runtime config) |
| Log directory | `/var/log/templar` (created by install script, writable under `ProtectSystem=strict`) |
| Binary rollback backups | `/opt/azcoin-super/releases/template-provider/` |
| systemd unit | `azcoin-template-provider.service` |

## Prerequisites

- Rust toolchain (`cargo`)
- Build dependencies for `templar/` (including libzmq where required)
- `azcoin-templar` system user and runtime config already provisioned on the target host
- Do **not** add or fetch the old standalone repository remote

## Build

From the repository root:

```bash
./deploy/scripts/build-support-node.sh
```

The script builds from `templar/` and reports the release artifact path. If the executable name differs from `azcoin-template-provider`, the script fails and lists what it found.

## Install

On the support node (requires root):

```bash
sudo ./deploy/scripts/install-support-node.sh
```

This script:

1. Installs the templar-built binary to `/opt/azcoin-super/bin/azcoin-template-provider`
2. Backs up any existing installed binary to `/opt/azcoin-super/releases/template-provider/azcoin-template-provider.<timestamp>`
3. Creates `/var/log/templar` owned by `azcoin-templar:azcoin-templar` (mode `0750`) for the configured `log_file` path (`/var/log/templar/templar.log`)
4. Installs `deploy/systemd/azcoin-template-provider.service` to `/etc/systemd/system/` (includes `/var/log/templar` in `ReadWritePaths`)
5. Runs `systemctl daemon-reload`

It does **not** create or modify `/etc/azcoin-super/templar` config files.

## Restart

```bash
sudo systemctl daemon-reload
sudo systemctl restart azcoin-template-provider.service
sudo systemctl status azcoin-template-provider.service --no-pager
```

Health check (does not print config contents):

```bash
sudo -u azcoin-templar /opt/azcoin-super/bin/azcoin-template-provider \
  --config /etc/azcoin-super/templar/azcoin-template-provider.toml \
  --health-check
```

## Rollback

```bash
sudo cp -a /opt/azcoin-super/releases/template-provider/azcoin-template-provider.<TIMESTAMP> \
  /opt/azcoin-super/bin/azcoin-template-provider
sudo systemctl restart azcoin-template-provider.service
```

## Smoke test

```bash
./deploy/scripts/smoke-test-support-node.sh
```
