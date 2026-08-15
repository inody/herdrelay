#!/usr/bin/env bash
set -euo pipefail

LABEL="dev.herdrelay"
ROTATE_LABEL="dev.herdrelay.log-rotate"
LEGACY_LABEL="dev.herdr.discord-bridge"
PLIST_PATH="$HOME/Library/LaunchAgents/${LABEL}.plist"
ROTATE_PLIST_PATH="$HOME/Library/LaunchAgents/${ROTATE_LABEL}.plist"
LEGACY_PLIST_PATH="$HOME/Library/LaunchAgents/${LEGACY_LABEL}.plist"

launchctl bootout "gui/$UID" "$PLIST_PATH" >/dev/null 2>&1 || true
launchctl bootout "gui/$UID" "$ROTATE_PLIST_PATH" >/dev/null 2>&1 || true
launchctl bootout "gui/$UID" "$LEGACY_PLIST_PATH" >/dev/null 2>&1 || true
rm -f "$PLIST_PATH" "$ROTATE_PLIST_PATH" "$LEGACY_PLIST_PATH"

echo "Uninstalled ${LABEL}"
