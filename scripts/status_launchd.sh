#!/usr/bin/env bash
set -euo pipefail

LABEL="dev.herdr.discord-bridge"

launchctl print "gui/$UID/${LABEL}"
