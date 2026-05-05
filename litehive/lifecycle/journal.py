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
from typing import Any

from litehive.domain.common import utcnow
from litehive.tasks.event_log import append_task_event
from litehive.workspace import Workspace

from litehive.domain.common import PipelineState
from litehive.domain.lifecycle_deltas import StateDelta
from .events import Event

logger = logging.getLogger(__name__)


def _event_payload(event: Event) -> dict[str, Any]:
    """
    Flatten a transition Event into a JSON-friendly dict for the row.

    Non-dataclass events store as ``{}`` so a future event subclass that
    forgets ``@dataclass`` cannot crash the journal writer mid-transition
    — losing the payload is preferable to losing the row.
    """
    if is_dataclass(event):
        return asdict(event)
    return {}


def _decode_transition_rows(rows: list) -> list[dict[str, Any]]:
    """
    Project ``pipeline_transitions`` SQL rows to plain replay dicts.

    The two JSON columns (``event_payload`` and ``delta``) are
    decoded here so the diagnostics CLI / recovery agent can read
    structured payloads instead of opaque strings. Caller:
    :meth:`SqlitePipelineJournal.load_transitions`.
    """
    decoded: list[dict[str, Any]] = []
    for row in rows:
        decoded.append({
            "seq": row["seq"],
            "created_at": row["created_at"],
            "from_stage": row["from_stage"],
            "event_type": row["event_type"],
            "event_payload": json.loads(row["event_payload"]),
            "to_stage": row["to_stage"],
            "rule_description": row["rule_description"],
            "delta": json.loads(row["delta"]),
        })
    return decoded


_DELTA_DEFAULT_VALUES: tuple[Any, ...] = (None, False, (), [], {})


