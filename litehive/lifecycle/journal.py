"""SQLite-backed journal of pipeline runner events.

Every runner-level event — task started, transition fired, stop requested,
task finished — is appended here. Each row is a (task_id, seq) pair so a full
replay of a task's lifecycle is possible later.

The base class owns logging and sequencing; subclasses implement storage.
"""

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from litehive.db.schema import connect_workspace_db
from litehive.domain.common import utcnow
from litehive.tasks.event_log import append_task_event

from litehive.domain.common import PipelineState
from litehive.domain.lifecycle_deltas import StateDelta
from .events import Event

logger = logging.getLogger(__name__)


def _event_payload(event: Event) -> dict[str, Any]:
    """Flatten a transition Event into a JSON-friendly dict for the transition row; non-dataclass events store as ``{}`` rather than blowing up the writer."""
    if is_dataclass(event):
        return asdict(event)
    return {}


def _delta_payload(delta: StateDelta) -> dict[str, Any]:
    """Drop default/empty fields from a ``StateDelta`` so the journal records only the values the transition actually changed; keeps replay diffs readable."""
    return {k: v for k, v in asdict(delta).items() if v not in (None, False, (), [], {})}


# Kinds recorded in the journal. Keep this list tight — adding a kind is a
# schema-visible change even though the sqlite column is free-form text.
KIND_TASK_STARTED = "task_started"
KIND_TRANSITION = "transition"
KIND_STOP_REQUESTED = "stop_requested"
KIND_TASK_FINISHED = "task_finished"


class PipelineJournal(ABC):
    """Abstract base class for runner-event journals.

    The base class:
      - generates monotonic per-task sequence numbers,
      - timestamps each record,
      - logs the event,
      - delegates the actual write to ``_store``.

    Subclasses only implement ``_store``. They may also override
    ``_load_starting_seq`` to resume sequencing across process restarts.
    """

    def __init__(self) -> None:
        """Initialize the per-task seq cache; concrete subclasses lazily fill it from storage on the first ``_append`` for a task."""
        self._next_seq: dict[str, int] = {}

    # ── public entry points used by the Runner ───────────────────────

    def task_started(self, task_id: str, stage: PipelineState) -> None:
        """Record that the runner began driving a task; called once per task pickup so replay can find the start."""
        self._append(KIND_TASK_STARTED, task_id, {"stage": str(stage)})

    def transition(
        self,
        task_id: str,
        from_stage: PipelineState,
        event: Event,
        to_stage: PipelineState,
        rule_description: str,
        delta: StateDelta,
    ) -> None:
        """Record one state-machine edge after the runner fires it; the structured row feeds analytics like 'how often does testing reject?'."""
        self._append(
            KIND_TRANSITION,
            task_id,
            {
                "from_stage": str(from_stage),
                "event_type": type(event).__name__,
                "event_payload": _event_payload(event),
                "to_stage": str(to_stage),
                "rule_description": rule_description,
                "delta": _delta_payload(delta),
            },
        )

    def stop_requested(self, task_id: str, stage: PipelineState) -> None:
        """Record an external stop intent (CLI ``stop``, daemon shutdown) so the gap between request and termination is reconstructable."""
        self._append(KIND_STOP_REQUESTED, task_id, {"stage": str(stage)})

    def task_finished(self, task_id: str, stage: PipelineState) -> None:
        """Record that the task reached a terminal state; bookend for ``task_started`` used by replay and post-mortem reporting."""
        self._append(KIND_TASK_FINISHED, task_id, {"stage": str(stage)})

    # ── template method ──────────────────────────────────────────────

    def _append(self, kind: str, task_id: str, payload: dict[str, Any]) -> None:
        """Template method shared by every public entry point: assigns the next per-task seq, timestamps the row, logs it, then delegates the write to ``_store``."""
        if task_id not in self._next_seq:
            self._next_seq[task_id] = self._load_starting_seq(task_id)
        seq = self._next_seq[task_id]
        self._next_seq[task_id] = seq + 1
        created_at = utcnow()
        self._log(kind, task_id, payload)
        self._store(task_id, seq, created_at, kind, payload)

    def _log(self, kind: str, task_id: str, payload: dict[str, Any]) -> None:
        """Emit a human-readable log line per journal kind so operators can follow runner activity in stderr without querying SQLite."""
        if kind == KIND_TASK_STARTED:
            logger.info("task %s: starting at stage=%s", task_id, payload["stage"])
        elif kind == KIND_TRANSITION:
            logger.info(
                "task %s: %s --[%s]--> %s (%s)",
                task_id,
                payload["from_stage"],
                payload["event_type"],
                payload["to_stage"],
                payload["rule_description"] or "no description",
            )
        elif kind == KIND_STOP_REQUESTED:
            logger.info("task %s: stop requested at stage=%s", task_id, payload["stage"])
        elif kind == KIND_TASK_FINISHED:
            logger.info("task %s: reached terminal %s", task_id, payload["stage"])

    def _load_starting_seq(self, task_id: str) -> int:
        """Hook for subclasses to resume seq numbering across restarts; the base returns 0 so in-memory journals always start fresh."""
        del task_id
        return 0

    @abstractmethod
    def _store(
        self,
        task_id: str,
        seq: int,
        created_at: str,
        kind: str,
        payload: dict[str, Any],
    ) -> None:
        """Persist one prepared journal row; the only piece a backend must implement, called from ``_append`` after seq + timestamp + log."""
        ...


