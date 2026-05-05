"""
Bundled SQL migration files for the workspace database.

The schema runtime in ``litehive.db.schema`` discovers each ``NNNN_name.sql``
file via ``importlib.resources`` and applies it inside a single transaction.
This package is data-only — there is no Python behaviour here, the init exists
so ``importlib.resources.files`` has a package handle to load against.
"""
