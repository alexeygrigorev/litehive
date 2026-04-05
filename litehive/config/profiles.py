"""Process profile definitions and rendering helpers."""

from copy import deepcopy
from typing import Any


def _shared_stage_sequence() -> list[str]:
    return ["grooming", "implementing", "testing", "accepting", "commit_to_git"]


def _shared_stage_text(stages: list[str]) -> str:
    return " -> ".join(stages) + "."


SHARED_PROCESS_PROFILE: dict[str, Any] = {
    "label": "Generic",
    "summary": "General software project workflow with deterministic local orchestration.",
    "source_of_truth": "tasks and implementation state live under `.litehive/`.",
    "task_source_of_truth": "issues or task records define scope; prompts and transcripts are supporting evidence.",
    "orchestrator_model": "the local runner is the manager and owns stage routing.",
    "routing_model": "routing stays deterministic and local; subagents execute assigned stages but do not self-route.",
    "shared_stages": _shared_stage_sequence(),
    "role_model": (
        "orchestrator-as-manager: `planner` owns grooming, `reviewer` owns acceptance, "
        "both are PM-style product roles, `swe` implements, and `qa` verifies."
    ),
    "tdd_expectations": "prefer test-first or test-tight changes and explain deviations.",
    "verification_discipline": "verification should be explicit, focused, and independent enough to catch regressions.",
    "acceptance_flow": "implementation must pass verification before acceptance.",
    "commit_recovery": (
        "successful tasks checkpoint to git; rollback and recover should remain deterministic."
    ),
    "prompt_scaffold": [
        "- Start from the shared process contract, then add repository context and task data.",
        "- Combine the generic base prompt with the selected project overlay instead of replacing the base.",
        "- Apply stage defaults first, then append any project-specific stage overlay for that step.",
        "- Keep stage prompts explicit about role, verification expectations, and final report format.",
    ],
    "init_scaffold": [
        "- Scaffold `.litehive/context.md` from the generic base process template.",
        "- Layer the project profile summary, workspace overlay, and stage overlay onto that base.",
        "- Treat process profiles as overlays on the shared contract rather than separate workflows.",
        "- Keep the task/issue source of truth, verification commands, and recovery policy visible in the scaffold.",
    ],
    "development_rules": [
        "- Keep changes scoped to the current task.",
        "- Prefer targeted tests over broad test suites.",
        "- Record assumptions clearly in the final report.",
    ],
    "tool_usage": [
        "- Use `uv run pytest -q` for the current smoke test suite.",
        "- Update litehive task artifacts instead of inventing external state stores.",
        "- If you add a new command or workflow, document it here for future runs.",
    ],
    "workspace_overlay": [
        "- Favor incremental, reviewable changes over broad refactors.",
        "- Keep implementation, verification, and acceptance evidence explicit.",
    ],
    "stage_instructions": {
        "grooming": [
            "Act as the planner: clarify the user problem, inspect the repo if needed, and produce a concrete execution plan.",
            "Focus on scope clarification, acceptance criteria quality, decomposition, follow-up tasks, and PM sizing.",
            "Do not make code changes in this stage.",
        ],
        "implementing": [
            "Implement the task in this repository.",
            "Keep changes tightly scoped and complete the work needed for the acceptance criteria.",
        ],
        "testing": [
            "Validate the implementation.",
            "Run focused checks or tests where possible and report failures precisely.",
            "Only make minimal fixes if absolutely necessary.",
        ],
        "accepting": [
            "Act as the reviewer: validate the end-user outcome against the acceptance criteria and decide whether it should be accepted or sent back.",
            "Be strict about regression detection, evidence quality, and final done versus not-done judgment.",
        ],
    },
    "stage_overlay": {},
    "specifics_heading": None,
    "specifics": [],
}


