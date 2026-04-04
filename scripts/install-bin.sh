#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
target_dir="${HOME}/bin"
uv_bin="${HOME}/.local/bin/uv"

mkdir -p "$target_dir"

if [[ ! -x "$uv_bin" ]]; then
  echo "uv not found at $uv_bin" >&2
  exit 1
fi

write_launcher() {
  local path="$1"
  local body="$2"
  cat >"$path" <<EOF
#!/usr/bin/env bash
set -euo pipefail

${body}
EOF
  chmod +x "$path"
  echo "Installed launcher: $path"
}

write_launcher \
  "${target_dir}/litehive" \
  "exec \"$uv_bin\" run --project \"$repo_root\" litehive \"\$@\""

case ":${PATH}:" in
  *":${HOME}/bin:"*)
    echo "~/bin is on PATH"
    ;;
  *)
    echo "~/bin is not on PATH" >&2
    echo "Add this to your shell config:" >&2
    echo 'export PATH="$HOME/bin:$PATH"' >&2
    exit 2
    ;;
esac

echo "Resolved launcher: $(command -v litehive || true)"
