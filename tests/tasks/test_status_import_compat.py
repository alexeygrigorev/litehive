import builtins
import importlib
import sys


def test_tasks_status_imports_without_heru(monkeypatch) -> None:
    for name in (
        "litehive.tasks.status",
        "litehive.tasks.runtime",
        "litehive.domain._heru_compat",
    ):
        monkeypatch.delitem(sys.modules, name, raising=False)

    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "heru" or name.startswith("heru."):
            raise ModuleNotFoundError("No module named 'heru'")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    module = importlib.import_module("litehive.tasks.status")

    assert hasattr(module, "update_task")
