import ast
from pathlib import Path


_FORBIDDEN_PREFIXES = (
    "litehive.config.normalization",
    "litehive.tasks.persistence",
    "litehive.tasks.worktree_inspection",
)
_SCAN_ROOTS = ("litehive", "tests", "tests_integration")


def _forbidden_imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith(_FORBIDDEN_PREFIXES):
                    hits.append(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith(_FORBIDDEN_PREFIXES):
                hits.append(node.module)
    return hits


def test_litehive_source_does_not_import_removed_modules() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    violations: list[str] = []
    for root_name in _SCAN_ROOTS:
        for path in sorted((repo_root / root_name).rglob("*.py")):
            hits = _forbidden_imports(path)
            if hits:
                violations.append(
                    f"{path.relative_to(repo_root)} -> {', '.join(sorted(set(hits)))}"
                )
    assert violations == []
