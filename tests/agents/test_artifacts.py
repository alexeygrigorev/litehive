import gzip
from pathlib import Path

from litehive.agents.artifacts import ArtifactService, write_stream_artifact, write_text_artifact


def test_artifact_service_writes_plain_stream_and_removes_compressed_variant(tmp_path: Path) -> None:
    base = tmp_path / "subagent"
    base.mkdir()
    compressed_path = base / "stdout.txt.gz"
    with gzip.open(compressed_path, "wt", encoding="utf-8") as handle:
        handle.write("old")

    ArtifactService(base).write_stream("stdout", "new", compress=False)

    assert (base / "stdout.txt").read_text(encoding="utf-8") == "new"
    assert not compressed_path.exists()


def test_artifact_service_compresses_large_text_and_removes_plain_variant(tmp_path: Path) -> None:
    base = tmp_path / "subagent"
    base.mkdir()
    plain_path = base / "execution_trace.md"
    plain_path.write_text("old", encoding="utf-8")
    content = "x" * 5000

    written = ArtifactService(base).write_text("execution_trace", ".md", content, compress=True)

    assert written == base / "execution_trace.md.gz"
    assert not plain_path.exists()
    with gzip.open(written, "rt", encoding="utf-8") as handle:
        assert handle.read() == content


def test_artifact_compatibility_functions_delegate_to_service(tmp_path: Path) -> None:
    base = tmp_path / "subagent"
    base.mkdir()

    write_stream_artifact(base, "stderr", "error", compress=False)
    prompt_path = write_text_artifact(base, "prompt", ".txt", "prompt", compress=False)

    assert (base / "stderr.txt").read_text(encoding="utf-8") == "error"
    assert prompt_path == base / "prompt.txt"
    assert prompt_path.read_text(encoding="utf-8") == "prompt"


def test_artifact_service_removes_both_text_variants(tmp_path: Path) -> None:
    base = tmp_path / "subagent"
    base.mkdir()
    plain_path = base / "execution_trace.md"
    compressed_path = base / "execution_trace.md.gz"
    plain_path.write_text("old", encoding="utf-8")
    with gzip.open(compressed_path, "wt", encoding="utf-8") as handle:
        handle.write("old")

    ArtifactService(base).remove_text("execution_trace", ".md")

    assert not plain_path.exists()
    assert not compressed_path.exists()
