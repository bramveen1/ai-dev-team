#!/usr/bin/env bash
# Push gitignored config files from dev to prod over SSH, preserving
# prod's memory and secrets. Run from the repo root on the dev machine.
#
# Files copied:
#   config/shared/WORLDVIEW.md       — universal behavior rules
#   config/agents/*/role.md          — per-agent job description
#   config/agents/*/personality.md   — per-agent voice
#
# Files explicitly NOT touched on prod:
#   config/shared/MEMORY.md          — prod has its own org memory
#   config/agents/*/memory/          — prod has its own per-agent memory
#   data/                            — secret store (run `@router grant` on prod instead)
#   .env                             — prod has its own Slack tokens
#
# Tracked files (agent.yaml, packs/, code, docs) travel via `git pull` —
# this script only handles the gitignored content.
#
# Usage:
#   scripts/sync-config-to-prod.sh [--dry-run] <user@host:/path/to/ai-dev-team>
#
# Examples:
#   scripts/sync-config-to-prod.sh --dry-run bram@prod.example.com:/srv/ai-dev-team
#   scripts/sync-config-to-prod.sh bram@prod.example.com:/srv/ai-dev-team

set -euo pipefail

DRY_RUN=""
if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN="--dry-run"
  shift
fi

if [[ $# -lt 1 ]]; then
  sed -n '2,/^$/p' "$0" | sed 's/^# \?//' >&2
  exit 1
fi

DEST="$1"

if [[ ! -d config/agents ]]; then
  echo "error: run this from the repo root (no config/agents/ here)." >&2
  exit 1
fi

# Build the list of files to push.
LIST=$(mktemp)
trap 'rm -f $LIST' EXIT

if [[ -f config/shared/WORLDVIEW.md ]]; then
  echo "config/shared/WORLDVIEW.md" >> "$LIST"
fi

for agent_dir in config/agents/*/; do
  [[ -d "$agent_dir" ]] || continue
  for file in role.md personality.md; do
    if [[ -f "$agent_dir$file" ]]; then
      echo "$agent_dir$file" >> "$LIST"
    fi
  done
done

if [[ ! -s "$LIST" ]]; then
  echo "error: nothing to sync from $(pwd)/config/." >&2
  exit 1
fi

echo "Pushing to: $DEST"
echo
echo "Files to copy:"
sed 's/^/  /' "$LIST"
echo
echo "Files explicitly NOT touched on prod:"
echo "  config/shared/MEMORY.md"
echo "  config/agents/*/memory/"
echo "  data/"
echo "  .env"
echo

if [[ -z "$DRY_RUN" ]]; then
  read -r -p "Proceed? [y/N] " confirm
  [[ "$confirm" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 0; }
fi

# --files-from implies --relative, so paths are preserved as-is on the
# remote. We avoid --delete on purpose; this is purely additive.
rsync -av $DRY_RUN \
  --files-from="$LIST" \
  ./ "$DEST/"

if [[ -n "$DRY_RUN" ]]; then
  echo
  echo "(Dry run only. Re-run without --dry-run to actually push.)"
fi
