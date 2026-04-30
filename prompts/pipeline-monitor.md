You are monitoring the litehive background runner. The runner is active (`litehive start` was called). Your job:

1. Check `litehive status` and `litehive pipeline journal <active_task_id>` every 3-5 minutes.
2. If a task is progressing normally (transitions growing, stages advancing), just report briefly and keep watching.
3. If a task is stuck in `recovering` or `failed`, investigate:
   - Read the journal for the triggering event
   - Check agent logs: `litehive task logs <task_id> --agent`
   - If it's a code bug you can fix, fix it directly, commit, push
   - If it needs a task reset, use `litehive pipeline reset <task_id>`
   - If it's a bigger issue, create a litehive task for it
4. If the background runner stopped, restart it with `litehive start`.
5. After fixing anything, keep monitoring — don't stop.

Key context:
- Read `docs/state-machine.md` for the maintained lifecycle model
- Rules table: `litehive/lifecycle/rules.py`
- Stages enum: `litehive/lifecycle/stages.py`
- Journal implementation: `litehive/lifecycle/journal.py`
- Agent CLI enforces role-based verdicts (non-recovery agents use pass/reject only)
- The after_implementing hook runs ruff + pytest on the full codebase
- `litehive pipeline journal <task_id>` shows the full state machine trace
- `litehive pipeline reset <task_id>` clears lifecycle state for a fresh start

Start by running `litehive status` to see what's active.
