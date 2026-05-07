"""Static architecture guardrails for ownership boundaries."""

import ast
import tomllib
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPO_ROOT / "litehive"


def _python_files(*parts: str) -> list[Path]:
    root = PACKAGE_ROOT.joinpath(*parts)
    if root.is_file():
        return [root]
    return sorted(root.rglob("*.py"))


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _imported_modules(tree: ast.AST) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module)
    return modules


def _call_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _name_of(node.func)
            if name is not None:
                names.add(name)
    return names


def _name_of(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _name_of(node.value)
        return node.attr if prefix is None else f"{prefix}.{node.attr}"
    return None


def _annotation_source(node: ast.AST | None) -> str | None:
    if node is None:
        return None
    return ast.unparse(node)


def _string_constants(tree: ast.AST) -> list[str]:
    return [node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)]


def _contains_mutating_sql(strings: list[str]) -> bool:
    mutating_sql = ("INSERT ", "UPDATE ", "DELETE ", "REPLACE ", "CREATE ", "DROP ", "ALTER ")
    return any(line.lstrip().upper().startswith(mutating_sql) for value in strings for line in value.splitlines())


def test_cli_modules_do_not_mutate_domain_state_with_direct_sqlite() -> None:
    violations: list[str] = []
    for path in _python_files("cli"):
        tree = _tree(path)
        modules = _imported_modules(tree)
        calls = _call_names(tree)
        reaches_sqlite = "sqlite3" in modules or "connect_workspace_db" in calls
        if not reaches_sqlite:
            continue
        if _contains_mutating_sql(_string_constants(tree)):
            violations.append(str(path.relative_to(REPO_ROOT)))

    assert violations == []


def test_role_prompt_code_does_not_mutate_tasks() -> None:
    prompt_policy_files = list(_python_files("roles"))
    forbidden_modules = {
        "litehive.tasks.status",
        "litehive.tasks.queue",
        "litehive.tasks.runtime",
    }
    forbidden_calls = {
        "append_task_event",
        "create_task",
        "dequeue_next_task",
        "drop_task_from_workspace_state",
        "persist_task_and_state_without_runner_guard",
        "persist_tasks_and_state",
        "save_state",
        "save_state_without_runner_guard",
        "save_task",
        "save_task_runtime",
    }
    violations: list[str] = []
    for path in prompt_policy_files:
        tree = _tree(path)
        imported_forbidden = sorted(module for module in _imported_modules(tree) if module in forbidden_modules)
        called_forbidden = sorted(name for name in _call_names(tree) if name.split(".")[-1] in forbidden_calls)
        if imported_forbidden or called_forbidden:
            rel = path.relative_to(REPO_ROOT)
            violations.append(f"{rel}: imports={imported_forbidden} calls={called_forbidden}")

    assert violations == []


def test_status_rendering_and_diagnostics_do_not_repair_or_mutate() -> None:
    status_files = (
        _python_files("observability", "status.py")
        + _python_files("observability", "status_diagnostics.py")
        + _python_files("observability", "status_io.py")
        + _python_files("observability", "status_loaders.py")
        + _python_files("observability", "status_probes.py")
        + _python_files("observability", "status_rendering.py")
        + _python_files("observability", "status_types.py")
    )
    forbidden_modules = {
        "litehive.recovery.execution_recovery",
        "litehive.recovery.workspace_repair",
        "litehive.tasks.queue",
        "litehive.tasks.status",
        "litehive.tasks.runtime",
    }
    forbidden_call_suffixes = {
        "append_task_event",
        "bootstrap",
        "clear_process_state",
        "clear_runner_lock_metadata",
        "create_task",
        "persist_task_and_state_without_runner_guard",
        "persist_tasks_and_state",
        "recover_stale_runner_state",
        "repair_workspace",
        "reset_task_for_recovery",
        "save_state",
        "save_task",
        "write_task_runtime",
    }
    violations: list[str] = []
    for path in status_files:
        tree = _tree(path)
        imported_forbidden = sorted(module for module in _imported_modules(tree) if module in forbidden_modules)
        called_forbidden = sorted(name for name in _call_names(tree) if name.split(".")[-1] in forbidden_call_suffixes)
        writes_sql = _contains_mutating_sql(_string_constants(tree))
        if imported_forbidden or called_forbidden or writes_sql:
            rel = path.relative_to(REPO_ROOT)
            violations.append(f"{rel}: imports={imported_forbidden} calls={called_forbidden} writes_sql={writes_sql}")

    assert violations == []


