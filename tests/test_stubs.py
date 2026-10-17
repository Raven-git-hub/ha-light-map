"""The P0 boundary: importing does no work, /healthz serves, and every module left for a
later phase (P5.2: the scheduler, analyser, almanac store and publish, and the runtime) still
raises NotImplementedError rather than passing silently."""

import asyncio
import importlib
import inspect
import subprocess
import sys
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent

# Later phases' modules: (module, callables and the arguments to call them with).
STUBS = {
    "app.modules.adaptive.scheduler": {
        "compute_boundaries": ({}, None, {}),
        "apply_collision_policy": ([],),
        "section_at": ({}, None, {}),
    },
    "app.modules.adaptive.analyser": {"run": (None, {}), "pool_unit": ([], {}, "day")},
    "app.modules.adaptive.almanac": {
        "latest": (None, "r"),
        "record": (None, []),
        "publish": (None, "r", {}),
    },
    "app.modules.adaptive.runtime": {"AdaptiveRuntime": (None, None, None, {})},
    "tools.sweep": {"main": ([],)},
}
KEYWORDS = {"run": {"now": None}}

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

    def test_later_phases_are_still_stubs(self):
        for module_name, calls in STUBS.items():
            module = importlib.import_module(module_name)
            for name, args in calls.items():
                with self.subTest(f"{module_name}.{name}"):
                    target = getattr(module, name)
                    with self.assertRaises(NotImplementedError):
                        result = target(*args, **KEYWORDS.get(name, {}))
                        if inspect.iscoroutine(result):
                            asyncio.run(result)


if __name__ == "__main__":
    unittest.main()
