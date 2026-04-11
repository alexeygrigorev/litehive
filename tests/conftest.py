"""Shared test fixtures."""

import os

import pytest

import litehive.agents.quota.codex_quota as _codex_quota_mod

# Skip fsync in tests — saves ~70% of file write time
os.environ["LITEHIVE_SKIP_FSYNC"] = "1"


def _noop_block_reason(**kw):
    return None


def _noop_check_quota(**kw):
    return _codex_quota_mod.UsageStatus(error="test-disabled")


def _noop_engine_quota_block(*args, **kwargs):
    return None, None


@pytest.fixture(autouse=True)
def _neutralize_codex_quota(request, monkeypatch):
    """Prevent real codex quota API calls during tests."""
    _codex_quota_mod.reset_cache()
    # Patch at the source module
    monkeypatch.setattr(_codex_quota_mod, "codex_quota_block_reason", _noop_block_reason)
    # Patch at import sites that did `from ... import codex_quota_block_reason`
    try:
        import litehive.cli._dry_run as dry_run_mod

        monkeypatch.setattr(dry_run_mod, "codex_quota_block_reason", _noop_block_reason)
    except (ImportError, AttributeError):
        pass
    try:
        import litehive.pipeline._models as models_mod

        monkeypatch.setattr(models_mod, "_engine_quota_block", _noop_engine_quota_block)
    except (ImportError, AttributeError):
        pass
    yield
    _codex_quota_mod.reset_cache()
