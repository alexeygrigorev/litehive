"""
Python post-apply hooks for workspace database migrations.

SQL files remain the source of truth for schema changes. Hooks in this
package are for migration-only data backfills that need project models
or validation logic after a specific SQL migration has committed.
"""

import sqlite3

from .task_intent_columns import sync_task_intent_columns


def run_post_migration_hook(connection: sqlite3.Connection, version: int) -> None:
    """
    Run the Python data hook tied to ``version``, if one exists.
    """
    if version == 7:
        sync_task_intent_columns(connection)
