"""Built-in process profile defaults."""

from typing import Any, TypeAlias

ProfileData: TypeAlias = dict[str, Any]

SHARED_PROCESS_PROFILE: ProfileData = {
    "label": "Generic",
    "summary": "General software project workflow with deterministic local orchestration.",
    "source_of_truth": "tasks and implementation state live under `.litehive/`.",
    "task_source_of_truth": "issues or task records define scope; DB-backed task evidence and reports support routing.",
    "orchestrator_model": "the local runner is the manager and owns stage routing.",
    "routing_model": "routing stays deterministic and local; subagents execute assigned stages but do not self-route.",
    "shared_stages": ["grooming", "implementing", "testing", "accepting", "commit_to_git"],
    "role_model": (
        "orchestrator-as-manager: `planner` owns grooming, `reviewer` owns acceptance, both are PM-style product "
        "roles, `swe` implements, and `qa` verifies."
    ),
    "tdd_expectations": "prefer test-first or test-tight changes and explain deviations.",
    "verification_discipline": "verification should be explicit, focused, and independent enough to catch regressions.",
    "acceptance_flow": "implementation must pass verification before acceptance.",
    "commit_recovery": "successful tasks checkpoint to git; recover should remain deterministic.",
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
        "- Update Litehive task records and reports instead of inventing external state stores.",
        "- If you add a new command or workflow, document it here for future runs.",
    ],
    "workspace_overlay": [
        "- Favor incremental, reviewable changes over broad refactors.",
        "- Keep implementation, verification, and acceptance evidence explicit.",
    ],
    "stage_instructions": {
        "grooming": [
            "Act as the planner: clarify the user problem, inspect the repo if needed, and produce a concrete "
            "execution plan.",
            "Focus on scope clarification, acceptance criteria quality, decomposition, follow-up tasks, and PM sizing.",
            "Do not make code changes in this stage.",
        ],
        "implementing": [
            "Implement the task in this repository.",
            "Keep changes tightly scoped and complete the work needed for the acceptance criteria.",
            "Write tests so each assertion would fail if the feature is broken.",
            "Do not spend test coverage on framework behavior or library guarantees.",
            "Do not add tests that only restate defaults, constants, or static data.",
            "Keep each test focused on one behavior.",
            "Avoid duplicate coverage; extend an existing test only when it is the same behavior.",
        ],
        "testing": [
            "Validate the implementation.",
            "Run focused checks or tests where possible and report failures precisely.",
            "Only make minimal fixes if absolutely necessary.",
            "Reject tests whose assertions would still pass if the feature were broken.",
            "Reject tests that duplicate existing coverage instead of covering a new behavior.",
            "Reject tests longer than 50 lines unless a shorter structure is genuinely impossible.",
            "Reject monolithic tests that exercise 5 or more behaviors in one flow.",
        ],
        "accepting": [
            "Act as the reviewer: validate the end-user outcome against the acceptance criteria and decide whether it "
            "should be accepted or sent back.",
            "Be strict about regression detection, evidence quality, and final done versus not-done judgment.",
        ],
    },
    "stage_overlay": {},
    "specifics_heading": None,
    "specifics": [],
}

