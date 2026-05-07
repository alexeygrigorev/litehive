"""
Subagent artifact writers.

Each helper is a thin wrapper around the atomic-write primitives in
`litehive.state.persist` that knows about one specific kind of file
the runner stores under `<task>/subagents/<id>-<role>/`: stream logs
(stdout/stderr), the execution trace, the prompt, and similar
companions.

Subagent artifacts are debug evidence. The runner does not delete
prior-attempt artifacts on the success path; the only deletion these
helpers do is *format-flip cleanup* — when the same logical artifact
flips between plain text and gzipped text, the variant that no longer
holds the current content is removed so we never have two
out-of-sync copies of one file. See `docs/code-style.md` (Subagent
Artifacts).
"""

from pathlib import Path

from litehive.state.persist import atomic_write_gzip_text, write_atomic_files

_COMPRESS_STREAM_ARTIFACT_MIN_BYTES = 4096
_COMPRESS_TEXT_ARTIFACT_MIN_BYTES = 4096


class ArtifactService:
    """
    Persist file-backed subagent debug evidence under one artifact root.

    The subagent manager and session writer call this service when they
    need disk artifacts beside the SQLite session rows. The service
    owns the base path so callers do not have to reassemble artifact
    filenames for each prompt, trace, stdout, or stderr write.
    """

    def __init__(self, base: Path) -> None:
        self.base = base

    def write_stream(self, name: str, content: str, compress: bool) -> None:
        """
        Persist a streaming log artifact for a subagent.

        The artifact lives at `<base>/<name>.txt` or
        `<base>/<name>.txt.gz`. The subagent manager uses this for
        stdout/stderr progress writes, where small payloads stay plain
        and large payloads may be gzipped.

        `name` identifies the logical stream, usually `stdout` or
        `stderr`. `compress` permits compression; content size still
        decides whether compression is worth using.
        """
        plain_path = self.base / f"{name}.txt"
        compressed_path = self.base / f"{name}.txt.gz"
        if compress and not content:
            if compressed_path.exists():
                compressed_path.unlink()
            write_text_if_changed(plain_path, "")
            return
        should_compress = compress and len(content.encode("utf-8")) >= _COMPRESS_STREAM_ARTIFACT_MIN_BYTES
        if should_compress:
            if plain_path.exists():
                plain_path.unlink()
            atomic_write_gzip_text(compressed_path, content)
            return
        if compressed_path.exists():
            compressed_path.unlink()
        write_text_if_changed(plain_path, content)

    def write_text(self, name: str, suffix: str, content: str, compress: bool) -> Path:
        """
        Persist a named non-stream text artifact for a subagent.

        The session writer uses this for prompts and execution traces,
        where the suffix distinguishes `.txt` prompt files from `.md`
        trace files. The returned path is the concrete file written,
        which may be the gzipped variant when compression is enabled.
        """
        plain_path = self.base / f"{name}{suffix}"
        compressed_path = self.base / f"{name}{suffix}.gz"
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

    def remove_text(self, name: str, suffix: str) -> None:
        """
        Remove both storage variants of a named text artifact.

        Session finalization calls this only when a trace has become
        semantically invalid for the current subagent state. It is not
        a success-path cleanup API; debug evidence should normally be
        retained for later inspection.
        """
        (self.base / f"{name}{suffix}").unlink(missing_ok=True)
        (self.base / f"{name}{suffix}.gz").unlink(missing_ok=True)


def write_stream_artifact(base: Path, name: str, content: str, compress: bool) -> None:
    """
    Persist a streaming log artifact for a subagent.

    Compatibility wrapper used by current manager/session call sites.
    New code should create `ArtifactService(base)` once and call
    `write_stream` so the artifact root is explicit in one object.
    """
    ArtifactService(base).write_stream(name, content, compress)


def write_text_if_changed(path: Path, content: str) -> bool:
    """
    Write content to a path only if it differs from what is there.

    ArtifactService uses this for frequently flushed stream logs so a
    repeated progress callback does not churn the filesystem or mtime
    when stdout/stderr has not changed. Returns `True` when a write
    occurred.
    """
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return False
    write_atomic_files({path: content})
    return True


def write_text_artifact(
    base: Path,
    name: str,
    suffix: str,
    content: str,
    compress: bool,
) -> Path:
    """
    Persist a non-streaming text artifact for a subagent.

    Compatibility wrapper used by current manager/session call sites.
    New code should create `ArtifactService(base)` once and call
    `write_text` so the artifact root is explicit in one object.
    """
    return ArtifactService(base).write_text(name, suffix, content, compress)


def remove_text_artifact(base: Path, name: str, suffix: str) -> None:
    """
    Remove both variants of a text artifact.

    Compatibility wrapper used by current session code. New code should
    call `ArtifactService(base).remove_text(...)` so deletion remains
    tied to one artifact root and does not look like general cleanup.
    """
    ArtifactService(base).remove_text(name, suffix)
