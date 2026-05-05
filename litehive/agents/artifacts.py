"""Subagent artifact writers.

Each helper is a thin wrapper around the atomic-write primitives in
``litehive.state.persist`` that knows about one specific kind of file
the runner stores under ``<task>/subagents/<id>-<role>/``: stream logs
(stdout/stderr), the execution trace, the prompt, and similar
companions.

Subagent artifacts are debug evidence. The runner does not delete
prior-attempt artifacts on the success path; the only deletion these
helpers do is *format-flip cleanup* — when the same logical artifact
flips between plain text and gzipped text, the variant that no longer
holds the current content is removed so we never have two
out-of-sync copies of one file. See ``docs/code-style.md`` (Subagent
Artifacts).
"""

from pathlib import Path

from litehive.state.persist import atomic_write_gzip_text, write_atomic_files

_COMPRESS_STREAM_ARTIFACT_MIN_BYTES = 4096
_COMPRESS_TEXT_ARTIFACT_MIN_BYTES = 4096


def write_stream_artifact(base: Path, name: str, content: str, compress: bool) -> None:
    """Persist a streaming log artifact (stdout / stderr) for a subagent.

    The artifact lives at either ``<base>/<name>.txt`` or
    ``<base>/<name>.txt.gz``. The format is chosen by ``compress`` and
    by ``content`` size: small payloads stay plain even when compression
    is enabled. When the chosen format flips, the off-format companion
    is removed so the directory never carries two stale copies of the
    same logical stream.
    """
    plain_path = base / f"{name}.txt"
    compressed_path = base / f"{name}.txt.gz"
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


def write_text_if_changed(path: Path, content: str) -> bool:
    """Write ``content`` to ``path`` only if it differs from what's there.

    Returns ``True`` when a write occurred. Used so repeatedly persisting
    a streaming artifact (which is rewritten each time the runner
    flushes) does not churn the filesystem (or its mtime) when the
    payload has not actually changed since the last flush.
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
    """Persist a non-streaming text artifact (prompt, execution trace, …).

    Behaviour mirrors :func:`write_stream_artifact` but the suffix is
    caller-supplied (e.g. ``.md``, ``.txt``) so the same helper covers
    multiple artifact kinds. Returns the path actually written, which
    depends on whether the payload was large enough to be compressed.
    Format-flip cleanup is performed for the same reason as in
    :func:`write_stream_artifact`.
    """
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


def remove_text_artifact(base: Path, name: str, suffix: str) -> None:
    """Remove both variants (plain + gzipped) of a text artifact.

    Used when the caller has determined that the artifact is no longer
    semantically valid (not as part of routine cleanup). Subagent
    debug evidence must not be removed via this helper on the success
    path.
    """
    (base / f"{name}{suffix}").unlink(missing_ok=True)
    (base / f"{name}{suffix}.gz").unlink(missing_ok=True)
