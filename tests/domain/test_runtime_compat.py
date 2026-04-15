import builtins
import importlib
import sys


def test_runtime_modules_fall_back_without_heru(monkeypatch) -> None:
    for name in (
        "litehive.domain.runtime",
        "litehive.domain.common",
        "litehive.domain._heru_compat",
    ):
        monkeypatch.delitem(sys.modules, name, raising=False)

    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "heru" or name.startswith("heru."):
            raise ModuleNotFoundError("No module named 'heru'")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    common = importlib.import_module("litehive.domain.common")
    runtime = importlib.import_module("litehive.domain.runtime")
    ref = runtime.SubagentRef(
        id="SA-0001",
        role="qa",
        engine="codex",
        status="failed",
        path="subagents/SA-0001-qa",
    )

    assert isinstance(common.utcnow(), str)
    assert ref.status == "failed"
