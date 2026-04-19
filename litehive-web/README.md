## litehive-web

This directory holds the paused web dashboard code outside the `litehive/` package.

- Source: extracted from the pre-`T-0324` dashboard implementation that previously lived under `litehive/web/`
- Status: archived for reference only; not installed, not imported by `litehive`, and not covered by the Litehive test suite
- Intent: keep the dashboard code in-repo without reintroducing package-local web modules, web-only dependencies, or new web work inside Litehive

If web work ever resumes, start from this directory instead of recreating `litehive/web/`.
