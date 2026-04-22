"""Helpers for attributing blocking test failures during recovery."""

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Iterable, Literal

from litehive.domain.reports import FollowUpTaskSpec

UNRELATED_TEST_BREAKAGE = "unrelated_test_breakage"

_PYTEST_NODEID_RE = re.compile(
    r"(?P<nodeid>(?:[\w.-]+/)*(?:test[\w.-]*|[\w.-]+_test)\.py(?:::[^\s`]+)*)"
)


@dataclass(frozen=True)
class TestFailureAttribution:
    classification: Literal["changed_surface", "unrelated_breakage", "unknown"]
    failing_tests: tuple[str, ...]
    matched_changed_files: tuple[str, ...]
    changed_files: tuple[str, ...]
    reasoning: str

    @property
    def is_changed_surface(self) -> bool:
        return self.classification == "changed_surface"

    @property
    def is_unrelated_breakage(self) -> bool:
        return self.classification == "unrelated_breakage"

    @property
    def primary_failing_test(self) -> str | None:
        return self.failing_tests[0] if self.failing_tests else None

    def to_prompt_payload(self) -> dict[str, Any]:
        return {
            "classification": self.classification,
            "failing_tests": list(self.failing_tests),
            "matched_changed_files": list(self.matched_changed_files),
            "changed_files": list(self.changed_files),
            "reasoning": self.reasoning,
        }


def attribute_test_failure(
    *,
    changed_files: Iterable[str],
    rejection_message: str = "",
    diagnostics: dict[str, Any] | None = None,
) -> TestFailureAttribution | None:
    changed_surface = tuple(_normalize_paths(changed_files))
    texts = list(_diagnostic_texts(rejection_message=rejection_message, diagnostics=diagnostics))
    failing_tests = tuple(_extract_failing_tests(texts))
    if not failing_tests:
        return None
    if not changed_surface:
        return TestFailureAttribution(
            classification="unknown",
            failing_tests=failing_tests,
            matched_changed_files=(),
            changed_files=changed_surface,
            reasoning="Recovery found a specific failing test, but the task has no recorded changed surface to compare.",
        )

    direct_matches = _direct_surface_matches(failing_tests, changed_surface, texts)
    if direct_matches:
        return TestFailureAttribution(
            classification="changed_surface",
            failing_tests=failing_tests,
            matched_changed_files=tuple(direct_matches),
            changed_files=changed_surface,
            reasoning=(
                "The failing test overlaps the task's changed surface, so recovery should treat it as in-scope."
            ),
        )

    token_matches = _token_surface_matches(failing_tests, changed_surface)
    if token_matches:
        return TestFailureAttribution(
            classification="changed_surface",
            failing_tests=failing_tests,
            matched_changed_files=tuple(token_matches),
            changed_files=changed_surface,
            reasoning=(
                "The failing test targets the same module family as the task's changed files, so recovery should keep "
                "the failure on the current task."
            ),
        )

    return TestFailureAttribution(
        classification="unrelated_breakage",
        failing_tests=failing_tests,
        matched_changed_files=(),
        changed_files=changed_surface,
        reasoning=(
            "Recovery found a specific failing test, and it does not overlap the task's changed surface. Treat it as "
            "pre-existing or unrelated breakage."
        ),
    )


def build_unrelated_test_follow_up(
    *,
    parent_task_id: str,
    failing_test: str,
    changed_files: Iterable[str],
) -> FollowUpTaskSpec:
    changed_surface = ", ".join(_normalize_paths(changed_files)[:3]) or "no recorded changed files"
    test_path = failing_test.split("::", 1)[0]
    return FollowUpTaskSpec(
        title=f"Fix unrelated failing test {Path(test_path).name}",
        rationale=(
            f"Recovery attributed `{failing_test}` to unrelated breakage outside {parent_task_id}'s changed surface "
            f"({changed_surface})."
        ),
        blocking=True,
        goal=f"Repair the failing test `{failing_test}` that blocked {parent_task_id}.",
        acceptance_criteria=[
            f"`uv run pytest -q {failing_test}` passes.",
            "The fix lands without depending on changes from the blocked task.",
        ],
        task_type="bugfix",
    )


def _diagnostic_texts(*, rejection_message: str, diagnostics: dict[str, Any] | None) -> list[str]:
    texts: list[str] = []
    if rejection_message.strip():
        texts.append(rejection_message)
    payload = diagnostics or {}
    last_report = payload.get("last_report")
    if isinstance(last_report, dict):
        test_results = last_report.get("test_results")
        if isinstance(test_results, list):
            texts.extend(str(item) for item in test_results if str(item).strip())
    failing_tests = payload.get("failing_tests")
    if isinstance(failing_tests, list):
        texts.extend(str(item) for item in failing_tests if str(item).strip())
    return texts


def _extract_failing_tests(texts: Iterable[str]) -> list[str]:
    failing_tests: list[str] = []
    seen: set[str] = set()
    for text in texts:
        for match in _PYTEST_NODEID_RE.finditer(text):
            nodeid = match.group("nodeid").strip().rstrip(").,:")
            if nodeid in seen:
                continue
            seen.add(nodeid)
            failing_tests.append(nodeid)
    return failing_tests


def _normalize_paths(paths: Iterable[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw_path in paths:
        stripped = str(raw_path).strip().strip("/")
        if not stripped or stripped in seen:
            continue
        seen.add(stripped)
        normalized.append(stripped)
    return normalized


def _direct_surface_matches(
    failing_tests: tuple[str, ...],
    changed_files: tuple[str, ...],
    texts: list[str],
) -> list[str]:
    matches: list[str] = []
    combined_text = "\n".join(texts)
    changed_set = set(changed_files)
    for nodeid in failing_tests:
        test_path = nodeid.split("::", 1)[0]
        if test_path in changed_set and test_path not in matches:
            matches.append(test_path)
    for changed_file in changed_files:
        if changed_file in combined_text and changed_file not in matches:
            matches.append(changed_file)
    return matches


def _token_surface_matches(failing_tests: tuple[str, ...], changed_files: tuple[str, ...]) -> list[str]:
    changed_tokens = {path: _stem_tokens(path) for path in changed_files}
    matches: list[str] = []
    for nodeid in failing_tests:
        test_tokens = _stem_tokens(nodeid.split("::", 1)[0])
        if not test_tokens:
            continue
        for changed_file, candidate_tokens in changed_tokens.items():
            if candidate_tokens & test_tokens and changed_file not in matches:
                matches.append(changed_file)
    return matches


def _stem_tokens(path: str) -> set[str]:
    stem = Path(path).stem.lower()
    stem = stem.removeprefix("test_").removesuffix("_test")
    tokens = re.split(r"[_\W]+", stem)
    return {token for token in tokens if len(token) >= 4}