def test_status_state_loading_uses_read_only_store_api() -> None:
    tree = _tree(PACKAGE_ROOT / "observability" / "status_loaders.py")
    calls = _call_names(tree)

    assert "load_workspace_state_read_only" in calls
    assert "load_workspace_state" not in calls


def test_domain_models_do_not_perform_file_io_or_persistence_migration() -> None:
    forbidden_modules = {
        "sqlite3",
        "yaml",
        "litehive.db.schema",
        "litehive.state.persist",
        "litehive.state.records",
        "litehive.state.store",
        "litehive.tasks.event_log",
    }
    forbidden_call_suffixes = {
        "connect_workspace_db",
        "load_config",
        "migrate",
        "open",
        "read_text",
        "runtime_store",
        "safe_load",
        "save_state",
        "save_task",
        "write_text",
    }
    violations: list[str] = []
    for path in _python_files("domain"):
        tree = _tree(path)
        imported_forbidden = sorted(module for module in _imported_modules(tree) if module in forbidden_modules)
        called_forbidden = sorted(name for name in _call_names(tree) if name.split(".")[-1] in forbidden_call_suffixes)
        if imported_forbidden or called_forbidden:
            rel = path.relative_to(REPO_ROOT)
            violations.append(f"{rel}: imports={imported_forbidden} calls={called_forbidden}")

    assert violations == []


def test_process_profiles_do_not_use_packaged_yaml() -> None:
    profiles_root = PACKAGE_ROOT / "config" / "profiles"
    yaml_files = sorted(path.name for path in profiles_root.glob("*.yaml"))
    loader_tree = _tree(profiles_root / "loader.py")
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    wheel_include = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"].get("include", [])

    assert yaml_files == []
    assert "yaml" not in _imported_modules(loader_tree)
    assert "litehive/config/profiles/*.yaml" not in wheel_include


def test_pyrefly_requires_return_annotations() -> None:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    pyrefly_config = pyproject["tool"]["pyrefly"]

    assert pyrefly_config["errors"]["unannotated-parameter"] == "error"
    assert pyrefly_config["errors"]["unannotated-return"] == "error"


