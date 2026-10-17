"""The frontend (the Map, the Log, and the Config tab's adaptive editor), pragmatically: static checks that always run (the
JS parses, every API call it makes is a route the app serves, the shell links each view),
and headless-Chromium runs of the key interactions against the app over the fake HA, which
are skipped where Node's Playwright and a Chromium aren't installed. No build step, no real
HA.
"""

import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path

import uvicorn

from app.core import config, deploy
from app.main import create_app, router
from tests.fakes import FakeHA, adaptive_wired, committed_config, scenes

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
SCRIPT = Path(__file__).resolve().parent / "ui" / "map_ui.cjs"
LOG_SCRIPT = Path(__file__).resolve().parent / "ui" / "log_ui.cjs"
CONFIG_SCRIPT = Path(__file__).resolve().parent / "ui" / "config_ui.cjs"
LANES_SCRIPT = Path(__file__).resolve().parent / "ui" / "lanes_ui.cjs"

# `api("PUT", `/api/bulbs/${bulb.id}/hard_switches`, ...)`: the method and path of each call.
API_CALL = re.compile(r'api\(\s*"(GET|POST|PUT|DELETE|PATCH)",\s*[`"](/api/[^`"]*)[`"]')


def node_env() -> dict[str, str] | None:
    """The environment Node needs to find a global Playwright, or None without one."""
    node = shutil.which("node")
    npm = shutil.which("npm")
    if node is None or npm is None:
        return None
    root = subprocess.run([npm, "root", "-g"], capture_output=True, text=True).stdout.strip()
    env = {**os.environ, "NODE_PATH": root}
    probe = subprocess.run([node, "-e", "require('playwright')"], env=env, capture_output=True)
    return env if probe.returncode == 0 else None


