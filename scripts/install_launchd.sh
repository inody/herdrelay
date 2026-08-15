#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

LABEL="dev.herdrelay"
ROTATE_LABEL="dev.herdrelay.log-rotate"
LEGACY_LABEL="dev.herdr.discord-bridge"
ROOT_DIR="$(pwd)"
PLIST_PATH="$HOME/Library/LaunchAgents/${LABEL}.plist"
ROTATE_PLIST_PATH="$HOME/Library/LaunchAgents/${ROTATE_LABEL}.plist"
LEGACY_PLIST_PATH="$HOME/Library/LaunchAgents/${LEGACY_LABEL}.plist"
LOG_DIR="$ROOT_DIR/logs"
RUN_SCRIPT="$ROOT_DIR/scripts/run_bot.sh"
ROTATE_SCRIPT="$ROOT_DIR/scripts/rotate_logs.py"

mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR"

cat > "$PLIST_PATH" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${LABEL}</string>

  <key>ProgramArguments</key>
  <array>
    <string>${RUN_SCRIPT}</string>
    <string>--log-level</string>
    <string>INFO</string>
  </array>

  <key>WorkingDirectory</key>
  <string>${ROOT_DIR}</string>

  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>${HOME}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>

  <key>RunAtLoad</key>
  <true/>

  <key>KeepAlive</key>
  <true/>

  <key>StandardOutPath</key>
  <string>${LOG_DIR}/launchd.out.log</string>

  <key>StandardErrorPath</key>
  <string>${LOG_DIR}/launchd.err.log</string>
</dict>
</plist>
PLIST

cat > "$ROTATE_PLIST_PATH" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${ROTATE_LABEL}</string>

  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>${ROTATE_SCRIPT}</string>
    <string>${LOG_DIR}/launchd.out.log</string>
    <string>${LOG_DIR}/launchd.err.log</string>
  </array>

  <key>StartInterval</key>
  <integer>3600</integer>

  <key>RunAtLoad</key>
  <true/>

  <key>StandardOutPath</key>
  <string>/dev/null</string>

  <key>StandardErrorPath</key>
  <string>/dev/null</string>
</dict>
</plist>
PLIST

chmod 644 "$PLIST_PATH" "$ROTATE_PLIST_PATH"
chmod +x "$ROTATE_SCRIPT"

# Migrate installations created before the project was renamed to HerdRelay.
launchctl bootout "gui/$UID" "$LEGACY_PLIST_PATH" >/dev/null 2>&1 || true
rm -f "$LEGACY_PLIST_PATH"
launchctl bootout "gui/$UID" "$PLIST_PATH" >/dev/null 2>&1 || true
launchctl bootout "gui/$UID" "$ROTATE_PLIST_PATH" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$UID" "$ROTATE_PLIST_PATH"
launchctl bootstrap "gui/$UID" "$PLIST_PATH"
launchctl kickstart -k "gui/$UID/${LABEL}"

echo "Installed and started ${LABEL}"
echo "Plist: ${PLIST_PATH}"
echo "Logs:  ${LOG_DIR}/launchd.out.log"
echo "       ${LOG_DIR}/launchd.err.log"
