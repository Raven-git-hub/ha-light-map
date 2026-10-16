"""tools/doctor.py, driven against the fake HAClient. It must never write to HA."""

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.core import config, inventory
from app.core.ha import HAAuthError
from tests.fakes import FakeHA, committed_config
from tools import doctor


class DoctorTest(unittest.TestCase):
    def run_doctor(self, ha, *argv, env=None):
        env = {"LM_HA_BASE_URL": "http://ha.local:8123", "LM_HA_TOKEN": "t"} if env is None else env
        out = io.StringIO()
        with (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(doctor, "HAClient", lambda **kwargs: ha),
            contextlib.redirect_stdout(out),
        ):
            code = doctor.main(list(argv))
        return code, out.getvalue()

    def test_prints_the_rooms_and_the_problems_without_writing(self):
        ha = FakeHA()
        code, out = self.run_doctor(ha)
        self.assertEqual(code, 0)
        self.assertEqual(ha.writes, [])
        self.assertTrue(ha.closed)
        for expected in (
            "Living Room  [room living_room, Area living_room]",
            "chandelier_1",
            "Garage  [room garage, Area garage]",
            "Unassigned",
            "1 light groups are skipped",
            "light.living_room_group",
            "1 lights have no entity-registry entry",
            "light.legacy_yaml",
            "1 lights have no Area (Unassigned)",
            "1 lights are unavailable right now",
            "The snapshot built from HA validates",
        ):
            self.assertIn(expected, out)

    def test_compares_a_committed_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            config.save(path, committed_config())
            code, out = self.run_doctor(FakeHA(), "--config", str(path))
        self.assertEqual(code, 0)
        self.assertIn("a sync would make 3 changes", out)
        self.assertIn("bulb added: light.counter_lamp -> room 'kitchen'", out)
        self.assertIn("switching groups", out)

    def test_an_ignored_light_is_listed_once_and_not_warned_about(self):
        doc = inventory.set_ignored(committed_config(), "porch", True)
        doc = inventory.set_ignored(doc, "chandelier_3", True)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            config.save(path, doc)
            code, out = self.run_doctor(FakeHA(), "--config", str(path))
        self.assertEqual(code, 0)
        self.assertIn("2 lights are ignored in the committed config", out)
        # porch has no Area and chandelier_3 is unavailable: nothing to fix for either.
        self.assertNotIn("lights have no Area", out)
        self.assertNotIn("lights are unavailable", out)
        self.assertRegex(out, r"porch +light\.porch +Porch  \(ignored\)")
        self.assertNotIn("lm_soft_porch", out)

    def test_previews_a_switching_plane_deploy_without_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            config.save(path, committed_config())
            ha = FakeHA()
            code, out = self.run_doctor(ha, "--config", str(path))
            self.assertEqual(ha.writes, [])
            for expected in (
                "A deploy of the switching plane would make 20 changes",
                "create input_boolean lm_soft_porch",
                "create automation lm_bridge_porch",
                "create automation lm_hard_living_all_off",
                "set input_boolean.lm_soft_porch to its bulb's current state first",
            ):
                self.assertIn(expected, out)
            ha.states = [s for s in ha.states if s["entity_id"] != "switch.reading_relay"]
            code, out = self.run_doctor(ha, "--config", str(path))
        self.assertEqual(code, 1)
        self.assertIn("A deploy of the switching plane would be refused", out)
        self.assertIn("switch.reading_relay (hard switch 'reading_switch')", out)

    def test_warns_when_area_writes_will_be_refused(self):
        code, out = self.run_doctor(FakeHA(admin=False))
        self.assertEqual(code, 0)
        self.assertIn("not an administrator", out)

    def test_a_rejected_token_fails(self):
        ha = FakeHA()

        async def reject():
            raise HAAuthError("rejected")

        ha.ping = reject
        code, out = self.run_doctor(ha)
        self.assertEqual(code, 1)
        self.assertIn("Token rejected", out)

    def test_needs_the_connection_settings(self):
        code, out = self.run_doctor(FakeHA(), env={})
        self.assertEqual(code, 2)
        self.assertIn("LM_HA_BASE_URL", out)


if __name__ == "__main__":
    unittest.main()