@contextmanager
def serve(app):
    """Run the app with uvicorn on a free local port, in a thread, for the browser."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(
        # log_config=None: uvicorn's own logging setup would re-enable every logger a test
        # has silenced.
        uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None, lifespan="on")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("the test server did not start")
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(10)


class StaticTest(unittest.TestCase):
    def test_every_api_call_is_a_route(self):
        routes = [(route.methods, route.path_regex) for route in router.routes]
        calls = set()
        for script in STATIC.glob("*.js"):
            calls |= set(API_CALL.findall(script.read_text(encoding="utf-8")))
        self.assertGreaterEqual(len(calls), 15)
        for method, path in sorted(calls):
            concrete = re.sub(r"\$\{[^}]*\}", "x", path).partition("?")[0]
            with self.subTest(f"{method} {path}"):
                self.assertTrue(
                    any(method in methods and regex.match(concrete) for methods, regex in routes),
                    f"the UI calls {method} {path}, which the app doesn't serve",
                )

    def test_the_shell_loads_the_map(self):
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        for asset in (
            "app.css",
            "map.css",
            "log.css",
            "config.css",
            "app.js",
            "map.js",
            "log.js",
            "config.js",
        ):
            self.assertIn(f'"{asset}"', html)
        for view in ("map.js", "log.js", "config.js"):
            self.assertLess(html.index('src="app.js"'), html.index(f'src="{view}"'))
        for tab in ("map", "now", "analysis", "almanac", "config", "log"):
            self.assertIn(f'href="#{tab}"', html)

    def test_no_markup_is_built_from_strings(self):
        # Names come from Home Assistant: text goes in as text nodes, never as HTML.
        for script in STATIC.glob("*.js"):
            text = script.read_text(encoding="utf-8")
            self.assertNotRegex(
                text, r"\.(innerHTML|outerHTML)\s*=|insertAdjacentHTML", script.name
            )

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_the_scripts_parse(self):
        for script in [*STATIC.glob("*.js"), SCRIPT, LOG_SCRIPT, CONFIG_SCRIPT, LANES_SCRIPT]:
            result = subprocess.run(
                ["node", "--check", str(script)], capture_output=True, text=True
            )
            self.assertEqual(result.returncode, 0, result.stderr)


class BrowserTest(unittest.TestCase):
    """The key interactions in headless Chromium, against the app over the fake HA."""

    def setUp(self):
        env = node_env()
        if env is None:
            self.skipTest("Node's Playwright is not installed")
        self.env = env
        deploy.log.disabled = True  # the fixture's unavailable bulb warns on every deploy
        self.addCleanup(setattr, deploy.log, "disabled", False)

    def test_the_map(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            config.save(path, committed_config())
            ha = FakeHA()
            app = create_app(ha=ha, config_path=path, verify_delay=0)
            shots = os.environ.get("LM_UI_SHOTS")  # set to a directory to keep screenshots
            with serve(app) as url:
                result = subprocess.run(
                    ["node", str(SCRIPT), url, *([shots] if shots else [])],
                    env=self.env,
                    capture_output=True,
                    text=True,
                    timeout=180,
                )
            lines = result.stdout.strip().splitlines()
            if "Executable doesn't exist" in result.stdout + result.stderr:
                self.skipTest("Playwright's Chromium is not installed")
            self.assertTrue(lines, result.stderr)
            report = json.loads(lines[-1])
            self.assertTrue(report["ok"], json.dumps(report, indent=2) + result.stderr)
            self.assertEqual(len(report["steps"]), 18)

            # What the clicks did, as the server and the fake HA saw it.
            saved = json.loads(path.read_text())
            kitchen = next(r for r in saved["rooms"] if r["id"] == "kitchen")
            self.assertEqual(
                [b["id"] for b in kitchen["bulbs"]], ["kitchen_strip", "porch", "counter_lamp"]
            )
            porch = kitchen["bulbs"][1]
            self.assertEqual(
                porch["hard_switches"],
                [
                    {
                        "id": "porch_button",
                        "name": "Porch button",
                        "source": {"type": "state", "entity_id": "switch.reading_relay"},
                        "mode": "toggle",
                    }
                ],
            )
            self.assertEqual(kitchen["bulbs"][0]["hard_switches"], [])  # that popup was cancelled
            # Ignored bulbs stay in the snapshot; the one un-ignored is back as it was.
            self.assertEqual([b["ignored"] for b in kitchen["bulbs"]], [True, False, False])
            # The group created earlier went with its only member when that was ignored.
            self.assertEqual(kitchen["switching_groups"], [])
            self.assertTrue(kitchen["adaptive"])
            self.assertIn("garage", [r["id"] for r in saved["rooms"]])
            living = next(r for r in saved["rooms"] if r["id"] == "living_room")
            self.assertIn("adaptive_config", living)  # the untick was cancelled
            # The stale popup's empty list didn't overwrite the other tab's binding.
            chandelier = next(b for b in living["bulbs"] if b["id"] == "chandelier_1")
            self.assertEqual([h["name"] for h in chandelier["hard_switches"]], ["Other tab"])
            porch_entry = next(e for e in ha.entities if e["entity_id"] == "light.porch")
            self.assertEqual(porch_entry["area_id"], "kitchen")
            self.assertIn("lm_hard_porch_button", ha.automations)
            # Deployed, then removed while it was ignored (and not deployed since).
            self.assertNotIn("lm_soft_counter_lamp", ha.helpers["input_boolean"])
            self.assertNotIn("lm_bridge_counter_lamp", ha.automations)
            self.assertNotIn("lm_soft_kitchen_strip", ha.helpers["input_boolean"])
            self.assertIn("lm_soft_porch", ha.helpers["input_boolean"])
            self.assertEqual(ha.fired, [])  # deploying from the UI drove no bulb

    def run_script(self, script, url, timeout=180):
        shots = os.environ.get("LM_UI_SHOTS")  # a directory to keep screenshots in
        result = subprocess.run(
            ["node", str(script), url, *([shots] if shots else [])],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        lines = result.stdout.strip().splitlines()
        if "Executable doesn't exist" in result.stdout + result.stderr:
            self.skipTest("Playwright's Chromium is not installed")
        self.assertTrue(lines, result.stderr)
        report = json.loads(lines[-1])
        self.assertTrue(report["ok"], json.dumps(report, indent=2) + result.stderr)
        return report

    def test_module_lanes_in_the_deploy_dialog(self):
        # The cutover switch on, in this test's scope only (the live app keeps it off until
        # P6): the dialog shows the al_ lane read-only and applies lm_ alone.
        with tempfile.TemporaryDirectory() as directory, adaptive_wired():
            path = Path(directory) / "config.json"
            config.save(path, committed_config())
            ha = FakeHA()
            app = create_app(ha=ha, config_path=path, verify_delay=0)
            with serve(app) as url:
                report = self.run_script(LANES_SCRIPT, url)
            self.assertEqual(len(report["steps"]), 2)
            self.assertIn("lm_bridge_chandelier_1", ha.automations)
            self.assertEqual([i for i in ha.automations if i.startswith("al_")], [])
            self.assertNotIn("input_select", ha.helpers)
            self.assertEqual(ha.fired, [])

    def test_the_adaptive_editor(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            doc = committed_config()
            living = doc["rooms"][0]
            # Two chandeliers adaptive in one unit; chandelier_3 (unavailable in HA) and the
            # reading lamp switched-only.
            living["adaptive_config"]["adaptive_bulbs"] = ["chandelier_1", "chandelier_2"]
            living["adaptive_config"]["learning_units"] = [
                {
                    "id": "chandelier",
                    "name": "Chandelier",
                    "members": ["chandelier_1", "chandelier_2"],
                }
            ]
            living["adaptive_config"]["scenes"] = scenes("chandelier")
            config.save(path, doc)
            ha = FakeHA()
            # HA reports the reading lamp as on/off only: the editor locks it switched-only.
            lamp = next(s for s in ha.states if s["entity_id"] == "light.reading_lamp")
            lamp["attributes"]["supported_color_modes"] = ["onoff"]
            app = create_app(ha=ha, config_path=path, verify_delay=0)
            with serve(app) as url:
                report = self.run_script(CONFIG_SCRIPT, url)
            self.assertEqual(len(report["steps"]), 12)

            # What the clicks saved, as the server has it.
            living = json.loads(path.read_text())["rooms"][0]
            block = living["adaptive_config"]
            self.assertEqual(
                block["adaptive_bulbs"], ["chandelier_1", "chandelier_2", "chandelier_3"]
            )
            self.assertEqual(
                block["learning_units"],
                [
                    {
                        "id": "living_all",
                        "name": "Chandelier",
                        "members": ["chandelier_1", "chandelier_2", "chandelier_3"],
                    }
                ],
            )
            self.assertEqual(block["scenes"]["night"]["units"], {"living_all": {"mode": "off"}})
            self.assertEqual(block["scenes"]["sunset"]["transition_seconds"], 90)
            config.load(path)  # what the editor saved loads
            # Editing the adaptive config wrote nothing to HA and drove no bulb.
            self.assertEqual(ha.writes, [])
            self.assertEqual(ha.fired, [])

    def test_the_log(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            config.save(path, committed_config())
            app = create_app(ha=FakeHA(), config_path=path)
            with serve(app) as url:
                # The event log's fixtures, oldest first, written as the observer would.
                for level, category, room, message, detail in (
                    ("debug", "heartbeat", None, "Heartbeat: 2 room(s), 5 bulb(s), 2 on.", None),
                    (
                        "info",
                        "reactive",
                        "living_room",
                        "Manual change in Living Room: chandelier_1.",
                        {"chandelier_1": [128, 220]},
                    ),
                    (
                        "warning",
                        "heartbeat",
                        "kitchen",
                        "Skipped a heartbeat: a guard was still raised.",
                        None,
                    ),
                    (
                        "error",
                        "connection",
                        None,
                        "Home Assistant rejected the token, so observation has stopped.",
                        None,
                    ),
                ):
                    app.state.data.log_event(level, category, message, room_id=room, detail=detail)
                shots = os.environ.get("LM_UI_SHOTS")  # a directory to keep screenshots in
                result = subprocess.run(
                    ["node", str(LOG_SCRIPT), url, *([shots] if shots else [])],
                    env=self.env,
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
            lines = result.stdout.strip().splitlines()
            if "Executable doesn't exist" in result.stdout + result.stderr:
                self.skipTest("Playwright's Chromium is not installed")
            self.assertTrue(lines, result.stderr)
            report = json.loads(lines[-1])
            self.assertTrue(report["ok"], json.dumps(report, indent=2) + result.stderr)
            self.assertEqual(len(report["steps"]), 5)


if __name__ == "__main__":
    unittest.main()
