"""
Sandbox isolation profiles for subagent execution.

Owns the per-role sandbox configuration (allow/deny lists, env scrubbing,
shimmed binaries) used when the runner spawns a subagent. The merge-resolver
profile in particular routes ``git`` through ``git_wrapper`` to block
destructive history rewrites; see ``litehive.sandbox.git_wrapper``.
"""
