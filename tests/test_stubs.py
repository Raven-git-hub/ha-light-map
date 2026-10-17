"""The P0 boundary: importing does no work, /healthz serves, and no stub is left: every
module P0 stubbed (the last, the adaptive engine, filled in P5.2a) does its work rather than
raising NotImplementedError."""

import ast
import subprocess
import sys
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent

# Any attempt to reach the network (HA) while importing fails the check.
IMPORT_CHECK = """
import importlib, socket, sys
from unittest import mock
with mock.patch.object(socket.socket, "connect", side_effect=AssertionError("network at import")):
    for name in sys.argv[1:]:
        importlib.import_module(name)
"""


def modules():
    for path in sorted([*(ROOT / "app").rglob("*.py"), *(ROOT / "tools").glob("*.py")]):
        parts = path.relative_to(ROOT).with_suffix("").parts
        yield ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


class BoundaryTest(unittest.TestCase):
    def test_importing_does_no_io(self):
        # In a fresh interpreter, so every module really is imported for the first time
        # (and reloading here can't swap the exception classes other tests hold).
        result = subprocess.run(
            [sys.executable, "-c", IMPORT_CHECK, *modules()],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_healthz(self):
        from app.main import app

        client = TestClient(app, base_url="http://localhost")
        self.assertEqual(client.get("/healthz").json(), {"status": "ok"})

    def test_no_stub_is_left(self):
        # A stub raises NotImplementedError; nothing in the app or its tools does any more.
        for path in [*(ROOT / "app").rglob("*.py"), *(ROOT / "tools").glob("*.py")]:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Raise) and node.exc is not None:
                    raised = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
                    name = getattr(raised, "id", getattr(raised, "attr", None))
                    with self.subTest(str(path.relative_to(ROOT))):
                        self.assertNotEqual(name, "NotImplementedError")


if __name__ == "__main__":
    unittest.main()