PROCESS_PROFILES: dict[str, dict[str, Any]] = {
    "generic": {},
    "python": {
        "label": "Python",
        "summary": "Python package or application workflow with pytest-oriented verification.",
        "role_model": (
            "`planner` frames the task, `reviewer` performs final PM-style acceptance, "
            "`swe` edits code, and `qa` runs focused verification."
        ),
        "tdd_expectations": "add or update focused tests near the changed Python module before broad suites.",
        "verification_discipline": (
            "prefer targeted `pytest` evidence close to the changed module before broader smoke coverage."
        ),
        "acceptance_flow": "verify behavior with targeted `pytest` coverage and note any residual risk.",
        "commit_recovery": "keep checkpoint commits deterministic and easy to recover.",
        "specifics_heading": "## Python specifics",
        "specifics": [
            "- Prefer `pytest` for automated verification.",
            "- Keep module boundaries and import hygiene clear.",
            "- Record virtualenv, `uv`, or toolchain expectations when they matter.",
        ],
        "workspace_overlay": [
            "- Prefer focused `pytest` coverage for the changed modules.",
            "- Keep dependency and packaging changes explicit and minimal.",
        ],
        "init_scaffold": [
            "- Seed Python workspaces with package layout, test entrypoints, and `uv` or virtualenv expectations.",
        ],
        "stage_overlay": {
            "implementing": [
                "- Write or update focused tests alongside the code change when feasible.",
            ],
            "testing": [
                "- Prefer targeted `pytest` invocations before broader test commands.",
            ],
        },
    },
    "django": {
        "label": "Django",
        "summary": "Django application workflow with app-level tests, migrations discipline, and settings awareness.",
        "role_model": (
            "`planner` frames the task, `reviewer` performs final PM-style acceptance, "
            "`swe` updates apps/views/models, and `qa` verifies user-visible behavior."
        ),
        "tdd_expectations": (
            "prefer app-level or regression tests first, especially around models, views, forms, and APIs."
        ),
        "verification_discipline": (
            "verify request paths, ORM behavior, and migration impact with focused Django or pytest coverage."
        ),
        "acceptance_flow": "confirm migrations, settings impact, and targeted test coverage before acceptance.",
        "commit_recovery": "keep schema and data-shape changes explicit so rollback remains predictable.",
        "specifics_heading": "## Django specifics",
        "specifics": [
            "- Call out migrations, settings, fixtures, and management command impacts.",
            "- Prefer focused test modules or app test cases over full-project runs when possible.",
            "- Note any admin, template, or ORM behavior affected by the task.",
        ],
        "tool_usage": [
            "- Use `uv run pytest -q` for the current smoke test suite unless the repo documents a different Django test command.",
            "- Update litehive task artifacts instead of inventing external state stores.",
            "- If you add a new command or workflow, document it here for future runs.",
        ],
        "workspace_overlay": [
            "- Treat migrations, settings, and database state as first-class review items.",
            "- Prefer narrow regression tests around the affected Django app and request path.",
        ],
        "init_scaffold": [
            "- Seed Django workspaces with app boundaries, settings entrypoints, and migration review notes.",
        ],
        "stage_overlay": {
            "implementing": [
                "- Keep model, migration, view, form, and template changes coordinated.",
            ],
            "testing": [
                "- Verify migrations and run focused Django or pytest coverage for the affected app.",
            ],
            "accepting": [
                "- Reject changes that leave migration or settings impact unclear.",
            ],
        },
    },
    "rust": {
        "label": "Rust",
        "summary": "Rust crate or workspace workflow with compile-first discipline and tight tests.",
        "role_model": (
            "`planner` scopes the task, `reviewer` performs final PM-style acceptance, "
            "`swe` edits crates/modules, and `qa` verifies compile and test results."
        ),
        "tdd_expectations": "prefer regression tests or unit tests close to the affected module before broader runs.",
        "verification_discipline": "treat `cargo check`, focused tests, and compiler warnings as acceptance evidence.",
        "acceptance_flow": "compile, run focused tests, and surface warnings or clippy debt explicitly.",
        "commit_recovery": "keep checkpoints deterministic and avoid opaque generated churn.",
        "tool_usage": [
            "- Use the repository's documented `cargo` commands for targeted verification.",
            "- Update litehive task artifacts instead of inventing external state stores.",
            "- If you add a new command or workflow, document it here for future runs.",
        ],
        "specifics_heading": "## Rust specifics",
        "specifics": [
            "- Prefer `cargo test` and targeted package/module verification.",
            "- Keep ownership, error handling, and public API changes explicit.",
            "- Note formatting, lint, and feature-flag expectations when relevant.",
        ],
        "workspace_overlay": [
            "- Favor small, compile-safe changes with clear module ownership.",
            "- Treat compiler errors, warnings, and feature flags as part of verification evidence.",
        ],
        "init_scaffold": [
            "- Seed Rust workspaces with crate boundaries, toolchain expectations, and `cargo` verification commands.",
        ],
        "stage_overlay": {
            "implementing": [
                "- Add or adjust focused Rust tests close to the changed crate or module.",
            ],
            "testing": [
                "- Prefer targeted `cargo test`, `cargo check`, or package-scoped verification before workspace-wide runs.",
            ],
        },
    },
    "cpp": {
        "label": "C/C++",
        "summary": "C or C++ workflow with compile-heavy verification, native toolchains, and linker-aware debugging.",
        "role_model": (
            "`planner` scopes the change, `reviewer` performs final PM-style acceptance, "
            "`swe` edits native code, and `qa` verifies compile and runtime behavior."
        ),
        "tdd_expectations": (
            "prefer focused native regression coverage near the affected target before broader rebuilds."
        ),
        "verification_discipline": (
            "treat compile success, targeted tests, and linker/toolchain diagnostics as acceptance evidence."
        ),
        "acceptance_flow": "verify the target builds cleanly, run focused checks, and surface warnings explicitly.",
        "commit_recovery": "keep generated build churn out of scope so checkpoints stay reviewable and deterministic.",
        "tool_usage": [
            "- Use the repository's documented build and test commands for the affected native target.",
            "- Update litehive task artifacts instead of inventing external state stores.",
            "- If you add a new command or workflow, document it here for future runs.",
        ],
        "specifics_heading": "## C/C++ specifics",
        "specifics": [
            "- Prefer target-scoped builds and tests over full rebuilds when possible.",
            "- Keep ABI, toolchain, warning, and linker impacts explicit.",
            "- Record sanitizer, compiler, or build-system expectations when they affect verification.",
        ],
        "workspace_overlay": [
            "- Favor narrow changes that keep compile and linker failures easy to localize.",
            "- Treat toolchain warnings, generated artifacts, and native resource usage as first-class signals.",
        ],
        "init_scaffold": [
            "- Seed C/C++ workspaces with target boundaries, build commands, and toolchain expectations.",
        ],
        "stage_overlay": {
            "implementing": [
                "- Keep native build-system, header, and source changes coordinated and reviewable.",
            ],
            "testing": [
                "- Prefer target-scoped compile and test commands before broader native builds.",
            ],
        },
    },
    "codehive": {
        "label": "Codehive-style",
        "summary": "Multi-agent coding workflow emphasizing manager routing, TDD, and deterministic recovery.",
        "source_of_truth": "issue/task state and execution artifacts must stay local and explicit.",
        "task_source_of_truth": (
            "issues or queued task records stay authoritative; subagent work must map back to that source."
        ),
        "orchestrator_model": "the orchestrator is the manager; subagents execute but do not choose routing.",
        "routing_model": "manager-owned deterministic routing, retries, and escalation stay in local code rather than prompts.",
        "role_model": (
            "`planner` owns task shaping, `reviewer` owns final PM-style acceptance, "
            "`swe` implements, and `qa` verifies, all through deterministic stage handoffs."
        ),
        "tdd_expectations": "default to regression-first or test-first implementation and explain any exception.",
        "verification_discipline": (
            "verification should be independent enough to catch behavioral regressions, not just restate implementation intent."
        ),
        "acceptance_flow": "only accept after focused verification and explicit comparison against task acceptance.",
        "commit_recovery": (
            "accepted tasks commit by default at commit_to_git using "
            "`litehive: complete <task-id> <task-slug>`; reruns append `(attempt N)`, "
            "rollback reverts that checkpoint into a new rollback commit, and recover requeues without reverting code."
        ),
        "specifics_heading": "## Codehive-style specifics",
        "specifics": [
            "- Preserve execution visibility through task reports, subagent transcripts, and recent progress.",
            "- Prefer narrow, reviewable scope per task and push follow-up work into later tasks.",
            "- Keep routing, retries, and escalation in local code rather than prompt-only behavior.",
        ],
        "tool_usage": [
            "- Update litehive task artifacts instead of inventing external state stores.",
            "- If you add a new workflow or command, document it here so future runs inherit the same context.",
        ],
        "workspace_overlay": [
            "- Treat the orchestrator as the manager and the only authority for routing and retries.",
            "- Maintain high execution visibility through reports, transcripts, and explicit stage summaries.",
        ],
        "init_scaffold": [
            "- Seed codehive-style workspaces with deterministic routing notes, visibility expectations, and checkpoint policy.",
        ],
        "stage_overlay": {
            "grooming": [
                "- Break ambiguity down into a concrete, deterministic plan with clear ownership.",
                "- Planner output should sharpen user value, scope edges, decomposition, and follow-up work.",
            ],
            "implementing": [
                "- Stay within the current task boundary and prefer test-first changes when the repo supports it.",
            ],
            "testing": [
                "- Verification should be independent enough to catch behavioral regressions, not just restate implementation intent.",
            ],
            "accepting": [
                "- Reviewer acceptance is managerial PM-style review against task goals, tests, and recovery policy, not a rubber stamp.",
                "- Reviewer judgment should focus on end-user outcome, regressions, and whether the task is actually done.",
                "- Accepted tasks proceed to `commit_to_git`, where Litehive creates the final checkpoint commit unless auto-commit is explicitly disabled.",
            ],
        },
    },
}


