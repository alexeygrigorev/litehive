"""Workspace configuration.

The package is organized by concern; import directly from the module
that owns the symbol you need rather than from this package surface.
Re-exporting from here would force every caller of one submodule to
load the rest of the package, hiding real import dependencies.

- ``loading`` — read/merge/effective-config helpers (`load_config`,
  `load_context`, `merge_config_layers`, `load_effective_config_data`).
- ``model`` — pydantic models for litehive config and sandbox/engine
  policy (`LitehiveConfig`, `ExternalEngineSandboxConfig`,
  `ExternalEngineSandboxPolicy`, validators).
- ``workspace`` — workspace bootstrap and path resolution
  (`ensure_workspace`, `resolve_workspace`,
  `normalize_workspace_root`, `render_workspace_gitignore`).
- ``paths`` — root/workspace path helpers (`litehive_root`,
  `workspace_path`, `workspace_data_dir`).
- ``workspace_files`` — file-name helpers for the workspace layout
  (`config_path`, `context_path`, `workspace_dir`,
  `workspace_gitignore_path`).
- ``registry`` — global cross-workspace registry of known workspaces.
- ``runtime_settings`` — audited per-workspace settings persisted in
  SQLite.
- ``engine_models`` — engine selection and freeze logic
  (`select_engine`, `active_engine_freezes`).
- ``profiles/`` — process profile loaders, defaults, and rendering.
"""
