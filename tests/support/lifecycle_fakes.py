from typing import Any

from litehive.domain.common import PipelineState
from litehive.lifecycle.journal import PipelineJournal
from litehive.lifecycle.persistence import TaskState
from litehive.lifecycle.sessions import Session


class InMemorySessionStore:
    """Session store fake for lifecycle tests."""

    def __init__(self) -> None:
        self._sessions: dict[tuple[str, PipelineState, str], Session] = {}

    def get_or_create(self, task_id: str, node_name: PipelineState, engine_name: str) -> Session:
        return self._sessions.setdefault((task_id, node_name, engine_name), Session())

    def persist(self, task_id: str, node_name: PipelineState, engine_name: str, session: Session) -> None:
        self._sessions[(task_id, node_name, engine_name)] = session

    def clear_node_sessions(self, task_id: str, node_name: PipelineState) -> None:
        for key in [k for k in self._sessions if k[:2] == (task_id, node_name)]:
            del self._sessions[key]


class InMemoryJournal(PipelineJournal):
    """Pipeline journal fake for lifecycle tests."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[dict[str, Any]] = []

    def _store(
        self,
        task_id: str,
        seq: int,
        created_at: str,
        kind: str,
        payload: dict[str, Any],
    ) -> None:
        self.records.append(
            {
                "task_id": task_id,
                "seq": seq,
                "created_at": created_at,
                "kind": kind,
                "payload": payload,
            }
        )


class InMemoryPersistence:
    """TaskState persistence fake for lifecycle tests."""

    def __init__(self) -> None:
        self._states: dict[str, TaskState] = {}

    def save(self, state: TaskState) -> None:
        self._states[state.task_id] = state

    def load(self, task_id: str) -> TaskState:
        return self._states[task_id]
