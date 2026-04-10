# T-0311 Fix Typer migration regressions — task add and queue promote are broken

## 2026-04-10T14:50:15+00:00
Task created immediately after T-0263 (Typer migration) landed. Two regressions discovered within minutes of landing: `task add` drops all options silently, `queue promote` crashes with AttributeError on a non-existent typer API. Both are blockers for normal workspace operation.
