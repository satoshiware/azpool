# Template Provider — migrate from standalone repository

The standalone `satoshiware/azcoin-template-provider` GitHub repository is archived/unavailable. All new development and builds use **`azpool/templar`** in this repository.

Do **not** add the old repository as a remote or attempt to fetch it for builds.

## Archive a local standalone clone with git bundle

If you still have a local checkout of the old standalone repository, preserve it offline before decommissioning the working copy.

```bash
OLD_REPO=~/repos/azcoin-template-provider
ARCHIVE_DIR=~/archives
mkdir -p "${ARCHIVE_DIR}"

cd "${OLD_REPO}"
git bundle create "${ARCHIVE_DIR}/azcoin-template-provider-$(date -u +%Y%m%d).bundle" --all
```

Verify the bundle (optional):

```bash
git clone "${ARCHIVE_DIR}/azcoin-template-provider-YYYYMMDD.bundle" /tmp/azcoin-template-provider-bundle-verify
cd /tmp/azcoin-template-provider-bundle-verify
git log --oneline -5
rm -rf /tmp/azcoin-template-provider-bundle-verify
```

Keep the `.bundle` file in your operator archive store. It is a read-only snapshot; builds and installs must use `azpool/templar`.

## Switch build and install workflows

1. Clone or update this `azpool` repository.
2. Build: `./deploy/scripts/build-support-node.sh`
3. Install: `sudo ./deploy/scripts/install-support-node.sh`
4. Restart: `sudo systemctl restart azcoin-template-provider.service`

Runtime config remains at `/etc/azcoin-super/templar/azcoin-template-provider.toml` (outside Git).

Installed binary path remains `/opt/azcoin-super/bin/azcoin-template-provider`.

Rollback binaries live under `/opt/azcoin-super/releases/template-provider/`.

See also: [template-provider-build-install.md](template-provider-build-install.md) and [ADR-azpool-templar-canonical-source.md](../adr/ADR-azpool-templar-canonical-source.md).
