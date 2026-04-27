#!/usr/bin/env python3
"""Debug repair functionality."""

import json
import tempfile
from pathlib import Path

from litehive.config.workspace import ensure_workspace
from litehive.db.schema import connect_workspace_db
from litehive.domain.common import utcnow
from litehive.recovery.workspace_repair import _normalize_stale_terminal_tasks, _stale_terminal_candidate_ids
from litehive.state.records import create_task, get_task_record
from litehive.tasks.reports import latest_stage_report
from litehive.state.persist import load_state


def debug_repair():
    """Debug why repair isn't working."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        print(f"Debugging repair in {workspace}")

        # Initialize workspace
        ensure_workspace(workspace)

        # Create a task
        task = create_task(workspace, title="Debug task")
        task_id = task.id
        print(f"Created task: {task_id}")

        # Add terminal pass report
        now = utcnow()
        report_payload = {
            "task_id": task_id,
            "verdict": "pass",
            "pipeline_state": "accepting",
            "created_at": now,
            "source": "agent",
            "summary": "Task completed successfully",
            "feedback": "QA verification passed",
            "submitted_via_cli": False
        }

        with connect_workspace_db(workspace) as conn:
            conn.execute(
                """
                INSERT INTO stage_reports (task_id, pipeline_state, payload, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (task_id, "accepting", json.dumps(report_payload), now)
            )
            print(f"Added terminal pass report for {task_id}")

        # Update task to stale state
        with connect_workspace_db(workspace) as conn:
            conn.execute(
                """
                UPDATE task_state
                SET payload = json_replace(
                    json_replace(payload, '$.status', 'queued'),
                    '$.pipeline_status', 'implementing'
                )
                WHERE task_id = ?
                """,
                (task_id,)
            )
            print(f"Updated {task_id} to stale state")

        # Check and fix queue removal
        with connect_workspace_db(workspace) as conn:
            # Check what's in the queue table
            all_queue_rows = conn.execute("SELECT workspace_key, payload FROM queue").fetchall()
            print(f"All queue rows: {[(row['workspace_key'], row['payload']) for row in all_queue_rows]}")

            for row in all_queue_rows:
                workspace_key = row['workspace_key']
                queue_data = json.loads(row["payload"])
                print(f"Workspace key '{workspace_key}', queue: {queue_data}")
                if task_id in queue_data:
                    queue_data.remove(task_id)
                    conn.execute(
                        "UPDATE queue SET payload = ? WHERE workspace_key = ?",
                        (json.dumps(queue_data), workspace_key)
                    )
                    print(f"Removed {task_id} from queue for workspace_key '{workspace_key}'")

        # Check detection
        candidates = _stale_terminal_candidate_ids(workspace)
        print(f"\nCandidates detected: {candidates}")

        # Check latest stage report
        task_record = get_task_record(workspace, task_id)
        latest_report = latest_stage_report(workspace, task_record)
        print(f"Latest report: {latest_report}")
        if latest_report:
            print(f"  - Verdict: {latest_report.verdict}")
            print(f"  - Pipeline state: {latest_report.pipeline_state}")

        # Check repair conditions manually
        print(f"\nDebugging repair conditions...")

        # Load state to check queue
        state = load_state(workspace, bootstrap=False)
        print(f"State active_task_id: {state.active_task_id}")
        print(f"State queue: {state.queue}")
        print(f"Task {task_id} in queue: {task_id in state.queue}")

        # Check terminal repair stages
        from litehive.recovery.workspace_repair import _TERMINAL_REPAIR_STAGES
        print(f"Terminal repair stages: {_TERMINAL_REPAIR_STAGES}")
        print(f"Report pipeline_state in terminal stages: {latest_report.pipeline_state in _TERMINAL_REPAIR_STAGES}")

        # Try repair
        print(f"\nAttempting repair...")
        result = _normalize_stale_terminal_tasks(workspace)
        print(f"Repair result: {result}")


if __name__ == "__main__":
    debug_repair()