# Configuration

Litehive reads configuration from two places:

1. Global config: `~/.config/litehive/config.yaml`
2. Workspace config: `.litehive/config.yaml`

Workspace values override global values. Task-level settings then override the
workspace for that task, and some commands such as `litehive run --engine ...`
can override the resolved engine for a single run.

## Creating Config

Initialize a workspace with defaults:

```bash
litehive configure
```

Seed common settings at creation time:

```bash
litehive configure \
  \
  --default-engine codex \
  --process-profile python \
  --default-retry-limit 3 \
  --claude-enabled \
  --claude-model claude-sonnet-4-20250514
```

## Core Keys

Common top-level fields in `.litehive/config.yaml`:

```yaml
default_engine: codex
recovery_engine: claude
process_profile: generic
litehive_source_path: /abs/path/to/litehive

codex_model: gpt-5.4-high
opencode_model: zai-coding-plan/glm-5.1
goz_model: glm-5-turbo
gemini_model: gemini-2.5-pro
copilot_model: gpt-5

claude_enabled: true
claude_model: claude-sonnet-4-20250514
claude_max_turns: 100

default_retry_limit: 3
default_stage_retry_limit: 2
auto_commit: true
```

What they do:

- `default_engine`: fallback engine when no task-specific or run-specific choice
  applies.
- `recovery_engine`: engine used for recovery and merge-resolution agents. If
  omitted, Litehive falls back to the task engine or workspace default engine.
- `process_profile`: prompt/process overlay used when scaffolding
  `.litehive/context.md`.
- `litehive_source_path`: path to the Litehive repository used for upstream
  issue filing and self-heal workflows.
- `*_model`: default model per adapter when that adapter supports model
  selection.
- `claude_enabled`: opt-in gate for the Claude adapter.
- `claude_max_turns`: guardrail to limit Claude CLI conversation length.
- `default_retry_limit`: workspace-level limit for rejections routed back from
  `testing` or `accepting`.
- `default_stage_retry_limit`: per-stage retry limit before Litehive escalates
  back to `grooming`.
- `auto_commit`: whether tasks create the final `commit_to_git` checkpoint by
  default.

## Engine Setup

Supported engines are:

- `codex`
- `opencode`
- `gemini`
- `copilot`
- `claude`
- `goz`

Set the workspace default engine:

```bash
litehive engine gemini
```

Set a task-specific engine:

```bash
litehive update T-0002 --engine opencode
```

Switch a task to a different engine mid-stream and record the reason:

```bash
litehive switch T-0002 gemini --reason "quota exhausted"
```

Model resolution is:

1. Explicit run override such as `litehive run --engine gemini --model ...`
2. Task-level `engine` and `model`
3. Task-type routing from `task_engine_routing`
4. Workspace `default_engine`
5. Adapter-specific default model field such as `opencode_model`

## Task Routing And Fallbacks

Litehive can route different task types to different engine orders:

```yaml
task_engine_routing:
  docs: [codex, gemini, opencode]
  review: [copilot, codex]
  refactor: [opencode, codex]
```

If an engine hits a limit or fails in a retryable way, Litehive walks the global
engine preference list to find the next available adapter:

```yaml
engine_preference: [codex, opencode, gemini, copilot, goz]
```

## Retry Policies

Litehive has two distinct retry layers:

1. Task/pipeline retries for rejected work.
2. Execution retries for transient adapter failures.

### Task And Stage Retry Limits

Per-task override at creation time:

```bash
litehive add "Stabilize flaky API test" \
  --goal "..." \
  --acceptance-criteria "..." \
  --retry-limit 5 \
 
```

Per-task override after creation:

```bash
litehive update T-0004 --retry-limit 5
litehive update T-0004 --retry-limit default
```

Behavior:

- `default_retry_limit` controls how many `testing` or `accepting` rejections a
  task can absorb before it is flagged as `retry_limit_exhausted`.
- `default_stage_retry_limit` controls how many times the same review stage can
  reject before Litehive escalates the task back to `grooming` instead of
  looping through implementation again.

