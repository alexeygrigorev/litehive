# Litehive Workspace Context

Process profile: Generic

Describe this repository and how subagents should work in it.

## Project
- Purpose:
- Main package/module locations:
- Commands to know:

## Process overlay
- Source of truth: tasks and implementation state live under `.litehive/`.
- Task source of truth: issues or task records define scope; prompts and transcripts are supporting evidence.
- Orchestrator model: the local runner is the manager and owns stage routing.
- Routing model: routing stays deterministic and local; subagents execute assigned stages but do not self-route.
- Shared stages: grooming -> implementing -> testing -> accepting -> commit_to_git.
- Role model: orchestrator-as-manager: `planner` owns grooming, `reviewer` owns acceptance, both are PM-style product roles, `swe` implements, and `qa` verifies.
- TDD expectations: prefer test-first or test-tight changes and explain deviations.
- Verification discipline: verification should be explicit, focused, and independent enough to catch regressions.
- Acceptance flow: implementation must pass verification before acceptance.
- Commit and recovery: successful tasks checkpoint to git; rollback and recover should remain deterministic.

## Project overlay
- General software project workflow with deterministic local orchestration.
- Favor incremental, reviewable changes over broad refactors.
- Keep implementation, verification, and acceptance evidence explicit.

## Init scaffold
- Scaffold `.litehive/context.md` from the generic base process template.
- Layer the project profile summary, workspace overlay, and stage overlay onto that base.
- Treat process profiles as overlays on the shared contract rather than separate workflows.
- Keep the task/issue source of truth, verification commands, and recovery policy visible in the scaffold.

## Prompt scaffold
- Start from the shared process contract, then add repository context and task data.
- Combine the generic base prompt with the selected project overlay instead of replacing the base.
- Apply stage defaults first, then append any project-specific stage overlay for that step.
- Keep stage prompts explicit about role, verification expectations, and final report format.

## Stage prompt scaffolding

### grooming
Act as the planner: clarify the user problem, inspect the repo if needed, and produce a concrete execution plan.
Focus on scope clarification, acceptance criteria quality, decomposition, follow-up tasks, and PM sizing.
Do not make code changes in this stage.

### implementing
Implement the task in this repository.
Keep changes tightly scoped and complete the work needed for the acceptance criteria.

### testing
Validate the implementation.
Run focused checks or tests where possible and report failures precisely.
Only make minimal fixes if absolutely necessary.

### accepting
Act as the reviewer: validate the end-user outcome against the acceptance criteria and decide whether it should be accepted or sent back.
Be strict about regression detection, evidence quality, and final done versus not-done judgment.

## Development rules
- Keep changes scoped to the current task.
- Prefer targeted tests over broad test suites.
- Record assumptions clearly in the final report.

## Tool usage
- Use `uv run pytest -q` for the current smoke test suite.
- Update litehive task artifacts instead of inventing external state stores.
- If you add a new command or workflow, document it here for future runs.
