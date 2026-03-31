#!/usr/bin/env bash
set -euo pipefail

workspace="${1:-.}"
workspace="$(cd "$workspace" && pwd)"

echo "== litehive status =="
uv run litehive status --workspace "$workspace"

log_base="$workspace/.litehive/logs/run-all"
if [[ ! -d "$log_base" ]]; then
  echo
  echo "No run-all logs found."
  exit 0
fi

latest_run="$(find "$log_base" -mindepth 1 -maxdepth 1 -type d | sort | tail -n 1)"
if [[ -z "$latest_run" ]]; then
  echo
  echo "No run-all logs found."
  exit 0
fi

echo
echo "== latest run-all logs =="
echo "directory: $latest_run"

latest_post="$(find "$latest_run" -maxdepth 1 -name '*-post-status.log' | sort | tail -n 1)"
latest_run_log="$(find "$latest_run" -maxdepth 1 -name '*-run.log' | sort | tail -n 1)"

if [[ -n "$latest_post" ]]; then
  echo
  echo "-- latest post-status --"
  cat "$latest_post"
fi

if [[ -n "$latest_run_log" ]]; then
  echo
  echo "-- latest run output tail --"
  tail -n 40 "$latest_run_log"
fi
