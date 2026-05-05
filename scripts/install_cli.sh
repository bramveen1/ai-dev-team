#!/usr/bin/env bash
# Install a CLI binary into the shared `agent-tools` Docker volume so every
# agent container has it on PATH at /opt/tools.
#
# Usage:
#   scripts/install_cli.sh <name> <download-url> [--strip-components N]
#
# Examples:
#   # gh CLI (raw binary inside a tarball)
#   scripts/install_cli.sh gh \
#     https://github.com/cli/cli/releases/download/v2.62.0/gh_2.62.0_linux_amd64.tar.gz \
#     --strip-components 2
#
#   # Single binary download
#   scripts/install_cli.sh fly https://fly.io/install.sh
#
# Behaviour:
#   - Spins up a one-shot alpine container with the shared volume mounted at
#     /opt/tools.
#   - Downloads the URL. If it's a tarball, extracts to /opt/tools (with the
#     optional --strip-components passed to tar). If it's a plain binary,
#     writes it to /opt/tools/<name> and chmods +x.
#   - The volume persists across restarts; every agent container sees the
#     binary on PATH next time it starts.
#
# This is a no-op the second time you run it — set up once per CLI per host.
set -euo pipefail

if [ $# -lt 2 ]; then
  echo "usage: $0 <name> <download-url> [--strip-components N]" >&2
  exit 1
fi

NAME="$1"
URL="$2"
shift 2

STRIP_ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --strip-components)
      STRIP_ARGS=(--strip-components "$2")
      shift 2
      ;;
    *)
      echo "unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

VOLUME="agent-tools"

docker volume inspect "$VOLUME" >/dev/null 2>&1 || docker volume create "$VOLUME" >/dev/null

docker run --rm \
  -v "$VOLUME":/opt/tools \
  alpine:3.20 \
  sh -c "
    set -euo pipefail
    apk add --no-cache curl tar gzip >/dev/null
    cd /tmp
    curl -sSL '$URL' -o download
    case '$URL' in
      *.tar.gz|*.tgz)
        tar -xzf download -C /opt/tools ${STRIP_ARGS[*]:-}
        ;;
      *)
        install -m 0755 download /opt/tools/$NAME
        ;;
    esac
    ls -la /opt/tools/$NAME 2>/dev/null || ls /opt/tools
  "

echo "Installed $NAME into volume $VOLUME (mounted at /opt/tools in agent containers)"
