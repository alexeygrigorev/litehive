from pathlib import Path

from litehive.agents.session_store import SubagentArtifactPayload, subagent_artifacts
from litehive.agents.subagent_ids import SubagentIdRepository
from litehive.config.workspace import ensure_workspace
from litehive.domain.runtime import Subagent
from litehive.state.records import create_task
from litehive.workspace import Workspace


def test_subagent_id_repository_advances_sqlite_counter(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    task = create_task(tmp_path, title="Allocate subagent ids")
    repository = SubagentIdRepository(workspace)

    first_id = repository.reserve_next_id(task)
    second_id = repository.reserve_next_id(task)

    assert first_id == "SA-0001"
    assert second_id == "SA-0002"
    with workspace.connect() as connection:
        row = connection.execute(
            """
            SELECT next_number
            FROM subagent_id_counters
            WHERE task_id = ?
            """,
            (task.id,),
        ).fetchone()
    assert row["next_number"] == 3


def test_subagent_id_repository_seeds_from_persisted_task_refs_and_sessions(tmp_path: Path) -> None:
    ensure_workspace(tmp_path)
    workspace = Workspace.from_path(tmp_path)
    task = create_task(tmp_path, title="Seed subagent ids")
    task.subagents.append(
        Subagent(
            id="SA-0007",
            role="swe",
            engine="codex",
            status="completed",
            path="subagents/SA-0007-swe",
        )
    )
    subagent_artifacts(workspace, task.id, "SA-0012").save(
        session=SubagentArtifactPayload({"status": "completed"}),
    )
    repository = SubagentIdRepository(workspace)

    subagent_id = repository.reserve_next_id(task)

    assert subagent_id == "SA-0013"
