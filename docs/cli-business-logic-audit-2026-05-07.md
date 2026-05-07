# CLI Business Logic Audit - 2026-05-07

Scope: every direct Python module in `litehive/cli/` was inspected for
business logic that still lives in CLI handlers instead of a domain,
agent, task, lifecycle, observability, worktree, daemon, or container
service.

## Completed Extraction

- `litehive/cli/agent_dispatch.py`: moved the agent role command
  allowlist and blocked-command message into
  `litehive/agents/command_policy.py`. The CLI dispatcher now only
  applies the policy to raw argv and decides whether to route through
  the full CLI.

## Module Findings

- `litehive/cli/__init__.py`: empty package marker; no business logic.
- `litehive/cli/common.py`: Typer/click helper functions only; keep in
  CLI.
- `litehive/cli/parse.py`: CLI option parsing for repeated/comma
  values; acceptable boundary parsing.
- `litehive/cli/display.py`: small operator label helpers. It still
  reads task interruption status fields, but this is presentation
  logic.
- `litehive/cli/app.py`: root app composition and default "run next"
  dispatch. The `_run_next_task` helper still dequeues directly and
  should be considered when runner dispatch is centralized.
- `litehive/cli/agent_cli.py`: mostly thin after the agent report/task
  mutation/environment extractions. Remaining logic is command option
  parsing, env boundary access through `LitehiveEnvironment`, and
  service dispatch.
- `litehive/cli/agent_dispatch.py`: now policy-free after the
  extraction above. Remaining logic is raw argv routing for cold start.
- `litehive/cli/daemon_cli.py`: thin wrapper around runner/daemon
  helpers, plus workspace option normalization for status.
- `litehive/cli/engine.py`: engine status rendering remains in CLI.
  Runtime setting mutation is delegated to config/runtime services, and
  quota probing has been moved to `config.engine_quota`.
- `litehive/cli/pipeline_cli.py`: still constructs
  `SqlitePersistence` and `SqliteJournal` directly and renders rich
  state details inline. This is covered by C15, C16, and C17.
- `litehive/cli/pool.py`: still owns pool report data construction,
  task bucket filtering, stop-condition labels, no-useful-progress
  mapping, dictionary-shaped reports, and text rendering. This is
  covered by P1-P11 and should move into a pool/report domain service.
- `litehive/cli/queue_cli.py`: still owns some command policy branches
  such as `promote` deciding when to resume, `requeue` deciding when to
  recover completed work, and queue view filtering for resumable tasks.
  Core mutations already delegate to task services.
- `litehive/cli/runner.py`: still owns the largest amount of behavior:
  pool run loop decisions, dry-run selection rendering, dirty-git stop
  policy, consecutive failure handling, operator report submission, DB
  backup/restore guards, migration command orchestration, event-log
  rebuild output, and runtime settings audit rendering. These should be
  split into runner/pool, report submission, backup, and DB command
  services as later checklist items reach them.
- `litehive/cli/task_cli.py`: task creation and mutations delegate to
  task services, but list/show rendering still computes dependency
  labels, close/flag labels, filtering, and active-agent mutation
  routing branches inline. The agent-specific mutation path is partly
  duplicated with `agent_cli` and should continue moving to
  `agents.task_mutation`.
- `litehive/cli/task_debug_support.py`: evidence/debug renderer. It
  reads artifacts and SQLite-backed evidence directly for display; this
  is operator inspection logic, but DB/artifact lookups could be moved
  behind an evidence service if this area changes.
- `litehive/cli/task_logs_support.py`: log and subagent evidence
  presentation. It still resolves latest/follow task choices and picks
  artifacts inline; acceptable as CLI support today, but a future log
  evidence service would make this easier to test without print loops.
- `litehive/cli/workspace.py`: status rendering delegates heavily to
  observability services, but health still owns quota probing, daemon
  status tuple shaping, flagged/done filtering, and quota-health
  domain labels. Quota health overlaps with `config.engine_quota` and
  should be consolidated if touched again.
- `litehive/cli/worktree_cli.py`: mostly thin around
  `WorktreeService`. It still counts rescue result statuses in the CLI;
  those counts could become a rescue summary object, but the mutation
  and inspection behavior is already service-owned.

## Follow-Up Order

1. Continue with C15-C17 in `pipeline_cli.py`.
2. Then address P1-P11 in `pool.py`.
3. After pool cleanup, return to `runner.py`, `queue_cli.py`,
   `task_cli.py`, and `workspace.py` for narrower service extractions.
