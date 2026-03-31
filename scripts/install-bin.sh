#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
target_dir="${HOME}/bin"
target_path="${target_dir}/litehive"
uv_bin="${HOME}/.local/bin/uv"

mkdir -p "$target_dir"

if [[ ! -x "$uv_bin" ]]; then
  echo "uv not found at $uv_bin" >&2
  exit 1
fi

cat >"$target_path" <<EOF
#!/usr/bin/env bash
set -euo pipefail

exec "$uv_bin" run --project "$repo_root" litehive "\$@"
EOF

chmod +x "$target_path"

echo "Installed launcher: $target_path"

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
