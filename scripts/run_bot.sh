#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ ! -f .env ]]; then
  echo "Missing .env. Copy .env.example and set DISCORD_TOKEN." >&2
  exit 1
fi

if [[ ! -f config.yaml ]]; then
  echo "Missing config.yaml. Copy config.example.yaml and fill Discord IDs." >&2
  exit 1
fi

exec uv run herdrelay --config config.yaml "$@"
