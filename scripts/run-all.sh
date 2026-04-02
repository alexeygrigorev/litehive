#!/usr/bin/env bash
set -euo pipefail

workspace="${1:-.}"
workspace="$(cd "$workspace" && pwd)"

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
log_root="$workspace/.litehive/logs/run-all/$timestamp"
mkdir -p "$log_root"

iteration=0

read_state_snapshot() {
  local file="$1"
  WORKSPACE="$workspace" uv run python - <<'PY' >"$file"
from pathlib import Path
import os
import yaml

workspace = Path(os.environ["WORKSPACE"])
state_path = workspace / ".litehive" / "state.yaml"
state = yaml.safe_load(state_path.read_text()) if state_path.exists() else {}

active_task_id = state.get("active_task_id")
queue = state.get("queue", []) or []
stop_reason = state.get("pool_stop_reason")

print(f"active_task_id: {active_task_id if active_task_id is not None else 'None'}")
print(f"queued_tasks: {len(queue)}")
print(f"pool_stop_reason: {stop_reason if stop_reason is not None else 'None'}")
if queue:
    print(f"queue_head: {queue[0]}")
PY
}

snapshot_field() {
  local field="$1"
  local file="$2"
  sed -n "s/^${field}: //p" "$file" | head -n 1
}

is_explicit_pool_stop_reason() {
  case "${1:-}" in
    dirty_git_state|max_tasks_reached|failure_detected|execution_limit_reached|execution_limit_fallbacks_exhausted|quota_threshold_reached|budget_threshold_reached|stop_condition_reached)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

echo "workspace: $workspace"
echo "logs: $log_root"

while true; do
  iteration=$((iteration + 1))
  prefix="$(printf '%04d' "$iteration")"

  pre_status_file="$log_root/${prefix}-pre-status.log"
  run_file="$log_root/${prefix}-run.log"
  post_status_file="$log_root/${prefix}-post-status.log"

  echo
  echo "== iteration $iteration =="
  read_state_snapshot "$pre_status_file"
  cat "$pre_status_file"

  active_task_id="$(snapshot_field active_task_id "$pre_status_file")"
  queued_tasks="$(snapshot_field queued_tasks "$pre_status_file")"
  stop_reason_before="$(snapshot_field pool_stop_reason "$pre_status_file")"

  if [[ "${active_task_id:-None}" == "None" && "${queued_tasks:-0}" == "0" ]]; then
    echo "No active or queued tasks remain. Stopping."
    break
  fi

  if [[ "${stop_reason_before:-None}" == "blocked_tasks_remaining" ]]; then
    echo "Blocked tasks remain and nothing is runnable. Stopping."
    break
  fi

  if is_explicit_pool_stop_reason "${stop_reason_before:-}"; then
    echo "Pool already stopped: $stop_reason_before"
    break
  fi

  if ! uv run litehive run --workspace "$workspace" | tee "$run_file"; then
    echo "litehive run failed; see $run_file"
    exit 1
  fi

  read_state_snapshot "$post_status_file"
  cat "$post_status_file"

  stop_reason="$(snapshot_field pool_stop_reason "$post_status_file")"
  active_after="$(snapshot_field active_task_id "$post_status_file")"
  queued_after="$(snapshot_field queued_tasks "$post_status_file")"

  if [[ "${active_after:-None}" == "None" && "${queued_after:-0}" == "0" ]]; then
    echo "No active or queued tasks remain. Stopping."
    break
  fi

  if [[ "${stop_reason:-None}" == "blocked_tasks_remaining" ]]; then
    echo "Blocked tasks remain and nothing is runnable. Stopping."
    break
  fi

  if is_explicit_pool_stop_reason "${stop_reason:-}"; then
    echo "Pool stopped: $stop_reason"
    break
  fi

  if [[ -n "${stop_reason:-}" && "${stop_reason}" != "None" && "${stop_reason}" != "queue_exhausted" ]]; then
    echo "Stopping after litehive reported stop_reason: $stop_reason"
    break
  fi
done
