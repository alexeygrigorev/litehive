"""
SQLite persistence layer for the workspace database.

The schema runtime (``litehive.db.schema``) and the bundled SQL migration
files (``litehive.db.migrations``) live here. Every workspace caller goes
through ``connect_workspace_db`` so pragmas, foreign keys, and on-demand
migration stay in one place — the rest of the system never opens
``data.db`` directly.
"""
