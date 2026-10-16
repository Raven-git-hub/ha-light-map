"""The Host allowlist on the HTTP API (P4): a request whose Host isn't listed is refused
before it reaches any route, which closes the DNS-rebinding path. Not authentication."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from app.main import allowed_hosts, create_app
from tests.fakes import FakeHA


class AllowlistTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "config.json"

    def client(self, base_url, **kwargs):
        app = create_app(ha=FakeHA(), config_path=self.path, **kwargs)
        client = TestClient(app, base_url=base_url)
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        return client

    def test_loopback_is_always_allowed(self):
        for base_url in ("http://localhost", "http://127.0.0.1", "http://localhost:8099"):
            with self.subTest(base_url):
                client = self.client(base_url)
                self.assertEqual(client.get("/api/status").status_code, 200)
                self.assertEqual(client.get("/healthz").status_code, 200)

    def test_any_other_host_is_refused_before_any_route(self):
        client = self.client("http://localhost")
        for path in ("/api/status", "/api/map", "/", "/app.js", "/healthz"):
            with self.subTest(path):
                # What a rebinding page's request carries: its own name, now pointing here.
                response = client.get(path, headers={"Host": "attacker.example:8099"})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.text, "Invalid host header")
        response = client.post(
            "/api/sync", json={"fingerprint": "x"}, headers={"Host": "attacker.example"}
        )
        self.assertEqual(response.status_code, 400)

    def test_the_environment_adds_names(self):
        env = {"LM_ALLOWED_HOSTS": "LightMap.lan, http://192.168.1.20:8099/ ,,"}
        with mock.patch.dict(os.environ, env):
            client = self.client("http://lightmap.lan:8099")
        self.assertEqual(client.get("/api/status").status_code, 200)
        self.assertEqual(
            client.get("/api/status", headers={"Host": "192.168.1.20:8099"}).status_code, 200
        )
        self.assertEqual(
            client.get("/api/status", headers={"Host": "lightmap.evil"}).status_code, 400
        )
        self.assertEqual(
            client.app.state.allowed_hosts,
            ["localhost", "127.0.0.1", "lightmap.lan", "192.168.1.20"],
        )

    def test_parsing(self):
        self.assertEqual(allowed_hosts(None), (["localhost", "127.0.0.1"], []))
        self.assertEqual(
            allowed_hosts("*.home.arpa, bad*name, [::1], fe80::1, localhost"),
            (
                ["localhost", "127.0.0.1", "*.home.arpa"],
                # IPv6 literals can't match: Starlette cuts the Host at its first colon.
                ["bad*name", "[::1]", "fe80::1"],
            ),
        )
        self.assertIn("*", allowed_hosts("*")[0])

    def test_a_wildcard_turns_it_off_with_a_warning(self):
        with (
            mock.patch.dict(os.environ, {"LM_ALLOWED_HOSTS": "*"}),
            self.assertLogs("app.main", "WARNING") as logs,
        ):
            client = self.client("http://anything.example")
            self.assertEqual(client.get("/api/status").status_code, 200)
        self.assertTrue(any("turns the Host check off" in line for line in logs.output))


if __name__ == "__main__":
    unittest.main()