def _delta_payload(delta: StateDelta) -> dict[str, Any]:
    """
    Drop default/empty fields from a ``StateDelta`` before journaling.

    The journal records only the fields the transition actually changed
    so replay diffs stay readable; without this, every row would carry
    every dormant ``StateDelta`` field as ``None``/``False``/empty.
    """
    payload: dict[str, Any] = {}
    for key, value in asdict(delta).items():
        if value not in _DELTA_DEFAULT_VALUES:
            payload[key] = value
    return payload


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
        """
        Initialize the per-task seq cache.

        The cache is filled lazily on the first ``_append`` for a task, so a
        concrete subclass that resumes seq numbering from storage only pays
        that read once per task even when the journal is shared across many
        tasks in one process.
        """
        self._next_seq: dict[str, int] = {}

    # ── public entry points used by the Runner ───────────────────────

    def task_started(self, task_id: str, stage: PipelineState) -> None:
        """
        Record that the runner has begun driving a task.

        Called once per task pickup; without this row, replay cannot find a
        coherent start for the lifecycle and the subsequent transition rows
        look like they appear out of nowhere.
        """
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
        """
        Record one state-machine edge after the runner fires it.

        The structured row is what feeds analytics like "how often does
        testing reject?" and powers the recovery agent's view of pipeline
        history; the columns are deliberately kept directly queryable so
        questions can be answered without re-parsing JSON.
        """
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
        """
        Record an external stop intent (CLI ``stop``, daemon shutdown).

        Recording the request separately from termination makes the gap
        between "operator asked us to stop" and "task actually stopped"
        reconstructable from replay.
        """
        self._append(KIND_STOP_REQUESTED, task_id, {"stage": str(stage)})

    def task_finished(self, task_id: str, stage: PipelineState) -> None:
        """
        Record that the task reached a terminal state.

        Bookend for ``task_started`` used by replay and post-mortem
        reporting; without it, the timeline cannot tell whether the
        runner finished cleanly or was interrupted.
        """
        self._append(KIND_TASK_FINISHED, task_id, {"stage": str(stage)})

    # ── template method ──────────────────────────────────────────────

    def _append(self, kind: str, task_id: str, payload: dict[str, Any]) -> None:
        """
        Template method shared by every public entry point above.

        Assigns the next per-task seq (lazy-loading from storage on first
        use), timestamps the row, logs it, and delegates the actual write
        to ``_store``. Keeping all four steps here is what lets backends
        only implement ``_store``.
        """
        if task_id not in self._next_seq:
            self._next_seq[task_id] = self._load_starting_seq(task_id)
        seq = self._next_seq[task_id]
        self._next_seq[task_id] = seq + 1
        created_at = utcnow()
        self._log(kind, task_id, payload)
        self._store(task_id, seq, created_at, kind, payload)

    def _log(self, kind: str, task_id: str, payload: dict[str, Any]) -> None:
        """
        Emit a human-readable log line for this journal kind.

        Logging happens here (not in ``_store``) so operators can follow
        runner activity on stderr without querying SQLite, and so a
        ``NullJournal`` can suppress logs by overriding only this method.
        """
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
        """
        Resume seq numbering after a process restart.

        Default implementation returns 0 so in-memory journals always
        start fresh; persistent backends override to read ``MAX(seq)``
        from storage so a restart does not collide on existing rows.
        """
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
        """
        Persist one prepared journal row.

        The only piece a backend must implement; the base class fills in
        seq, timestamp, and the log line before calling this so a backend
        only needs to know how to write a row, not how to sequence the
        journal.
        """
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

    def __init__(self, workspace: Workspace) -> None:
        """
        Bind the journal to a workspace.

        The ``Workspace`` selects which SQLite db is opened for every
        write via ``workspace.connect()``, so one ``SqliteJournal`` per
        workspace is the expected shape — sharing one across workspaces
        would route rows into the wrong db.
        """
        super().__init__()
        self.workspace = workspace

    def _store(
        self,
        task_id: str,
        seq: int,
        created_at: str,
        kind: str,
        payload: dict[str, Any],
    ) -> None:
        """
        Dispatch a row to the right table based on its kind.

        Transitions go to the structured ``pipeline_transitions`` table
        so columns stay queryable; lifecycle events go to the free-form
        ``pipeline_journal`` table where the payload shape is open.
        """
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
        """
        Write one transition row and mirror it onto the task event log.

        The mirrored task-event row is what powers operator-facing
        activity feeds, while the structured ``pipeline_transitions`` row
        is what analytics queries hit; both must land in the same
        commit so the two surfaces never disagree about what fired.
        """
        with self.workspace.connect() as connection:
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
                self.workspace,
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
        """
        Write a non-transition lifecycle row.

        Covers ``task_started`` / ``stop_requested`` / ``task_finished``;
        each row is also mirrored onto the task event log so activity
        replay sees the same lifecycle bookend whether it walks the
        journal or the events table.
        """
        with self.workspace.connect() as connection:
            connection.execute(
                """
                INSERT INTO pipeline_journal (task_id, seq, created_at, kind, payload)
                VALUES (?, ?, ?, ?, ?)
                """,
                (task_id, seq, created_at, kind, json.dumps(payload, sort_keys=True)),
            )
            append_task_event(
                self.workspace,
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
        """
        Resume per-task seq numbering across runner restarts.

        Queries ``MAX(seq)`` across both journal tables and returns the
        next free value. Without this, a restart would re-emit rows at
        seq 0 and collide with the existing rows from the previous
        process.
        """
        with self.workspace.connect() as connection:
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
        """
        Replay the structured transition rows for one task.

        Used by the diagnostics CLI and the recovery agent to
        reconstruct pipeline history; the recovery agent in particular
        relies on the from/to/event shape to detect repeated failure
        fingerprints across runs.
        """
        with self.workspace.connect() as connection:
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
        return _decode_transition_rows(rows)

    def load_lifecycle(self, task_id: str) -> list[dict[str, Any]]:
        """
        Replay the non-transition events for one task.

        Returns the started / stop_requested / finished bookends; paired
        with ``load_transitions`` to render a task's full timeline (the
        bookends frame the structured transition rows in between).
        """
        with self.workspace.connect() as connection:
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
        """
        Discard the prepared row.

        Tests and one-shot CLIs use ``NullJournal`` to keep the runner
        contract while skipping persistence — the runner still asks for
        a journal on every transition, but the row goes nowhere.
        """
        del task_id, seq, created_at, kind, payload
        return None

    def _log(self, kind: str, task_id: str, payload: dict[str, Any]) -> None:
        """
        Suppress the per-row log line as well.

        Without this override, lifecycle tests asserting on stderr would
        see runner activity even when persistence is disabled, and the
        "no journal" mode would not be truly silent.
        """
        del kind, task_id, payload
        return None
