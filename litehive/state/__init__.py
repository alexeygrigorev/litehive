"""Workspace persistence layer.

This package owns durable workspace state and the adapters that protect it:
SQLite store access, record repositories, event-log persistence, rebuild safety,
backups, and process/file locking. Domain packages should treat ``state`` as the
persistence boundary rather than a place for task, lifecycle, or UI behavior.
"""
