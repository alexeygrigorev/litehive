#!/usr/bin/env bash
set -euo pipefail

workspace="${1:-.}"
workspace="$(cd "$workspace" && pwd)"

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
log_root="$workspace/.litehive/logs/run-all/$timestamp"
mkdir -p "$log_root"

iteration=0

echo "workspace: $workspace"
echo "logs: $log_root"

while true; do
  iteration=$((iteration + 1))
  prefix="$(printf '%04d' "$iteration")"

  pre_status_file="$log_root/${prefix}-pre-status.log"
  run_file="$log_root/${prefix}-run.log"
  post_status_file="$log_root/${prefix}-post-status.log"

  uv run litehive status --workspace "$workspace" | tee "$pre_status_file"

  active_task_id="$(sed -n 's/^active_task_id: //p' "$pre_status_file" | head -n 1)"
  queued_tasks="$(sed -n 's/^queued_tasks: //p' "$pre_status_file" | head -n 1)"

  if [[ "${active_task_id:-None}" == "None" && "${queued_tasks:-0}" == "0" ]]; then
    echo "No active or queued tasks remain. Stopping."
    break
  fi

  if ! uv run litehive run --workspace "$workspace" | tee "$run_file"; then
    echo "litehive run failed; see $run_file"
    exit 1
  fi

  uv run litehive status --workspace "$workspace" | tee "$post_status_file"

  stop_reason="$(sed -n 's/^pool_stop_reason: //p' "$post_status_file" | head -n 1)"
  active_after="$(sed -n 's/^active_task_id: //p' "$post_status_file" | head -n 1)"
  queued_after="$(sed -n 's/^queued_tasks: //p' "$post_status_file" | head -n 1)"

  if [[ -n "${stop_reason:-}" && "${stop_reason}" != "None" ]]; then
    echo "Pool stopped: $stop_reason"
    break
  fi

  if [[ "${active_after:-None}" == "None" && "${queued_after:-0}" == "0" ]]; then
    echo "No active or queued tasks remain. Stopping."
    break
  fi
done