def available_process_profiles() -> list[str]:
    return sorted(PROCESS_PROFILES)


def resolve_process_profile(name: str | None) -> dict[str, Any]:
    profile = deepcopy(SHARED_PROCESS_PROFILE)
    if name is None:
        return profile

    overlay = PROCESS_PROFILES.get(name, PROCESS_PROFILES["generic"])
    for key, value in overlay.items():
        if key in {
            "development_rules",
            "tool_usage",
            "workspace_overlay",
            "specifics",
            "prompt_scaffold",
            "init_scaffold",
        }:
            profile[key].extend(deepcopy(value))
            continue
        if key == "stage_overlay":
            for stage, instructions in value.items():
                profile["stage_overlay"].setdefault(stage, []).extend(deepcopy(instructions))
            continue
        if key == "stage_instructions":
            for stage, instructions in value.items():
                profile["stage_instructions"].setdefault(stage, []).extend(deepcopy(instructions))
            continue
        profile[key] = deepcopy(value)
    return profile


def _render_process_overlay(profile: dict[str, Any]) -> list[str]:
    return [
        "## Process overlay",
        f"- Source of truth: {profile['source_of_truth']}",
        f"- Task source of truth: {profile['task_source_of_truth']}",
        f"- Orchestrator model: {profile['orchestrator_model']}",
        f"- Routing model: {profile['routing_model']}",
        f"- Shared stages: {_shared_stage_text(profile['shared_stages'])}",
        f"- Role model: {profile['role_model']}",
        f"- TDD expectations: {profile['tdd_expectations']}",
        f"- Verification discipline: {profile['verification_discipline']}",
        f"- Acceptance flow: {profile['acceptance_flow']}",
        f"- Commit and recovery: {profile['commit_recovery']}",
    ]


