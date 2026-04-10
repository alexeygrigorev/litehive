# Sandboxing And Git Access

Litehive now applies git access policy at the sandbox filesystem boundary, not
through prompt text, role env vars, or PATH-only tricks.

## Profiles

Every subagent role is mapped through one audited code path in
`litehive/agents/sandbox.py`:

- `merge-resolver` -> `merge-resolver`
- every other role, including unknown roles -> `no-git`

The default is fail-closed. If Litehive does not recognize a role, it gets the
same no-git sandbox as `planner`, `swe`, `qa`, `reviewer`, `recovery`, and
other non-merge-resolver stages.

## No-Git Profile

The no-git profile builds a sandbox filesystem view where `git` is absent from
reachable executable paths. Litehive mirrors the mounted command directories
without the `git` entrypoint, so normal invocation and known absolute paths such
as `/usr/bin/git` fail inside the sandbox.

This policy does not depend on:

- `PATH`
- `LD_PRELOAD`
- `LITEHIVE_*`
- prompt text
- agent-reported role strings inside the sandbox

Even if an agent mutates its environment, there is no visible `git` binary in
the sandbox filesystem view for non-merge-resolver roles.

Litehive also removes the `origin` remote from task worktrees before handing
them to normal execution roles. That is a second layer of protection in case a
sandbox policy is bypassed.

## Merge-Resolver Profile

`merge-resolver` is the only role that receives a visible `git` command. The
profile prepends a single wrapper at `/sandbox/bin/git` and keeps the normal git
entrypoints masked from the sandboxed filesystem view.

The wrapper allows safe commands such as:

- `git --version`
- `git status`
- `git add`
- `git commit`
- normal merge-resolution flows

The wrapper rejects a hardcoded denylist and writes a rejection entry to
`.litehive/runtime/attention.log`.

Denied commands include:

- `git push --force`
- `git push -f`
- `git push --force-with-lease`
- `git push --mirror`
- `git filter-repo`
- `git filter-branch`
- `git reflog expire`
- `git gc --prune=now`
- `git update-ref -d refs/remotes/*`
- `git reset --hard origin/*`
- `git remote set-url origin ...`
- `git rebase` onto protected refs
- `git cherry-pick` onto protected refs

## Tests

Deterministic coverage lives in `tests/test_sandbox_git_profiles.py`.

Those tests spawn a real subprocess through the sandbox launcher. They are
written against the sandbox primitive directly rather than launching an LLM
agent, so they stay fast and deterministic.

On hosts where bubblewrap itself cannot create a sandbox namespace, the direct
integration cases are skipped rather than reporting a false Litehive failure.
The policy code still remains covered by the non-skipped unit tests.

## Manual Breakout Audit

Manual adversarial audits live under `experiments/sandbox-breakout/`.

That directory is intentionally outside the default pytest suite. Use it when
you want to launch a real agent CLI inside the no-git profile and probe for
unexpected escape paths such as alternate CLIs, Python subprocess tricks, or
filesystem discovery behavior that the deterministic tests did not model.
