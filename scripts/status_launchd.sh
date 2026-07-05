#!/usr/bin/env bash
set -euo pipefail

LABEL="dev.herdrelay"

launchctl print "gui/$UID/${LABEL}"
