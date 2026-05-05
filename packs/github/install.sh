#!/usr/bin/env bash
# Install the gh CLI into the shared agent-tools Docker volume.
#
# Run this once on the host before granting the github pack to any agent.
# Idempotent — safe to re-run; later runs upgrade or reinstall.
#
# Usage:
#   packs/github/install.sh                # latest pinned version
#   GH_VERSION=2.62.0 packs/github/install.sh
#
# After this, every agent container sees `gh` on PATH at /opt/tools/gh.

set -euo pipefail

VERSION="${GH_VERSION:-2.62.0}"
ARCH="${GH_ARCH:-linux_amd64}"
URL="https://github.com/cli/cli/releases/download/v${VERSION}/gh_${VERSION}_${ARCH}.tar.gz"
VOLUME="agent-tools"

docker volume inspect "$VOLUME" >/dev/null 2>&1 || docker volume create "$VOLUME" >/dev/null

docker run --rm \
  -v "$VOLUME":/opt/tools \
  alpine:3.20 sh -c "
    set -eu
    apk add --no-cache curl tar gzip >/dev/null
    cd /tmp
    curl -sSL '$URL' -o gh.tar.gz
    tar -xzf gh.tar.gz
    install -m 0755 gh_${VERSION}_${ARCH}/bin/gh /opt/tools/gh
    /opt/tools/gh --version
  "

echo "Installed gh ${VERSION} into volume ${VOLUME} (mounted at /opt/tools in agent containers)"