def test_daemon_runner_liveness_helpers_remain_domain_typed() -> None:
    """
    Keep daemon loop predicates on domain types, not loose objects.
    """
    tree = _tree(PACKAGE_ROOT / "daemon" / "execution.py")
    annotations: dict[str, list[str | None]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in {"_runner_is_live", "_has_work"}:
            annotations[node.name] = [_annotation_source(argument.annotation) for argument in node.args.args]

    assert annotations == {
        "_has_work": ["WorkspaceState"],
        "_runner_is_live": ["RunnerStatusState"],
    }


def test_merge_warning_type_is_not_reintroduced() -> None:
    """
    Keep merge feedback on structured task/report fields, not a warning class.

    The old ``MergeWarning`` name did not survive the SQLite/report
    ownership migration. Merge-related operator signals should remain
    explicit task state, reports, or activity entries instead of a
    generic Python warning category whose consumers are hard to trace.
    """
    definitions: list[str] = []
    for path in _python_files():
        tree = _tree(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) and node.name == "MergeWarning":
                definitions.append(str(path.relative_to(REPO_ROOT)))

    assert definitions == []


def test_production_code_uses_subagent_name_not_subagent_ref() -> None:
    """
    Keep Litehive-owned code on the domain name for persisted subagents.

    Heru's transport model is still called ``SubagentRef``. Inside
    Litehive, ``TaskRecord.subagents`` stores the subagent record
    itself, so production code should import and annotate ``Subagent``.
    ``domain.runtime`` is the one allowed compatibility boundary.
    """
    violations: list[str] = []
    allowed_path = PACKAGE_ROOT / "domain" / "runtime.py"
    for path in _python_files():
        if path == allowed_path:
            continue
        tree = _tree(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id == "SubagentRef":
                violations.append(str(path.relative_to(REPO_ROOT)))
                break

    assert violations == []


def test_domain_docs_define_subagent_manager_boundary() -> None:
    """
    Keep the broad manager role documented in the domain vocabulary.

    SubagentManager is intentionally a coordinator around focused
    collaborators; when that boundary disappears from the docs, new
    behavior tends to accumulate in the manager by default.
    """
    domain_doc = (REPO_ROOT / "docs" / "domain.md").read_text(encoding="utf-8")

    assert "## Subagent Execution Boundary" in domain_doc
    assert "`litehive.agents.manager.SubagentManager` is the per-invocation coordinator" in domain_doc
    assert "`EngineManager`" in domain_doc
    assert "`SubagentRunCallbacks`" in domain_doc
    assert "`SubagentSessionManager`" in domain_doc
    assert "`SandboxLauncher`" in domain_doc


def test_core_boundary_dependency_graph_does_not_gain_new_edges() -> None:
    bounded_contexts = {"tasks", "state", "lifecycle", "agents", "observability"}
    allowed_edges = {
        ("agents", "observability"),
        ("agents", "state"),
        ("agents", "tasks"),
        ("lifecycle", "agents"),
        ("lifecycle", "state"),
        ("lifecycle", "tasks"),
        ("observability", "lifecycle"),
        ("observability", "state"),
        ("observability", "tasks"),
        ("state", "tasks"),
        ("tasks", "agents"),
        ("tasks", "lifecycle"),
        ("tasks", "observability"),
        ("tasks", "state"),
    }
    allowed_cycle_edges = {
        ("agents", "observability"),
        ("agents", "state"),
        ("agents", "tasks"),
        ("lifecycle", "agents"),
        ("lifecycle", "state"),
        ("lifecycle", "tasks"),
        ("observability", "lifecycle"),
        ("observability", "state"),
        ("observability", "tasks"),
        ("state", "tasks"),
        ("tasks", "agents"),
        ("tasks", "lifecycle"),
        ("tasks", "observability"),
        ("tasks", "state"),
    }
    edges: set[tuple[str, str]] = set()
    for path in _python_files():
        relative = path.relative_to(PACKAGE_ROOT).parts
        if not relative or relative[0] not in bounded_contexts:
            continue
        source = relative[0]
        for module in _imported_modules(_tree(path)):
            parts = module.split(".")
            if len(parts) >= 2 and parts[0] == "litehive" and parts[1] in bounded_contexts and parts[1] != source:
                edges.add((source, parts[1]))

    assert sorted(edges) == sorted(allowed_edges)
    assert sorted(_cycle_participating_edges(edges, bounded_contexts)) == sorted(allowed_cycle_edges)


def _cycle_participating_edges(edges: set[tuple[str, str]], nodes: set[str]) -> set[tuple[str, str]]:
    graph: dict[str, set[str]] = {node: set() for node in nodes}
    for source, target in edges:
        graph[source].add(target)

    cycle_edges: set[tuple[str, str]] = set()
    for source, target in edges:
        if _can_reach(graph, target, source, ignored_edge=(source, target)):
            cycle_edges.add((source, target))
    return cycle_edges


def _can_reach(
    graph: dict[str, set[str]],
    source: str,
    target: str,
    *,
    ignored_edge: tuple[str, str],
) -> bool:
    seen: set[str] = set()
    pending = [source]
    while pending:
        node = pending.pop()
        if node == target:
            return True
        if node in seen:
            continue
        seen.add(node)
        pending.extend(
            next_node for next_node in graph[node] if next_node not in seen and (node, next_node) != ignored_edge
        )
    return False
