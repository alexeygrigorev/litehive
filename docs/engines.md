# Engines

Litehive executes work by calling external coding-agent CLIs. Each supported
adapter translates a Litehive prompt into the engine's command-line interface,
captures output, parses stage reports, and records usage or limit signals.

## Supported Adapters

Current adapters:

- `codex`
- `opencode`
- `gemini`
- `copilot`
- `claude`
- `goz`

The adapter implementations live in `litehive/engines.py`.

## Default Models

Config defaults:

- `codex_model`: optional
- `opencode_model`: `zai-coding-plan/glm-5.1`
- `goz_model`: `glm-5-turbo`
- `gemini_model`: optional
- `copilot_model`: optional
- `claude_model`: `claude-sonnet-4-20250514`

Claude is intentionally opt-in:

```yaml
claude_enabled: true
claude_model: claude-sonnet-4-20250514
claude_max_turns: 100
```

## Choosing An Engine

Litehive resolves engine order in this precedence:

1. run override such as `litehive run --engine gemini`
2. task-level engine on the task record
3. `task_engine_routing` for the task type
4. workspace `default_engine`

Examples:

```bash
litehive engine codex
litehive update T-0005 --engine opencode
litehive run --engine gemini --model gemini-2.5-pro
```

## Task-Type Routing

A workspace can route different categories of work to different engines:

```yaml
task_engine_routing:
  adapter: [codex, opencode, gemini, copilot, goz]
  bugfix: [codex, opencode, copilot, gemini, goz]
  research: [gemini, codex, opencode, copilot, goz]
  review: [copilot, codex, opencode, gemini, goz]
  refactor: [opencode, codex, copilot, gemini, goz]
  docs: [codex, gemini, opencode, copilot, goz]
  intake: [opencode, codex, gemini, copilot, goz]
```

If a task does not have an explicit `task_type`, Litehive can infer one from the
task text using keywords such as `review`, `investigate`, `refactor`, `docs`,
and `fix`.

## Engine Preference

When an engine reaches quota or another execution limit, Litehive can retry the
stage on the next available engine in the global preference list:

```yaml
engine_preference: [codex, opencode, gemini, copilot, goz]
```

When the primary engine fails, Litehive walks this list in order, skipping the
failed engine and any frozen engines. This is separate from task retry policy.
Fallbacks happen inside an attempt to keep work moving when the current engine
is unavailable.

## Recovery Engine

Recovery and merge-resolution agents can use a dedicated engine:

```yaml
recovery_engine: claude
```

If `recovery_engine` is not set, Litehive falls back to the task engine or the
workspace default engine.

## Limit And Usage Detection

Litehive classifies common limit signals from engine output, including:

- usage limits
- quota exhaustion
- rate limits
- budget or credit exhaustion
- capacity issues

It also recognizes retryable transient failures in three classes:

- `timeout`
- `network`
- `service`

Those signals drive:

- execution retries
- fallback routing
- pool stop conditions such as `--stop-on-limit`
- engine monitoring artifacts

## Continuation Support

Some adapters expose resumable session or thread identifiers. Litehive extracts
continuation hints where the underlying CLI supports them:

- Codex: thread id
- OpenCode: session id
- Gemini: session id
- Claude: session id
- Copilot: no continuation id captured

This data is used for continuation handoffs during retries, interruptions, and
engine switches.

## Sandboxing External Engines

Advanced deployments can run external engines in a sandbox:

```yaml
external_engine_sandbox:
  enabled: true
  backend: docker
  runtime_binary: docker
  image: litehive-external-engine:latest
  default_network_mode: none
  default_workspace_mode: rw
```

Per-engine policies can define:

- network mode
- workspace mount mode
- allowed environment variables
- mounted credential files

This is most useful when you want tighter isolation between Litehive and the
external engine process.

## Adding A New Engine Adapter

Adding an engine is a code change, not just a config change. The usual steps are:

1. Add a new adapter class in `litehive/engines.py`.
2. Implement command construction for the external CLI.
3. Implement transcript rendering and stage-report parsing.
4. Add usage-limit and retryable-failure extraction if the engine exposes useful
   signals.
5. Register the adapter in the engine registry so it appears in `ENGINE_CHOICES`.
6. Add config defaults if the engine supports a model field or special guardrails.
7. Add or update integration coverage so the CLI adapter is exercised end to end.
8. Update documentation and any routing defaults that should include the new
   engine.

At minimum, a Litehive adapter must answer these questions cleanly:

- How is the prompt passed to the CLI?
- How does Litehive recover the assistant transcript?
- How does Litehive parse the agent's final stage report?
- How are limit, interruption, and transient-failure signals recognized?
- Does the engine support model override or continuation?

## Practical Recommendations

- Start with one stable default engine before introducing task-type routing.
- Enable Claude only if you actually intend to spend quota on it.
- Set `recovery_engine` intentionally if you want failures handled by a
  different adapter than normal execution.
- Use `litehive switch` instead of manually editing `task.yaml` when changing a
  task's engine mid-run.
