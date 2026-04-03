#!/usr/bin/env bash
set -euo pipefail

workspace="${1:-.}"
workspace="$(cd "$workspace" && pwd)"

runtime_dir="$workspace/.litehive/runtime"
log_dir="$workspace/.litehive/logs/run-all"
pid_file="$runtime_dir/run-all.pid"
launcher_log="$log_dir/daemon.log"

mkdir -p "$runtime_dir" "$log_dir"

if [[ -f "$pid_file" ]]; then
  existing_pid="$(cat "$pid_file" 2>/dev/null || true)"
  if [[ -n "${existing_pid:-}" ]] && kill -0 "$existing_pid" 2>/dev/null; then
    echo "run-all already running: pid=$existing_pid"
    exit 0
  fi
  rm -f "$pid_file"
fi

child_script=$(cat <<'EOF'
set -euo pipefail
workspace="$1"
pid_file="$2"
launcher_log="$3"
cd "$workspace"
echo "$$" >"$pid_file"
trap 'rm -f "$pid_file"' EXIT
exec bash scripts/run-all.sh . >>"$launcher_log" 2>&1
EOF
)

setsid bash -lc "$child_script" bash "$workspace" "$pid_file" "$launcher_log" >/dev/null 2>&1 &
sleep 1

if [[ -f "$pid_file" ]]; then
  pid="$(cat "$pid_file" 2>/dev/null || true)"
  if [[ -n "${pid:-}" ]] && kill -0 "$pid" 2>/dev/null; then
    echo "run-all started: pid=$pid"
    exit 0
  fi
fi

echo "run-all failed to start"
exit 1
