"""SQLite-backed runtime storage for workspace execution state."""

import json
import logging
import re
import sqlite3

from litehive.config.paths import workspace_path
from litehive.db.schema import consume_rebuilt_database_marker
from litehive.domain.common import PipelineStatus, TaskStatus, utcnow
from litehive.domain.runtime import TaskRuntime
from litehive.domain.task import TaskIntentRecord, TaskStateRecord, WorkspaceState
from litehive.tasks.audit import TaskAuditEntry, insert_task_audit_entries
from litehive.tasks.event_log import append_task_event, task_event_type_for_audit_action
from litehive.workspace import Workspace

logger = logging.getLogger(__name__)

_TASK_DIR_RE = re.compile(r"^T-(\d{4})-")
_TASK_SCOPED_TABLES = (
    "task_state",
    "task_intent",
    "task_journal",
    "task_activity",
    "stage_reports",
    "recovery_reports",
    "hook_artifacts",
    "subagent_sessions",
    "subagent_id_counters",
    "events",
    "worktrees",
    "pipeline_transitions",
    "pipeline_journal",
    "pipeline_task_state",
    "pipeline_sessions",
)


class RuntimeStore:
    """
    Small repository-style API over the workspace runtime database.

    Owns every direct write to the SQLite tables so the persist layer
    and call sites that wrap it can rely on consistent column projection,
    atomic transactions, and event-log emission instead of each caller
    inventing its own SQL.
    """

    def __init__(self, workspace: Workspace) -> None:
        """
        Pin the store to a workspace.

        Constructed via the ``runtime_store`` module factory so tests can
        monkey-patch a single symbol instead of every call site; binding
        the workspace once means the store does not have to look it up
        on every call.
        """
        self.workspace = workspace

    def bootstrap(self) -> None:
        """
        Initialise workspace rows and replay the event log after DB loss.

        Called on first connect to a workspace; if the SQLite task tables
        are empty but the JSONL event log has events, the replay
        rebuilds the DB from the log so a wiped or corrupt workspace
        heals itself instead of losing history.
        """
        with self.workspace.connect() as connection:
            self.create_workspace_state_rows(connection)
            if consume_rebuilt_database_marker(self.workspace.root):
                connection.commit()
            connection.commit()
        if self._should_rebuild_from_task_event_log():
            from litehive.tasks.event_log import rebuild_sqlite_from_task_event_log  # noqa: PLC0415

            rebuild_sqlite_from_task_event_log(self.workspace)

    def _should_rebuild_from_task_event_log(self) -> bool:
        """
        True when SQLite is empty but the event log has history.

        ``bootstrap`` uses this signal to decide whether to replay the
        log on the next workspace open; the combination is the
        unambiguous "DB was wiped, log survived" shape that triggers a
        rebuild rather than a no-op bootstrap.
        """
        from litehive.tasks.event_log import sqlite_task_tables_empty, task_event_log_has_events  # noqa: PLC0415

        return task_event_log_has_events(self.workspace) and sqlite_task_tables_empty(self.workspace)

    def load_workspace_state(self) -> WorkspaceState | None:
        """
        Reassemble pool + queue rows into a single ``WorkspaceState``.

        Stitches the two-table split (``pool_state`` for non-queue
        fields, ``queue`` for the ordered task list) back into one
        object so callers see a unified shape; returns ``None`` when
        the workspace has never been bootstrapped.
        """
        with self.workspace.connect() as connection:
            self.create_workspace_state_rows(connection)
            state_row = connection.execute(
                "SELECT payload FROM pool_state WHERE workspace_key = ?",
                ("workspace",),
            ).fetchone()
            queue_row = connection.execute(
                "SELECT payload FROM queue WHERE workspace_key = ?",
                ("workspace",),
            ).fetchone()
        if state_row is None or queue_row is None:
            return None
        payload = json.loads(state_row["payload"])
        payload["queue"] = json.loads(queue_row["payload"])
        return WorkspaceState(**payload)

    def load_workspace_state_read_only(self) -> WorkspaceState | None:
        """
        Read workspace state without acquiring the writer lock.

        Status snapshot rendering runs concurrently with the daemon and
        must not contend for the workspace DB; opens SQLite in
        ``?mode=ro`` and returns ``None`` when the DB file does not yet
        exist so a cold workspace renders as "no state" rather than
        raising.
        """
        db_path = workspace_path(self.workspace.root, "data.db")
        if not db_path.exists():
            return None
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as connection:
            connection.row_factory = sqlite3.Row
            state_row = connection.execute(
                "SELECT payload FROM pool_state WHERE workspace_key = ?",
                ("workspace",),
            ).fetchone()
            queue_row = connection.execute(
                "SELECT payload FROM queue WHERE workspace_key = ?",
                ("workspace",),
            ).fetchone()
        if state_row is None or queue_row is None:
            return None
        payload = json.loads(state_row["payload"])
        payload["queue"] = json.loads(queue_row["payload"])
        return WorkspaceState(**payload)

    def save_workspace_state(self, state: WorkspaceState) -> None:
        """
        Persist the pool/queue snapshot and append a replay event in one tx.

        Used by the persist layer when only workspace-level fields
        (counters, queue) changed and no per-task records need to be
        written; multi-record writes go through
        ``save_runtime_transaction`` so the per-task and workspace
        writes land atomically.
        """
        with self.workspace.connect() as connection:
            self._save_workspace_state(connection, state)
            self._append_workspace_state_event(state)
            connection.commit()

    def load_task_state(self, task_id: str) -> TaskStateRecord | None:
        """
        Hydrate a task's mutable runtime/lifecycle record.

        The half that changes as the task runs (status, retry counters,
        active subagent, current stage); paired with ``load_task_intent``
        which carries the immutable definition.
        """
        with self.workspace.connect() as connection:
            row = connection.execute(
                "SELECT payload FROM task_state WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(row["payload"])
        return TaskStateRecord(**payload)

    def load_task_intent(self, task_id: str) -> TaskIntentRecord | None:
        """
        Read the immutable task definition (goal, criteria, plan).

        The half set at creation time and rarely mutated; paired with
        ``load_task_state`` which carries the runtime/lifecycle row.
        Returns ``None`` for unknown task ids so callers can branch
        rather than handle an exception.
        """
        with self.workspace.connect() as connection:
            row = connection.execute(
                "SELECT payload FROM task_intent WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(row["payload"])
        return TaskIntentRecord.model_validate(payload)

    def list_task_intents(self) -> list[TaskIntentRecord]:
        """
        Enumerate every known task intent in id order.

        Feeds list/status surfaces and the queue rebuilder; the in-order
        scan keeps a deterministic baseline for callers that compose
        their own ordering on top.
        """
        with self.workspace.connect() as connection:
            rows = connection.execute(
                "SELECT payload FROM task_intent ORDER BY task_id ASC",
            ).fetchall()
        intents: list[TaskIntentRecord] = []
        for row in rows:
            payload = json.loads(row["payload"])
            intents.append(TaskIntentRecord.model_validate(payload))
        return intents

    def save_task_intent(self, task_id: str, intent: TaskIntentRecord) -> None:
        """
        Persist a task-definition row plus a replay event when only intent changes.

        Used on task creation and on ``litehive update`` — anywhere a
        full multi-record transaction is overkill; multi-record changes
        go through ``save_runtime_transaction`` instead so workspace
        and per-task writes land atomically.
        """
        with self.workspace.connect() as connection:
            self._save_task_intent(connection, task_id, intent)
            append_task_event(
                self.workspace,
                event_type="task_intent_saved",
                task_id=task_id,
                payload={"task_intent": intent.model_dump(mode="json")},
            )
            connection.commit()

    def save_task_state(self, task_id: str, state: TaskStateRecord) -> None:
        """
        Persist a task's runtime/lifecycle row plus a replay event.

        The single-record convenience write; multi-record stage
        transitions go through ``save_runtime_transaction`` so
        workspace, intent, state, and audit entries land atomically
        rather than as a sequence of independent writes.
        """
        with self.workspace.connect() as connection:
            self._save_task_state(connection, task_id, state)
            append_task_event(
                self.workspace,
                event_type="task_state_saved",
                task_id=task_id,
                payload={"task_state": state.model_dump(mode="json")},
            )
            connection.commit()

    def save_runtime_transaction(
        self,
        task_intents: dict[str, TaskIntentRecord] | None = None,
        task_states: dict[str, TaskStateRecord] | None = None,
        workspace_state: WorkspaceState | None = None,
        task_journal_messages: dict[str, str] | None = None,
        audit_entries: list[TaskAuditEntry] | None = None,
    ) -> None:
        """
        Atomically commit a stage transition.

        Writes the workspace, per-task records, journal, and audit
        entries in one transaction — every record lands together or
        none does, so a crash mid-write cannot leave queue and
        ``task_state`` disagreeing about who owns what. The single
        write entry point for the queue mutation and locking layers.
        """
        with self.workspace.connect() as connection:
            if workspace_state is not None:
                self._save_workspace_state(connection, workspace_state)
            for task_id, intent in (task_intents or {}).items():
                self._save_task_intent(connection, task_id, intent)
            for task_id, state in (task_states or {}).items():
                self._save_task_state(connection, task_id, state)
            task_journal_entries: dict[str, list[dict[str, object]]] = {}
            for task_id, message in (task_journal_messages or {}).items():
                entry = self._append_task_journal(connection, task_id, message)
                task_journal_entries.setdefault(task_id, []).append(entry)
            insert_task_audit_entries(connection, audit_entries or [])
            self._append_runtime_transaction_events(
                task_intents=task_intents or {},
                task_states=task_states or {},
                workspace_state=workspace_state,
                task_journal_entries=task_journal_entries,
                audit_entries=audit_entries or [],
            )
            connection.commit()

    def delete_task_records(
        self,
        task_id: str,
        audit_entries: list[TaskAuditEntry] | None = None,
    ) -> None:
        """
        Purge every per-task row across all task-scoped tables.

        Called by the records layer when an operator deletes a task;
        the audit entries (if any) are the only trace of why the rows
        are gone, so they land in the same transaction as the deletes
        and the matching event-log entries.
        """
        with self.workspace.connect() as connection:
            for table_name in _TASK_SCOPED_TABLES:
                connection.execute(f"DELETE FROM {table_name} WHERE task_id = ?", (task_id,))
            insert_task_audit_entries(connection, audit_entries or [])
            if audit_entries:
                for entry in audit_entries:
                    append_task_event(
                        self.workspace,
                        event_type=task_event_type_for_audit_action(entry.action),
                        task_id=entry.task_id,
                        payload={"audit_entry": entry.model_dump(mode="json")},
                    )
            else:
                append_task_event(
                    self.workspace,
                    event_type="task_deleted",
                    task_id=task_id,
                    payload={},
                )
            connection.commit()

    def _append_workspace_state_event(self, state: WorkspaceState) -> None:
        """
        Emit a ``workspace_state_saved`` event so replay can rebuild the snapshot.

        Without the event the JSONL log would carry per-task changes but
        no record of the workspace pool/queue, and a rebuild would lose
        every queue ordering and counter the workspace had accumulated.
        """
        append_task_event(
            self.workspace,
            event_type="workspace_state_saved",
            task_id=None,
            payload={"workspace_state": state.model_dump(mode="json")},
        )

    def _append_runtime_transaction_events(
        self,
        task_intents: dict[str, TaskIntentRecord],
        task_states: dict[str, TaskStateRecord],
        workspace_state: WorkspaceState | None,
        task_journal_entries: dict[str, list[dict[str, object]]],
        audit_entries: list[TaskAuditEntry],
    ) -> None:
        """
        Write the replay-event tail of ``save_runtime_transaction``.

        Each touched task gets exactly one event carrying the full diff;
        audit-driven events take priority; a workspace-only mutation
        still emits one ``workspace_state_saved`` row when no per-task
        event covers it. The dedupe is what stops a single transition
        from generating multiple competing events for the same task.
        """
        logged_task_ids: set[str] = set()
        if workspace_state is None:
            workspace_payload = None
        else:
            workspace_payload = workspace_state.model_dump(mode="json")

        def payload_for_task(task_id: str) -> dict[str, object]:
            """
            Build the per-task event payload for this transaction.

            Bundles every record touching ``task_id`` (intent, state,
            journal, workspace) so a replay sees the same atomic
            snapshot the original write produced; without bundling, a
            replay would have to stitch together separate events to
            reconstruct one transition.
            """
            payload: dict[str, object] = {}
            if task_id in task_intents:
                payload["task_intent"] = task_intents[task_id].model_dump(mode="json")
            if task_id in task_states:
                payload["task_state"] = task_states[task_id].model_dump(mode="json")
            if task_id in task_journal_entries:
                payload["task_journal"] = task_journal_entries[task_id]
            if workspace_payload is not None:
                payload["workspace_state"] = workspace_payload
            return payload

        for entry in audit_entries:
            payload = payload_for_task(entry.task_id)
            payload["audit_entry"] = entry.model_dump(mode="json")
            append_task_event(
                self.workspace,
                event_type=task_event_type_for_audit_action(entry.action),
                task_id=entry.task_id,
                payload=payload,
            )
            logged_task_ids.add(entry.task_id)

        for task_id in sorted((set(task_intents) | set(task_states)) - logged_task_ids):
            append_task_event(
                self.workspace,
                event_type="task_state_saved",
                task_id=task_id,
                payload=payload_for_task(task_id),
            )
            logged_task_ids.add(task_id)

        for task_id in sorted(set(task_journal_entries) - logged_task_ids):
            append_task_event(
                self.workspace,
                event_type="task_journal_recorded",
                task_id=task_id,
                payload=payload_for_task(task_id),
            )
            logged_task_ids.add(task_id)

        if workspace_state is not None and not logged_task_ids:
            self._append_workspace_state_event(workspace_state)

    def _save_workspace_state(self, connection: sqlite3.Connection, state: WorkspaceState) -> None:
        """
        Write the ``pool_state`` and ``queue`` rows from a ``WorkspaceState``.

        The inverse of ``load_workspace_state``; splits the queue out of
        the payload because pool and queue are stored as separate rows,
        so a hot queue update does not have to rewrite the (larger)
        workspace pool blob.
        """
        now = utcnow()
        payload = state.model_dump(mode="json")
        queue_payload = json.dumps(payload.pop("queue"), sort_keys=True)
        connection.execute(
            """
            INSERT INTO pool_state (workspace_key, payload, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(workspace_key) DO UPDATE SET
                payload = excluded.payload,
                updated_at = excluded.updated_at
            """,
            ("workspace", json.dumps(payload, sort_keys=True), now),
        )
        connection.execute(
            """
            INSERT INTO queue (workspace_key, payload, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(workspace_key) DO UPDATE SET
                payload = excluded.payload,
                updated_at = excluded.updated_at
            """,
            ("workspace", queue_payload, now),
        )

    def load_task_runtime(self, task_id: str) -> TaskRuntime | None:
        """
        Project just the ``TaskRuntime`` sub-object out of the task_state row.

        Convenience for runtime callers that only need the pipeline/
        execution sub-tree; saves them from re-deriving it from the full
        ``TaskStateRecord`` every call.
        """
        state = self.load_task_state(task_id)
        if state is None:
            return None
        return state.runtime

    def save_task_runtime(self, task_id: str, runtime: TaskRuntime) -> None:
        """
        Replace the runtime block on a task row, preserving lifecycle fields.

        Pipeline-runner call sites only have a ``TaskRuntime`` in hand;
        this helper fuses it with the existing ``TaskStateRecord`` (or a
        fresh one if the task is new) so callers don't have to reload
        the full record just to update the runtime sub-tree.
        """
        state = self.load_task_state(task_id) or TaskStateRecord()
        state.runtime = runtime.model_copy(deep=True)
        state.updated_at = runtime.pipeline.updated_at or state.updated_at or utcnow()
        self.save_task_state(task_id, state)

    def _save_task_state(
        self,
        connection: sqlite3.Connection,
        task_id: str,
        state: TaskStateRecord,
    ) -> None:
        """
        Upsert ``task_state`` and mirror status onto the ``task_intent`` columns.

        The mirrored columns let queries filter by status without
        parsing the JSON blob; they MUST be rewritten on every state
        change or the queue selector will see stale status and pick a
        task that has already moved on.
        """
        now = state.updated_at or state.runtime.pipeline.updated_at or utcnow()
        state = state.model_copy(deep=True)
        state.updated_at = now
        if state.runtime.pipeline.updated_at is None:
            state.runtime.pipeline.updated_at = now
        payload = state.model_dump(mode="json")
        payload["updated_at"] = now
        connection.execute(
            """
            INSERT INTO task_state (task_id, payload, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
                payload = excluded.payload,
                updated_at = excluded.updated_at
            """,
            (task_id, json.dumps(payload, sort_keys=True), now),
        )
        connection.execute(
            """
            UPDATE task_intent
            SET lifecycle_status = ?, pipeline_status = ?
            WHERE task_id = ?
            """,
            (state.status.value, state.pipeline_status.value, task_id),
        )

    def _save_task_intent(
        self,
        connection: sqlite3.Connection,
        task_id: str,
        intent: TaskIntentRecord,
    ) -> None:
        """
        Upsert ``task_intent`` with denormalised columns alongside the JSON payload.

        The denormalised columns (slug, title, priority, plan_json, ...)
        exist so list/filter queries avoid full JSON parsing; on
        conflict the payload and columns are rewritten while
        lifecycle/pipeline status come from the live ``task_state`` row
        to avoid clobbering in-flight transitions.
        """
        now = utcnow()
        payload = intent.model_dump(mode="json")
        existing_state = _load_task_state_for_intent_columns(connection, task_id)
        column_values = _task_intent_column_values(intent, existing_state)
        connection.execute(
            """
            INSERT INTO task_intent (
                task_id,
                payload,
                updated_at,
                slug,
                title,
                created_at,
                priority,
                goal,
                acceptance_criteria_json,
                constraints_json,
                plan_json,
                dependencies_json,
                provenance_json,
                lifecycle_status,
                pipeline_status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
                payload = excluded.payload,
                updated_at = excluded.updated_at,
                slug = excluded.slug,
                title = excluded.title,
                created_at = excluded.created_at,
                priority = excluded.priority,
                goal = excluded.goal,
                acceptance_criteria_json = excluded.acceptance_criteria_json,
                constraints_json = excluded.constraints_json,
                plan_json = excluded.plan_json,
                dependencies_json = excluded.dependencies_json,
                provenance_json = excluded.provenance_json
            """,
            (
                task_id,
                json.dumps(payload, sort_keys=True),
                now,
                column_values["slug"],
                column_values["title"],
                column_values["created_at"],
                column_values["priority"],
                column_values["goal"],
                column_values["acceptance_criteria_json"],
                column_values["constraints_json"],
                column_values["plan_json"],
                column_values["dependencies_json"],
                column_values["provenance_json"],
                column_values["lifecycle_status"],
                column_values["pipeline_status"],
            ),
        )

    def append_task_journal(self, task_id: str, message: str) -> None:
        """
        Append a free-form journal line to a task.

        Used by ``litehive journal`` and stage commentary; each entry is
        auto-numbered so concurrent appends do not collide on
        ``entry_index``, and a replay event is emitted in the same
        transaction for the JSONL log.
        """
        with self.workspace.connect() as connection:
            entry = self._append_task_journal(connection, task_id, message)
            append_task_event(
                self.workspace,
                event_type="task_journal_recorded",
                task_id=task_id,
                payload={"task_journal": [entry]},
            )
            connection.commit()

    def _append_task_journal(
        self,
        connection: sqlite3.Connection,
        task_id: str,
        message: str,
    ) -> dict[str, object]:
        """
        Insert one ``task_journal`` row inside an existing transaction.

        Returns the entry for replay events; shared by
        ``append_task_journal`` (single-record write) and
        ``save_runtime_transaction`` (bulk transition) so both paths
        emit identical replay payloads.
        """
        created_at = utcnow()
        row = connection.execute(
            "SELECT COALESCE(MAX(entry_index) + 1, 0) AS next_index FROM task_journal WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            entry_index = 0
        else:
            entry_index = int(row["next_index"])
        metadata = "{}"
        connection.execute(
            """
            INSERT INTO task_journal (task_id, entry_index, created_at, message, metadata)
            VALUES (?, ?, ?, ?, ?)
            """,
            (task_id, entry_index, created_at, message, metadata),
        )
        return {
            "task_id": task_id,
            "entry_index": entry_index,
            "created_at": created_at,
            "message": message,
            "metadata": {},
        }

    def save_process_state(
        self,
        process_key: str,
        status: str,
        payload: dict[str, object],
    ) -> None:
        """
        Record a long-lived process row with PID and heartbeat for liveness checks.

        Called by the process-lock layer when a daemon claims its slot
        and by the daemon registry to publish heartbeats; the
        denormalised pid/heartbeat columns let liveness probes avoid
        full JSON parsing on every check.
        """
        now = utcnow()
        with self.workspace.connect() as connection:
            connection.execute(
                """
                INSERT INTO runtime_process_state (
                    process_key,
                    status,
                    pid,
                    workspace,
                    command,
                    active_task_id,
                    log_dir,
                    started_at,
                    heartbeat_at,
                    payload,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(process_key) DO UPDATE SET
                    status = excluded.status,
                    pid = excluded.pid,
                    workspace = excluded.workspace,
                    command = excluded.command,
                    active_task_id = excluded.active_task_id,
                    log_dir = excluded.log_dir,
                    started_at = excluded.started_at,
                    heartbeat_at = excluded.heartbeat_at,
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (
                    process_key,
                    status,
                    _optional_int(payload.get("pid")),
                    _optional_str(payload.get("workspace")),
                    _optional_str(payload.get("command")),
                    _optional_str(payload.get("active_task_id")),
                    _optional_str(payload.get("log_dir")),
                    _optional_str(payload.get("started_at")),
                    _optional_str(payload.get("heartbeat_at")),
                    json.dumps(payload, sort_keys=True),
                    now,
                ),
            )
            connection.commit()

    def clear_process_state(self, process_key: str) -> None:
        """
        Remove the process registry row on clean shutdown.

        Frees the named slot so the next daemon/runner start sees an
        empty registry rather than a phantom owner; ``ProcessLockManager``
        uses this on release so dashboards stop reporting a stale identity.
        """
        with self.workspace.connect() as connection:
            connection.execute("DELETE FROM runtime_process_state WHERE process_key = ?", (process_key,))
            connection.commit()

    def load_process_state(self, process_key: str) -> dict[str, object] | None:
        """
        Return the registered process row merged with its JSON payload.

        Used by liveness checks; the merge fills any missing JSON keys
        from the denormalised columns so older payloads written before
        a column was added still surface PID/heartbeat to the caller
        without requiring a one-shot migration.
        """
        with self.workspace.connect() as connection:
            row = connection.execute(
                """
                SELECT
                    process_key,
                    status,
                    pid,
                    workspace,
                    command,
                    active_task_id,
                    log_dir,
                    started_at,
                    heartbeat_at,
                    payload
                FROM runtime_process_state
                WHERE process_key = ?
                """,
                (process_key,),
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(str(row["payload"]))
        if not isinstance(payload, dict):
            return None
        for key in (
            "process_key",
            "status",
            "pid",
            "workspace",
            "command",
            "active_task_id",
            "log_dir",
            "started_at",
            "heartbeat_at",
        ):
            if payload.get(key) is None and row[key] is not None:
                payload[key] = row[key]
        return payload

    def highest_task_number(self) -> int:
        """
        Scan ``task_intent`` and ``task_state`` for the largest ``T-NNNN`` prefix.

        Called by the records layer when a workspace is rebooted:
        ``WorkspaceState.next_task_number`` must not collide with an
        existing T-id, so it is recomputed from the actual rows rather
        than trusted across restarts.
        """
        task_ids: set[str] = set()
        with self.workspace.connect() as connection:
            for table_name in ("task_intent", "task_state"):
                rows = connection.execute(f"SELECT task_id FROM {table_name}").fetchall()
                task_ids.update(str(row["task_id"]) for row in rows)
        highest = 0
        for task_id in task_ids:
            match = _TASK_DIR_RE.match(f"{task_id}-")
            if match is None:
                continue
            highest = max(highest, int(match.group(1)))
        return highest

    @staticmethod
    def create_workspace_state_rows(connection: sqlite3.Connection) -> None:
        """
        Seed the singleton ``pool_state`` and ``queue`` rows on first connect.

        ``INSERT OR IGNORE`` makes it safe to call on every connect:
        the bootstrap path and ``load_workspace_state`` both invoke
        this so a freshly created DB behaves the same as one that has
        been running for weeks.
        """
        now = utcnow()
        connection.execute(
            """
            INSERT OR IGNORE INTO pool_state (workspace_key, payload, updated_at)
            VALUES (?, ?, ?)
            """,
            (
                "workspace",
                json.dumps(
                    WorkspaceState().model_dump(mode="json", exclude={"queue"}),
                    sort_keys=True,
                ),
                now,
            ),
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO queue (workspace_key, payload, updated_at)
            VALUES (?, ?, ?)
            """,
            ("workspace", json.dumps([], sort_keys=True), now),
        )
        connection.commit()


def runtime_store_for_workspace(workspace: Workspace) -> RuntimeStore:
    """
    Module-level factory for ``RuntimeStore`` from an injected workspace.

    Used everywhere instead of direct construction so tests can
    monkey-patch a single symbol; without the factory each call site
    would have to be patched independently when redirecting the store.
    """
    return RuntimeStore(workspace)


def _load_task_state_for_intent_columns(
    connection: sqlite3.Connection,
    task_id: str,
) -> TaskStateRecord | None:
    """
    Read just enough of the live ``task_state`` row to compute mirrored columns.

    ``_save_task_intent`` uses this so an intent rewrite does not
    stomp the in-flight status from a concurrent state transition;
    without the lookup, a metadata edit would clobber the indexed
    status with the stale value the caller had at hand.
    """
    row = connection.execute("SELECT payload FROM task_state WHERE task_id = ?", (task_id,)).fetchone()
    if row is None:
        return None
    try:
        payload = json.loads(str(row["payload"]))
        return TaskStateRecord.model_validate(payload)
    except (json.JSONDecodeError, TypeError, ValueError):
        logger.warning("Ignoring invalid task_state row while saving task_intent columns for %s", task_id)
        return None


def _task_intent_column_values(
    intent: TaskIntentRecord,
    state: TaskStateRecord | None = None,
) -> dict[str, str]:
    """
    Project a ``TaskIntentRecord`` onto the flat denormalised columns.

    The single producer of those column values so list/filter queries
    see the same shape regardless of which call path wrote the row;
    the optional live state is folded in so the mirrored status columns
    reflect whatever ``task_state`` says at write time.
    """
    if intent.created_from is None:
        provenance_payload: dict = {}
    else:
        provenance_payload = intent.created_from.model_dump(mode="json")
    if state is None:
        lifecycle_status = TaskStatus.QUEUED.value
        pipeline_status = PipelineStatus.BACKLOG.value
    else:
        lifecycle_status = state.status.value
        pipeline_status = state.pipeline_status.value
    return {
        "slug": intent.slug,
        "title": intent.title,
        "created_at": intent.created_at,
        "priority": intent.priority,
        "goal": intent.goal,
        "acceptance_criteria_json": json.dumps(intent.acceptance_criteria, sort_keys=True),
        "constraints_json": json.dumps(intent.constraints, sort_keys=True),
        "plan_json": json.dumps(intent.plan, sort_keys=True),
        "dependencies_json": json.dumps(intent.depends_on, sort_keys=True),
        "provenance_json": json.dumps(provenance_payload, sort_keys=True),
        "lifecycle_status": lifecycle_status,
        "pipeline_status": pipeline_status,
    }


def _optional_str(value: object) -> str | None:
    """
    Coerce a ``runtime_process_state`` payload field to ``str``.

    Preserves ``None`` so the nullable denormalised columns on that
    table do not get the literal text ``"None"``; downstream filters
    that test ``IS NULL`` would otherwise miss real entries.
    """
    if value is None:
        return None
    return str(value)


def _optional_int(value: object) -> int | None:
    """
    Coerce a ``runtime_process_state`` payload field to ``int``.

    Matches ``_optional_str`` for the pid column so a malformed payload
    entry yields ``NULL`` rather than blowing up the daemon registry
    write; the catch-all ``except`` is what makes a partially-corrupt
    payload survive without the caller seeing it.
    """
    if value is None:
        return None
    if not isinstance(value, (int, str, bytes, float)):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
