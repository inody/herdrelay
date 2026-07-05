#!/usr/bin/env bash
set -euo pipefail

LABEL="dev.herdrelay"
PLIST_PATH="$HOME/Library/LaunchAgents/${LABEL}.plist"

launchctl bootout "gui/$UID" "$PLIST_PATH" >/dev/null 2>&1 || true
rm -f "$PLIST_PATH"

echo "Uninstalled ${LABEL}"