### Execution Retry Policies

`execution_retry_policies` covers transient CLI failures such as timeouts,
network issues, or overloaded services:

```yaml
execution_retry_policies:
  codex:
    max_retries: 2
    backoff_seconds: 0.25
    backoff_multiplier: 2.0
    retry_on: [timeout, network, service]
  model_family:glm:
    max_retries: 3
    backoff_seconds: 0.5
    backoff_multiplier: 2.0
    retry_on: [timeout, service]
  external_cli:
    max_retries: 1
    backoff_seconds: 0.25
    backoff_multiplier: 1.0
    retry_on: [network]
```

Valid selectors are:

- an engine name such as `codex`
- `external_cli`
- `model_family:<family>`

Valid `retry_on` classifications are:

- `timeout`
- `network`
- `service`

## Hooks

Runner hooks execute around the implementation and acceptance boundaries.
Supported hook points are:

- `before_swe_implementation`
- `after_swe_implementation`
- `before_pm_acceptance`
- `after_pm_acceptance`

Each hook has a shell command and a `blocking` flag:

```yaml
runner_hooks:
  before_swe_implementation:
    - command: uv run python scripts/preflight.py
      blocking: true
  after_swe_implementation:
    - command: uv run pytest -q tests/test_workspace.py
      blocking: false
  before_pm_acceptance:
    - command: uv run ruff check .
      blocking: true
```

You can also add hooks at workspace creation time:

```bash
litehive configure \
  \
  --hook 'before_swe_implementation=blocking:uv run python scripts/preflight.py' \
  --hook 'after_pm_acceptance=nonblocking:uv run python scripts/notify.py'
```

## Pool Controls

Pool-level stop conditions can live in config so `litehive run --drain` and the
daemon stop predictably:

```yaml
pool_stop_on_failure: false
pool_max_tasks: 10
pool_stop_on_execution_limit: true
pool_quota_threshold: 2
pool_budget_threshold: 1
pool_stop_on_dirty_git: true
pool_usage_cap: 25
pool_cost_cap: 40
engine_usage_caps:
  codex: 10
  claude: 3
engine_budget_caps:
  claude: 9
engine_costs:
  claude: 3
  codex: 1
pool_selection_policy: dependency_aware
```

These values define when a draining run or daemon iteration should stop before
claiming more work.

## Subagent Resource Limits

Litehive can cap resource usage for subagent execution:

```yaml
subagent_resource_limits:
  enabled: true
  memory_mb: 8192
  cpu_count: 4
  process_limit: 512
```

This is especially relevant for heavier `rust` and `cpp` process profiles, where
the code applies profile-aware defaults.

## External Engine Sandboxing

Advanced deployments can run external engine CLIs inside a sandbox:

```yaml
external_engine_sandbox:
  enabled: true
  backend: docker
  runtime_binary: docker
  image: litehive-external-engine:latest
  default_network_mode: none
  default_workspace_mode: rw
```

Per-engine policies can selectively allow environment variables, network
settings, and mounted credentials.

## Agent Startup Guidance

You can add role-specific instructions that become part of agent prompts:

```yaml
agent_startup_guidance:
  all:
    - Start from the task record and latest reports.
  swe:
    - Prefer targeted file reads over broad repo scans.
  qa:
    - Verify with focused tests before broader commands.
  recovery:
    - Inspect recovery artifacts before changing code.
```

Valid keys are `all`, `planner`, `swe`, `qa`, `reviewer`, and `recovery`.

## Recommended Starting Point

For a normal single-repo setup, this is a reasonable first config:

```yaml
default_engine: codex
recovery_engine: codex
process_profile: generic
default_retry_limit: 3
default_stage_retry_limit: 2
auto_commit: true

runner_hooks:
  before_pm_acceptance:
    - command: uv run pytest -q
      blocking: true
```

Then tune routing, fallbacks, and retry behavior only after you have a few real
runs to learn from.
