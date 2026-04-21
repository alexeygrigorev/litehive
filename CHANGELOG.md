# Changelog

## [Unreleased]

### Breaking Changes

**Config Loading Now Strict** - Config loading no longer tolerates unknown/deprecated keys and invalid values. Previously, invalid configurations would be silently ignored or fall back to defaults. Now they will cause litehive to fail with clear error messages.

#### Migration Required

If you have any of the following in your `.litehive/config.yaml`, they must be removed or updated:

1. **Deprecated keys (now error instead of ignored):**
   - `engine_fallbacks` → Use `engine_preference` to specify engine ordering
   - `runner_hook_execution_mode` → Removed (hooks are always synchronous)

2. **Invalid profile/policy values (now error instead of fallback):**
   - `process_profile` must be one of the available profiles (run `litehive status` to see available options)
   - `pool_selection_policy` must be one of: `fifo`, `priority_first`, `dependency_aware`

3. **Unknown config keys:** Any unrecognized top-level or nested config keys will now cause an error

#### What Changed

- Removed silent fallback to 'generic' for invalid `process_profile`
- Removed silent fallback to 'dependency_aware' for invalid `pool_selection_policy`  
- Removed silent ignoring of deprecated config keys
- Unknown process profiles now raise errors instead of falling back to 'generic'
- Config validation errors are now strict and will fail fast

This change improves config reliability by ensuring all configuration is explicitly valid rather than silently falling back to potentially unexpected defaults.