class SqliteJournal(PipelineJournal):
    """Writes journal events to the workspace SQLite db.

    Transitions land in the structured ``pipeline_transitions`` table so their
    columns can be queried directly (e.g. "how often did testing reject?").
    Lifecycle events (task_started, stop_requested, task_finished) land in
    ``pipeline_journal`` with a free-form payload.

    Uses ``connect_workspace_db`` so migrations run automatically and pragmas
    match the rest of the codebase.
    """

    def __init__(self, workspace_root: Path) -> None:
        """Bind the journal to a workspace; ``workspace_root`` selects which SQLite db ``connect_workspace_db`` opens for every write."""
        super().__init__()
        self.workspace_root = workspace_root

    def _store(
        self,
        task_id: str,
        seq: int,
        created_at: str,
        kind: str,
        payload: dict[str, Any],
    ) -> None:
        """Route writes to the structured ``pipeline_transitions`` table or the free-form ``pipeline_journal`` table depending on kind, so transitions stay queryable by column."""
        if kind == KIND_TRANSITION:
            self._insert_transition(task_id, seq, created_at, payload)
        else:
            self._insert_lifecycle(task_id, seq, created_at, kind, payload)

    def _insert_transition(
        self,
        task_id: str,
        seq: int,
        created_at: str,
        payload: dict[str, Any],
    ) -> None:
        """Write one transition row plus its mirrored task-event so analytics and operator activity feeds see the same edge."""
        with connect_workspace_db(self.workspace_root) as connection:
            connection.execute(
                """
                INSERT INTO pipeline_transitions (
                    task_id, seq, created_at,
                    from_stage, event_type, event_payload,
                    to_stage, rule_description, delta
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    seq,
                    created_at,
                    payload["from_stage"],
                    payload["event_type"],
                    json.dumps(payload["event_payload"], sort_keys=True),
                    payload["to_stage"],
                    payload["rule_description"],
                    json.dumps(payload["delta"], sort_keys=True),
                ),
            )
            append_task_event(
                self.workspace_root,
                event_type="pipeline_transition_recorded",
                task_id=task_id,
                payload={
                    "pipeline_transition": {
                        "task_id": task_id,
                        "seq": seq,
                        "created_at": created_at,
                        "from_stage": payload["from_stage"],
                        "event_type": payload["event_type"],
                        "event_payload": payload["event_payload"],
                        "to_stage": payload["to_stage"],
                        "rule_description": payload["rule_description"],
                        "delta": payload["delta"],
                    }
                },
            )
            connection.commit()

    def _insert_lifecycle(
        self,
        task_id: str,
        seq: int,
        created_at: str,
        kind: str,
        payload: dict[str, Any],
    ) -> None:
        """Write a non-transition lifecycle row (started / stop_requested / finished) and mirror it onto the task event log for activity replay."""
        with connect_workspace_db(self.workspace_root) as connection:
            connection.execute(
                """
                INSERT INTO pipeline_journal (task_id, seq, created_at, kind, payload)
                VALUES (?, ?, ?, ?, ?)
                """,
                (task_id, seq, created_at, kind, json.dumps(payload, sort_keys=True)),
            )
            append_task_event(
                self.workspace_root,
                event_type="pipeline_journal_recorded",
                task_id=task_id,
                payload={
                    "pipeline_journal": {
                        "task_id": task_id,
                        "seq": seq,
                        "created_at": created_at,
                        "kind": kind,
                        "payload": payload,
                    }
                },
            )
            connection.commit()

    def _load_starting_seq(self, task_id: str) -> int:
        """Resume per-task seq numbering across runner restarts by querying ``MAX(seq)`` across both journal tables; without this, restarts would collide on existing rows."""
        with connect_workspace_db(self.workspace_root) as connection:
            row = connection.execute(
                """
                SELECT MAX(seq) AS max_seq FROM (
                    SELECT seq FROM pipeline_transitions WHERE task_id = ?
                    UNION ALL
                    SELECT seq FROM pipeline_journal      WHERE task_id = ?
                )
                """,
                (task_id, task_id),
            ).fetchone()
        if row and row["max_seq"] is not None:
            max_seq = row["max_seq"]
        else:
            max_seq = -1
        return max_seq + 1

    def load_transitions(self, task_id: str) -> list[dict[str, Any]]:
        """Replay the structured transition rows for one task; used by the diagnostics CLI and the recovery agent to reconstruct pipeline history."""
        with connect_workspace_db(self.workspace_root) as connection:
            rows = connection.execute(
                """
                SELECT seq, created_at, from_stage, event_type, event_payload,
                       to_stage, rule_description, delta
                FROM pipeline_transitions
                WHERE task_id = ?
                ORDER BY seq ASC
                """,
                (task_id,),
            ).fetchall()
        return [
            {
                "seq": row["seq"],
                "created_at": row["created_at"],
                "from_stage": row["from_stage"],
                "event_type": row["event_type"],
                "event_payload": json.loads(row["event_payload"]),
                "to_stage": row["to_stage"],
                "rule_description": row["rule_description"],
                "delta": json.loads(row["delta"]),
            }
            for row in rows
        ]

    def load_lifecycle(self, task_id: str) -> list[dict[str, Any]]:
        """Replay non-transition events (started/stop_requested/finished); paired with ``load_transitions`` to render a task's full timeline."""
        with connect_workspace_db(self.workspace_root) as connection:
            rows = connection.execute(
                """
                SELECT seq, created_at, kind, payload
                FROM pipeline_journal
                WHERE task_id = ?
                ORDER BY seq ASC
                """,
                (task_id,),
            ).fetchall()
        return [
            {
                "seq": row["seq"],
                "created_at": row["created_at"],
                "kind": row["kind"],
                "payload": json.loads(row["payload"]),
            }
            for row in rows
        ]


class NullJournal(PipelineJournal):
    """Drops every record; use when the runner should not journal at all."""

    def _store(
        self,
        task_id: str,
        seq: int,
        created_at: str,
        kind: str,
        payload: dict[str, Any],
    ) -> None:
        """No-op store: tests and one-shot CLIs use ``NullJournal`` to keep the runner contract while skipping persistence."""
        del task_id, seq, created_at, kind, payload
        return None

    def _log(self, kind: str, task_id: str, payload: dict[str, Any]) -> None:
        """Suppress runner log lines too, so lifecycle tests asserting on stderr stay deterministic when the journal is disabled."""
        del kind, task_id, payload
        return None
