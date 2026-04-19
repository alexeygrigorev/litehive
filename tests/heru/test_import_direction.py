import json
import subprocess
import sys


def test_import_heru_does_not_import_litehive() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import importlib, json, sys; "
                "to_clear = [name for name in sys.modules if name == 'heru' or name.startswith('heru.') "
                "or name == 'litehive' or name.startswith('litehive.')]; "
                "[sys.modules.pop(name, None) for name in to_clear]; "
                "importlib.import_module('heru'); "
                "print(json.dumps(sorted(name for name in sys.modules if name == 'litehive' or name.startswith('litehive.'))))"
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == []
