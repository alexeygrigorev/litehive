"""Subagent artifact writing and pruning helpers."""

from pathlib import Path

from litehive.state.persist import atomic_write_gzip_text, write_atomic_files

_COMPRESS_STREAM_ARTIFACT_MIN_BYTES = 4096
_COMPRESS_TEXT_ARTIFACT_MIN_BYTES = 4096


def write_stream_artifact(base: Path, name: str, content: str, *, compress: bool) -> None:
    plain_path = base / f"{name}.txt"
    compressed_path = base / f"{name}.txt.gz"
    if compress and not content:
        if compressed_path.exists():
            compressed_path.unlink()
        write_text_if_changed(plain_path, "")
        return
    should_compress = (
        compress and len(content.encode("utf-8")) >= _COMPRESS_STREAM_ARTIFACT_MIN_BYTES
    )
    if should_compress:
        if plain_path.exists():
            plain_path.unlink()
        atomic_write_gzip_text(compressed_path, content)
        return
    if compressed_path.exists():
        compressed_path.unlink()
    write_text_if_changed(plain_path, content)


def write_text_if_changed(path: Path, content: str) -> bool:
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return False
    write_atomic_files({path: content})
    return True


def write_text_artifact(
    base: Path,
    name: str,
    suffix: str,
    content: str,
    *,
    compress: bool,
) -> Path:
    plain_path = base / f"{name}{suffix}"
    compressed_path = base / f"{name}{suffix}.gz"
    should_compress = compress and len(content.encode("utf-8")) >= _COMPRESS_TEXT_ARTIFACT_MIN_BYTES
    if should_compress:
        if plain_path.exists():
            plain_path.unlink()
        atomic_write_gzip_text(compressed_path, content)
        return compressed_path
    if compressed_path.exists():
        compressed_path.unlink()
    write_atomic_files({plain_path: content})
    return plain_path


def prune_superseded_subagent_artifacts(task_root: Path, *, keep_subagent_id: str) -> None:
    subagents_root = task_root / "subagents"
    if not subagents_root.exists():
        return
    raw_names = (
        "prompt.txt",
        "transcript.md",
        "transcript.md.gz",
        "stdout.log",
        "stdout.txt",
        "stdout.txt.gz",
        "stderr.log",
        "stderr.txt",
        "stderr.txt.gz",
        "timeline.yaml",
        "timeline.yaml.gz",
    )
    prefix = f"{keep_subagent_id}-"
    for child in subagents_root.iterdir():
        if not child.is_dir() or child.name.startswith(prefix):
            continue
        for name in raw_names:
            (child / name).unlink(missing_ok=True)
