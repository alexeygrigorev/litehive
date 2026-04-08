"""Shared test fixtures."""

import os

import pytest

import litehive.engines._codex_quota as _codex_quota_mod

# Skip fsync in tests — saves ~70% of file write time
os.environ["LITEHIVE_SKIP_FSYNC"] = "1"


def _noop_block_reason(**kw):
    return None


def _noop_check_quota(**kw):
    return _codex_quota_mod.CodexQuotaStatus(error="test-disabled")


@pytest.fixture(autouse=True)
def _neutralize_codex_quota(request, monkeypatch):
    """Prevent real codex quota API calls during tests.

    Tests in test_codex_quota.py exercise the quota module directly with
    explicit fake fetchers and are excluded from this fixture.
    """
    _codex_quota_mod.reset_cache()
    if request.module.__name__ != "tests.test_codex_quota":
        # Patch at the source module
        monkeypatch.setattr(_codex_quota_mod, "codex_quota_block_reason", _noop_block_reason)
        # Patch at import sites that did `from ... import codex_quota_block_reason`
        try:
            import litehive.cli._dry_run as dry_run_mod

            monkeypatch.setattr(dry_run_mod, "codex_quota_block_reason", _noop_block_reason)
        except (ImportError, AttributeError):
            pass
        try:
            import litehive.runtime._builder as builder_mod

            monkeypatch.setattr(builder_mod, "codex_quota_block_reason", _noop_block_reason)
            monkeypatch.setattr(builder_mod, "check_codex_quota", _noop_check_quota)
        except (ImportError, AttributeError):
            pass
    yield
    _codex_quota_mod.reset_cache()
