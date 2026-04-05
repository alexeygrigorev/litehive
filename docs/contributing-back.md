# Contributing Back To Litehive

Litehive can file upstream Litehive work from another project without leaving the
current workspace. The protocol is local-first: the baseline contribution is a
task created in the Litehive workspace identified by `litehive_source_path`.

## Configure the upstream repo

In the external project workspace, point Litehive at the Litehive source repo:

```bash
litehive configure \
  \
  --litehive-source-path /abs/path/to/litehive
```

`litehive_source_path` should point at the Litehive repository root. The command
uses that path to locate or initialize the upstream Litehive workspace where new
upstream tasks will be created.

## File an upstream issue

Create an upstream Litehive task from the current project:

```bash
litehive issue \
  --upstream "engine timeout not working" \
  --type runtime_bug \
  --details "Observed during recovery while running project X." \
 
```

The created upstream task stores:

- source project name
- source workspace path
- source task id and stage when available
- contribution type
- Litehive source path used for the handoff

Supported contribution types:

- `runtime_bug`
- `missing_feature`
- `config_improvement`
- `prompt_improvement`
- `engine_adapter_fix`

These cover the required scenarios:

- bug discovered during task execution
- missing feature needed by the project
- config or prompt improvement based on real usage
- engine adapter fix needed

## Recovery-agent escalation

Recovery agents should classify whether a failure belongs to the current project
or to Litehive itself. When the problem is a Litehive bug, missing feature, or
workflow/config/prompt issue, the recovery path should file an upstream task
instead of only leaving a local note:

```bash
litehive issue \
  --upstream "Litehive crashed while recovering adapter task" \
  --type runtime_bug \
  --details "Include traceback, reproduction steps, and why this is a Litehive failure." \
  --source-role recovery \
  --source-stage implementing \
 
```

The recovery prompt now explicitly tells recovery agents to use this flow when
they detect a Litehive-side issue.

## Patch handoff

If the external project already knows the Litehive change it wants to propose,
prepare a branch in the Litehive repo and attach that branch to the upstream
task:

```bash
litehive issue \
  --upstream "Tune Codex timeout handling" \
  --type engine_adapter_fix \
  --patch-branch recover/codex-timeout-fix \
  --prepare-patch-branch \
  --details "Branch prepared for a candidate fix in the Litehive repo." \
 
```

This local-first handoff gives the Litehive workspace a durable task plus patch
metadata (`branch`, `base_ref`, and whether the branch was prepared). Operators
can continue with a normal Litehive task flow, or later open a hosted PR from
that branch if they want forge-level review.

## Operator visibility

`litehive status` and task summaries show upstream origin metadata so operators
can see which project triggered the contribution and trace back to the source
workspace/task. Upstream task records persist that metadata in the task YAML so
later QA and reviewer stages can verify the feedback loop end to end.
