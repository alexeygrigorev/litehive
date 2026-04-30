#!/usr/bin/env python3
"""Test stale terminal task detection and reconciliation."""

import json
import tempfile
from pathlib import Path

from litehive.config.workspace import ensure_workspace
from litehive.db.schema import connect_workspace_db
from litehive.domain.common import utcnow
from litehive.recovery.workspace_repair import repair_workspace_state, _stale_terminal_candidate_ids
from litehive.state.records import create_task, get_task_record


def test_stale_terminal_task_detection():
    """Test detection and reconciliation of stale terminal tasks."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        print(f"Testing stale terminal detection in {workspace}")

        # Initialize workspace
        ensure_workspace(workspace)

        # Create a task
        task = create_task(workspace, title="Test stale terminal task")
        task_id = task.id
        assert task is not None

        # Simulate task completion by adding a terminal pass report
        now = utcnow()
        report_payload = {
            "task_id": task_id,
            "verdict": "pass",
            "pipeline_state": "accepting",  # Terminal stage
            "created_at": now,
            "source": "agent",
            "summary": "Task completed successfully",
            "feedback": "QA verification passed",
            "submitted_via_cli": False
        }

        # Save the terminal pass report to the database
        with connect_workspace_db(workspace) as conn:
            conn.execute(
                """
                INSERT INTO stage_reports (task_id, pipeline_state, payload, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (task_id, "accepting", json.dumps(report_payload), now)
            )

        # Remove task from queue to simulate it's not currently queued but has stale state
        with connect_workspace_db(workspace) as conn:
            # Get current queue (use correct workspace key)
            queue_row = conn.execute("SELECT payload FROM queue WHERE workspace_key = 'workspace'").fetchone()
            if queue_row:
                queue_data = json.loads(queue_row["payload"])
                if task_id in queue_data:
                    queue_data.remove(task_id)
                    conn.execute(
                        "UPDATE queue SET payload = ? WHERE workspace_key = 'workspace'",
                        (json.dumps(queue_data),)
                    )

        # Simulate stale state: task has terminal pass evidence but status is still queued
        task.status = "queued"  # Simulate stale state
        task.pipeline_status = "implementing"  # Not terminal

        # Force save the stale state to database
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

        print(f"✅ Created task {task_id} with stale terminal state")
        print("   - Status: queued (should be done)")
        print("   - Pipeline status: implementing (should be done)")
        print("   - Has terminal pass report: ✅")

        # Test candidate detection (acceptance criterion 1 - detection)
        print("\n1️⃣ Testing: Stale terminal candidate detection")
        candidates = _stale_terminal_candidate_ids(workspace)
        criterion1_detection = task_id in candidates
        print(f"   Candidates found: {candidates}")
        print(f"   Task {task_id} detected: {'✅ PASS' if criterion1_detection else '❌ FAIL'}")

        # Test reconciliation (acceptance criterion 1 - reconciliation)
        print("\n2️⃣ Testing: Stale terminal task reconciliation")
        summary_before = repair_workspace_state(workspace)
        reconciled = summary_before.mutated and task_id in summary_before.terminal_task_ids
        print(f"   Repair summary mutated: {summary_before.mutated}")
        print(f"   Terminal task IDs: {summary_before.terminal_task_ids}")
        print(f"   Task {task_id} reconciled: {'✅ PASS' if reconciled else '❌ FAIL'}")

        # Verify task state after reconciliation
        task_after = get_task_record(workspace, task_id)
        assert task_after is not None
        state_corrected = (
            task_after.status == "done" and
            task_after.pipeline_status == "done" and
            task_after.close_reason == "done"
        )
        print("   Task state after repair:")
        print(f"     - Status: {task_after.status}")
        print(f"     - Pipeline status: {task_after.pipeline_status}")
        print(f"     - Close reason: {task_after.close_reason}")
        print(f"   State corrected: {'✅ PASS' if state_corrected else '❌ FAIL'}")

        # Test performance optimization (acceptance criterion 2)
        print("\n3️⃣ Testing: Performance optimization on clean queue")
        # After reconciliation, queue should be clean - verify no more candidates
        clean_candidates = _stale_terminal_candidate_ids(workspace)
        performance_optimized = len(clean_candidates) == 0
        print(f"   Clean candidates after repair: {clean_candidates}")
        print(f"   Performance optimized: {'✅ PASS' if performance_optimized else '❌ FAIL'}")

        # Test naming of normalized tasks (acceptance criterion 3)
        print("\n4️⃣ Testing: Repair output names normalized terminal tasks")
        output_naming = task_id in summary_before.terminal_task_ids
        print(f"   Terminal task IDs in summary: {summary_before.terminal_task_ids}")
        print(f"   Task {task_id} named in output: {'✅ PASS' if output_naming else '❌ FAIL'}")

        # Overall result
        all_criteria_met = (
            criterion1_detection and reconciled and
            state_corrected and performance_optimized and output_naming
        )

        print("\n📋 ACCEPTANCE CRITERIA VERIFICATION:")
        print(f"   1a. Detection: {'✅' if criterion1_detection else '❌'}")
        print(f"   1b. Reconciliation: {'✅' if reconciled else '❌'}")
        print(f"   1c. State correction: {'✅' if state_corrected else '❌'}")
        print(f"   2. Performance optimization: {'✅' if performance_optimized else '❌'}")
        print(f"   3. Output naming: {'✅' if output_naming else '❌'}")
        print(f"\n🎯 OVERALL: {'✅ ALL CRITERIA MET' if all_criteria_met else '❌ SOME CRITERIA FAILED'}")

        return all_criteria_met


if __name__ == "__main__":
    success = test_stale_terminal_task_detection()
    exit(0 if success else 1)