def _render_project_overlay(profile: dict[str, Any]) -> list[str]:
    return [
        "## Project overlay",
        f"- {profile['summary']}",
        *profile["workspace_overlay"],
    ]


def _render_scaffold_sections(profile: dict[str, Any]) -> list[str]:
    return [
        "## Init scaffold",
        *profile["init_scaffold"],
        "",
        "## Prompt scaffold",
        *profile["prompt_scaffold"],
        "",
    ]


def _render_stage_prompt_scaffolding(profile: dict[str, Any]) -> list[str]:
    lines = ["## Stage prompt scaffolding"]
    for stage in profile["shared_stages"]:
        stage_instructions = profile.get("stage_instructions", {}).get(stage, [])
        stage_overlay = profile.get("stage_overlay", {}).get(stage, [])
        if not stage_instructions and not stage_overlay:
            continue
        lines.extend(["", f"### {stage}"])
        lines.extend(stage_instructions)
        lines.extend(stage_overlay)
    lines.append("")
    return lines


def render_context_template(profile_name: str) -> str:
    profile = resolve_process_profile(profile_name)
    lines = [
        "# Litehive Workspace Context",
        "",
        f"Process profile: {profile['label']}",
        "",
        "Describe this repository and how subagents should work in it.",
        "",
        "## Project",
        "- Purpose:",
        "- Main package/module locations:",
        "- Commands to know:",
        "",
    ]
    lines.extend(_render_process_overlay(profile))
    lines.append("")
    lines.extend(_render_project_overlay(profile))
    lines.append("")
    lines.extend(_render_scaffold_sections(profile))
    lines.extend(_render_stage_prompt_scaffolding(profile))
    if profile.get("specifics_heading"):
        lines.append(profile["specifics_heading"])
        lines.extend(profile.get("specifics", []))
        lines.append("")
    lines.extend(
        [
            "## Development rules",
            *profile["development_rules"],
            "",
            "## Tool usage",
            *profile["tool_usage"],
            "",
        ]
    )
    return "\n".join(lines)
