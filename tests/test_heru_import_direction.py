import json
import os
import subprocess
import sys


def test_import_heru_does_not_transitively_import_litehive() -> None:
    script = """
import json
import sys

import heru

del heru
loaded = sorted(name for name in sys.modules if name == "litehive" or name.startswith("litehive."))
print(json.dumps(loaded))
"""
    env = os.environ.copy()
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )

    assert json.loads(completed.stdout) == []