PROCESS_PROFILE_OVERLAYS: dict[str, ProfileData] = {
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
            "`planner` owns task shaping, `reviewer` owns final PM-style acceptance, `swe` implements, and `qa` "
            "verifies, all through deterministic stage handoffs."
        ),
        "tdd_expectations": "default to regression-first or test-first implementation and explain any exception.",
        "verification_discipline": (
            "verification should be independent enough to catch behavioral regressions, not just restate "
            "implementation intent."
        ),
        "acceptance_flow": "only accept after focused verification and explicit comparison against task acceptance.",
        "commit_recovery": (
            "accepted tasks commit by default at commit_to_git using `litehive: complete <task-id> <task-slug>`; "
            "reruns append `(attempt N)`, and recover requeues a completed task without reverting code."
        ),
        "specifics_heading": "## Codehive-style specifics",
        "specifics": [
            "- Preserve execution visibility through task reports, task evidence, and recent progress.",
            "- Prefer narrow, reviewable scope per task and push follow-up work into later tasks.",
            "- Keep routing, retries, and escalation in local code rather than prompt-only behavior.",
        ],
        "tool_usage": [
            "- Update Litehive task records and reports instead of inventing external state stores.",
            "- If you add a new workflow or command, document it here so future runs inherit the same context.",
        ],
        "workspace_overlay": [
            "- Treat the orchestrator as the manager and the only authority for routing and retries.",
            "- Maintain high execution visibility through reports, task evidence, and explicit stage summaries.",
        ],
        "init_scaffold": [
            "- Seed codehive-style workspaces with deterministic routing notes, visibility expectations, and checkpoint policy."
        ],
        "stage_overlay": {
            "grooming": [
                "- Break ambiguity down into a concrete, deterministic plan with clear ownership.",
                "- Planner output should sharpen user value, scope edges, decomposition, and follow-up work.",
            ],
            "implementing": [
                "- Stay within the current task boundary and prefer test-first changes when the repo supports it."
            ],
            "testing": [
                "- Verification should be independent enough to catch behavioral regressions, not just restate implementation intent."
            ],
            "accepting": [
                "- Reviewer acceptance is managerial PM-style review against task goals, tests, and recovery policy, not a rubber stamp.",
                "- Reviewer judgment should focus on end-user outcome, regressions, and whether the task is actually done.",
                "- Accepted tasks proceed to `commit_to_git`, where Litehive creates the final checkpoint commit unless auto-commit is explicitly disabled.",
            ],
        },
    },
    "cpp": {
        "label": "C/C++",
        "summary": "C or C++ workflow with compile-heavy verification, native toolchains, and linker-aware debugging.",
        "role_model": (
            "`planner` scopes the change, `reviewer` performs final PM-style acceptance, `swe` edits native code, "
            "and `qa` verifies compile and runtime behavior."
        ),
        "tdd_expectations": "prefer focused native regression coverage near the affected target before broader rebuilds.",
        "verification_discipline": "treat compile success, targeted tests, and linker/toolchain diagnostics as acceptance evidence.",
        "acceptance_flow": "verify the target builds cleanly, run focused checks, and surface warnings explicitly.",
        "commit_recovery": "keep generated build churn out of scope so checkpoints stay reviewable and deterministic.",
        "tool_usage": [
            "- Use the repository's documented build and test commands for the affected native target.",
            "- Update Litehive task records and reports instead of inventing external state stores.",
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
            "- Seed C/C++ workspaces with target boundaries, build commands, and toolchain expectations."
        ],
        "stage_overlay": {
            "implementing": ["- Keep native build-system, header, and source changes coordinated and reviewable."],
            "testing": ["- Prefer target-scoped compile and test commands before broader native builds."],
        },
    },
    "django": {
        "label": "Django",
        "summary": "Django application workflow with app-level tests, migrations discipline, and settings awareness.",
        "role_model": (
            "`planner` frames the task, `reviewer` performs final PM-style acceptance, `swe` updates apps/views/models, "
            "and `qa` verifies user-visible behavior."
        ),
        "tdd_expectations": "prefer app-level or regression tests first, especially around models, views, forms, and APIs.",
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
            "- Update Litehive task records and reports instead of inventing external state stores.",
            "- If you add a new command or workflow, document it here for future runs.",
        ],
        "workspace_overlay": [
            "- Treat migrations, settings, and database state as first-class review items.",
            "- Prefer narrow regression tests around the affected Django app and request path.",
        ],
        "init_scaffold": [
            "- Seed Django workspaces with app boundaries, settings entrypoints, and migration review notes."
        ],
        "stage_overlay": {
            "implementing": [
                "- Keep model, migration, view, form, and template changes coordinated.",
                "- Do not add tests that only prove ORM round-trips, model field persistence, or `CASCADE` behavior.",
                "- Do not test URL resolution separately when request-level coverage already exercises the route.",
                "- Use `setUpTestData` for read-only fixtures that can be shared across tests.",
                "- Prefer `assertContains` for response body checks instead of brittle string matching.",
            ],
            "testing": [
                "- Verify migrations and run focused Django or pytest coverage for the affected app.",
                "- Reject tests that only prove ORM round-trips, model field persistence, or `CASCADE` behavior.",
                "- Reject standalone URL resolution tests unless routing behavior itself is the feature under test.",
                "- Reject read-only fixture setup that should use `setUpTestData`.",
                "- Reject response assertions that use raw string matching where `assertContains` is the correct Django assertion.",
            ],
            "accepting": ["- Reject changes that leave migration or settings impact unclear."],
        },
    },
    "generic": {},
    "python": {
        "label": "Python",
        "summary": "Python package or application workflow with pytest-oriented verification.",
        "role_model": (
            "`planner` frames the task, `reviewer` performs final PM-style acceptance, `swe` edits code, and `qa` "
            "runs focused verification."
        ),
        "tdd_expectations": "add or update focused tests near the changed Python module before broad suites.",
        "verification_discipline": "prefer targeted `pytest` evidence close to the changed module before broader smoke coverage.",
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
            "- Seed Python workspaces with package layout, test entrypoints, and `uv` or virtualenv expectations."
        ],
        "stage_overlay": {
            "implementing": [
                "- Write or update focused tests alongside the code change when feasible.",
                "- Use `pytest` for automated verification.",
                "- Use `tmp_path` or pytest fixtures instead of manual tempfiles in repo code or `/tmp` setup.",
                "- Mock external calls and integration edges, not the internal logic under test.",
                "- Do not use `time.sleep` in tests; use deterministic synchronization or time control.",
            ],
            "testing": [
                "- Prefer targeted `pytest` invocations before broader test commands.",
                "- Verify new or updated tests use `pytest` idioms and fixtures.",
                "- Reject tests that use manual tempfiles where `tmp_path` would make isolation explicit.",
                "- Reject tests that mock the unit's internal logic instead of external boundaries.",
                "- Reject tests that rely on `time.sleep` instead of deterministic control.",
            ],
        },
    },
    "rust": {
        "label": "Rust",
        "summary": "Rust crate or workspace workflow with compile-first discipline and tight tests.",
        "role_model": (
            "`planner` scopes the task, `reviewer` performs final PM-style acceptance, `swe` edits crates/modules, "
            "and `qa` verifies compile and test results."
        ),
        "tdd_expectations": "prefer regression tests or unit tests close to the affected module before broader runs.",
        "verification_discipline": "treat `cargo check`, focused tests, and compiler warnings as acceptance evidence.",
        "acceptance_flow": "compile, run focused tests, and surface warnings or clippy debt explicitly.",
        "commit_recovery": "keep checkpoints deterministic and avoid opaque generated churn.",
        "tool_usage": [
            "- Use the repository's documented `cargo` commands for targeted verification.",
            "- Update Litehive task records and reports instead of inventing external state stores.",
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
            "- Seed Rust workspaces with crate boundaries, toolchain expectations, and `cargo` verification commands."
        ],
        "stage_overlay": {
            "implementing": ["- Add or adjust focused Rust tests close to the changed crate or module."],
            "testing": [
                "- Prefer targeted `cargo test`, `cargo check`, or package-scoped verification before workspace-wide runs."
            ],
        },
    },
}
