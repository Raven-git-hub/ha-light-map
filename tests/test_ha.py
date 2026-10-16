"""app.core.ha: the token, REST over a mock transport, and the WebSocket against a local
fake HA server (127.0.0.1 only; no real Home Assistant)."""

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx
from websockets.asyncio.server import serve

from app.core import ha as ha_mod
from app.core.ha import HAAuthError, HAClient, HACommandError, HAError, HANotFoundError

TOKEN = "good-token"


class TokenTest(unittest.TestCase):
    def env(self, **values):
        return mock.patch.dict(os.environ, values, clear=True)

    def token_file(self, content):
        handle = tempfile.NamedTemporaryFile("w", delete=False, suffix=".token")
        handle.write(content)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def test_lm_ha_token_wins(self):
        with self.env(LM_HA_TOKEN="from-env", LM_HA_TOKEN_FILE=self.token_file("from-file")):
            self.assertEqual(ha_mod.token_from_env(), "from-env")

    def test_falls_back_to_the_file_and_strips_its_newline(self):
        with self.env(LM_HA_TOKEN_FILE=self.token_file("from-file\n")):
            self.assertEqual(ha_mod.token_from_env(), "from-file")

    def test_a_blank_lm_ha_token_does_not_shadow_the_file(self):
        with self.env(LM_HA_TOKEN="  ", LM_HA_TOKEN_FILE=self.token_file("from-file")):
            self.assertEqual(ha_mod.token_from_env(), "from-file")

    def test_errors(self):
        missing = str(Path(tempfile.gettempdir()) / "lm-no-such-token-file")
        for values in ({}, {"LM_HA_TOKEN": ""}, {"LM_HA_TOKEN_FILE": missing}):
            with self.env(**values), self.assertRaises(HAError):
                ha_mod.token_from_env()
        with self.env(LM_HA_TOKEN_FILE=self.token_file("\n")), self.assertRaises(HAError):
            ha_mod.token_from_env()


class ClientBasicsTest(unittest.IsolatedAsyncioTestCase):
    async def test_repr_leaves_the_token_out(self):
        self.assertNotIn(TOKEN, repr(HAClient("http://ha:8123", TOKEN)))

    async def test_websocket_url(self):
        self.assertEqual(ha_mod._websocket_url("https://ha:8123"), "wss://ha:8123/api/websocket")
        self.assertEqual(ha_mod._websocket_url("http://ha:8123"), "ws://ha:8123/api/websocket")
        with self.assertRaises(HAError):
            HAClient("ha:8123", TOKEN)

    async def test_subscribe_events_arrives_in_p4(self):
        async def handler(event):
            pass

        with self.assertRaises(NotImplementedError):
            await HAClient("http://ha:8123", TOKEN).subscribe_events("state_changed", handler)


class RestTest(unittest.IsolatedAsyncioTestCase):
    def client(self, respond):
        self.requests: list[httpx.Request] = []

        def handler(request):
            self.requests.append(request)
            return respond(request)

        client = HAClient("http://ha:8123/", TOKEN, transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.close)
        return client

    async def test_get_states_sends_the_bearer_token(self):
        client = self.client(lambda r: httpx.Response(200, json=[{"entity_id": "light.a"}]))
        self.assertEqual(await client.get_states(), [{"entity_id": "light.a"}])
        request = self.requests[0]
        self.assertEqual((request.method, str(request.url)), ("GET", "http://ha:8123/api/states"))
        self.assertEqual(request.headers["Authorization"], f"Bearer {TOKEN}")

    async def test_set_state_and_call_service(self):
        client = self.client(lambda r: httpx.Response(200, json={"ok": True}))
        await client.set_state("sensor.al_almanac_lounge", "ok", {"units": {}})
        await client.call_service("light", "turn_on", {"entity_id": "light.a"})
        self.assertEqual(
            [(r.method, r.url.path, json.loads(r.content)) for r in self.requests],
            [
                (
                    "POST",
                    "/api/states/sensor.al_almanac_lounge",
                    {"state": "ok", "attributes": {"units": {}}},
                ),
                ("POST", "/api/services/light/turn_on", {"entity_id": "light.a"}),
            ],
        )

    async def test_ids_in_paths_are_checked_before_sending(self):
        client = self.client(lambda r: httpx.Response(200))
        for call in (
            client.set_state("../config", "x", {}),
            client.call_service("light", "../turn_on", {}),
            client.request("GET", "/auth/token"),
        ):
            with self.assertRaises(ValueError):
                await call
        self.assertEqual(self.requests, [])

    async def test_a_rejected_token(self):
        client = self.client(lambda r: httpx.Response(401, text="401: Unauthorized"))
        with self.assertRaises(HAAuthError):
            await client.ping()

    async def test_http_errors_and_unreachable_hosts(self):
        with self.assertRaises(HAError):
            await self.client(lambda r: httpx.Response(500)).get_states()

        def refuse(request):
            raise httpx.ConnectError("connection refused", request=request)

        with self.assertRaises(HAError):
            await self.client(refuse).get_states()

    async def test_automation_configs(self):
        stored = {"id": "lm_bridge_a", "alias": "lm_bridge_a", "triggers": []}

        def respond(request):
            if request.url.path.endswith("/lm_missing"):
                status = 404 if request.method == "GET" else 400
                return httpx.Response(status, json={"message": "Resource not found"})
            if request.method == "GET":
                return httpx.Response(200, json=stored)
            return httpx.Response(200, json={"result": "ok"})

        client = self.client(respond)
        self.assertEqual(await client.get_automation_config("lm_bridge_a"), stored)
        self.assertIsNone(await client.get_automation_config("lm_missing"))
        await client.save_automation_config("lm_bridge_a", stored)
        self.assertTrue(await client.delete_automation_config("lm_bridge_a"))
        self.assertFalse(await client.delete_automation_config("lm_missing"))
        # A delete looks first: HA answers a DELETE of a missing id with a bare 400.
        self.assertEqual(
            [(r.method, r.url.path) for r in self.requests],
            [
                ("GET", "/api/config/automation/config/lm_bridge_a"),
                ("GET", "/api/config/automation/config/lm_missing"),
                ("POST", "/api/config/automation/config/lm_bridge_a"),
                ("GET", "/api/config/automation/config/lm_bridge_a"),
                ("DELETE", "/api/config/automation/config/lm_bridge_a"),
                ("GET", "/api/config/automation/config/lm_missing"),
            ],
        )
        self.assertEqual(json.loads(self.requests[2].content), stored)
        with self.assertRaises(ValueError):
            await client.save_automation_config("../../config", {})

    async def test_a_refusal_carries_has_reason(self):
        client = self.client(
            lambda r: httpx.Response(400, json={"message": "Message malformed: extra keys"})
        )
        with self.assertRaises(HAError) as caught:
            await client.save_automation_config("lm_bridge_a", {})
        self.assertIn("HTTP 400: Message malformed: extra keys", str(caught.exception))
        self.assertNotIsInstance(caught.exception, HANotFoundError)
        with self.assertRaises(HANotFoundError):
            await self.client(lambda r: httpx.Response(404, text="404: Not Found")).get_states()

    async def test_empty_and_text_bodies(self):
        self.assertIsNone(await self.client(lambda r: httpx.Response(200)).request("GET", "/api/x"))
        text = await self.client(lambda r: httpx.Response(200, text="hi")).request("GET", "/api/x")
        self.assertEqual(text, "hi")


AREAS = [{"area_id": "kitchen", "name": "Kitchen"}]
ENTRY = {"entity_id": "light.lamp", "area_id": "kitchen", "device_id": None}
SILENT, HANG_UP = object(), object()


class FakeHAServer:
    """Speaks just enough of HA's WebSocket API: auth, then one canned answer per type."""

    def __init__(self):
        self.received: list[dict] = []
        self.connections = 0

    def answer(self, message):
        kind = message["type"]
        if kind == "test/hang_up":
            return HANG_UP
        if kind == "test/ignore":
            return SILENT
        if kind == "test/fail" or (
            kind == "config/entity_registry/get" and message["entity_id"] != "light.lamp"
        ):
            code = "not_found" if kind != "test/fail" else "home_assistant_error"
            return {"success": False, "error": {"code": code, "message": "nope"}}
        results = {
            "config/area_registry/list": AREAS,
            "config/area_registry/create": {"area_id": "office", "name": message.get("name")},
            "config/area_registry/update": {
                "area_id": message.get("area_id"),
                "name": message.get("name"),
            },
            "config/entity_registry/list": [ENTRY],
            "config/entity_registry/get": ENTRY,
            "config/entity_registry/update": {
                "entity_entry": {**ENTRY, "area_id": message.get("area_id")}
            },
            "config/device_registry/list": [{"id": "dev1", "area_id": None}],
            "input_boolean/list": [{"id": "lm_soft_a", "name": "LM Soft a"}],
            "input_boolean/create": {"id": "lm_soft_a", "name": message.get("name")},
            "input_boolean/update": {
                "id": message.get("input_boolean_id"),
                "name": message.get("name"),
            },
        }
        return {"success": True, "result": results.get(kind)}

    async def handler(self, ws):
        self.connections += 1
        await ws.send(json.dumps({"type": "auth_required", "ha_version": "2026.10.0"}))
        auth = json.loads(await ws.recv())
        if auth != {"type": "auth", "access_token": TOKEN}:
            await ws.send(json.dumps({"type": "auth_invalid", "message": "Invalid access token"}))
            return
        await ws.send(json.dumps({"type": "auth_ok", "ha_version": "2026.10.0"}))
        async for raw in ws:
            message = json.loads(raw)
            self.received.append(message)
            reply = self.answer(message)
            if reply is HANG_UP:
                await ws.close()
                return
            if reply is not SILENT:
                await ws.send(json.dumps({"id": message["id"], "type": "result", **reply}))


class WebSocketTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fake = FakeHAServer()
        self.server = await serve(self.fake.handler, "127.0.0.1", 0)
        port = self.server.sockets[0].getsockname()[1]
        self.base_url = f"http://127.0.0.1:{port}"

    async def asyncTearDown(self):
        self.server.close()
        await self.server.wait_closed()

    def client(self, token=TOKEN, timeout=5.0):
        client = HAClient(self.base_url, token, timeout=timeout)
        self.addAsyncCleanup(client.close)
        return client

    async def test_the_read_loop_runs_before_any_command(self):
        client = self.client()
        await client.connect()
        self.assertTrue(client.connected)
        self.assertFalse(client._reader.done())
        self.assertEqual(client.ha_version, "2026.10.0")
        self.assertEqual(self.fake.received, [])
        # A result can only reach a waiting command through the read loop.
        self.assertEqual(await client.list_areas(), AREAS)

    async def test_registry_commands(self):
        client = self.client()
        self.assertEqual(
            await client.create_area("Office"), {"area_id": "office", "name": "Office"}
        )
        await client.rename_area("kitchen", "Galley")
        self.assertEqual(await client.list_entity_registry(), [ENTRY])
        self.assertEqual(await client.get_entity_registry_entry("light.lamp"), ENTRY)
        self.assertIsNone(await client.get_entity_registry_entry("light.missing"))
        self.assertEqual((await client.set_entity_area("light.lamp", None))["area_id"], None)
        await client.list_device_registry()
        self.assertEqual(
            [{k: v for k, v in m.items() if k != "id"} for m in self.fake.received],
            [
                {"type": "config/area_registry/create", "name": "Office"},
                {"type": "config/area_registry/update", "area_id": "kitchen", "name": "Galley"},
                {"type": "config/entity_registry/list"},
                {"type": "config/entity_registry/get", "entity_id": "light.lamp"},
                {"type": "config/entity_registry/get", "entity_id": "light.missing"},
                {
                    "type": "config/entity_registry/update",
                    "entity_id": "light.lamp",
                    "area_id": None,
                },
                {"type": "config/device_registry/list"},
            ],
        )
        ids = [m["id"] for m in self.fake.received]
        self.assertEqual(ids, sorted(set(ids)))

    async def test_helper_commands(self):
        client = self.client()
        self.assertEqual(
            await client.list_helpers("input_boolean"), [{"id": "lm_soft_a", "name": "LM Soft a"}]
        )
        created = await client.create_helper(
            "input_boolean", {"name": "LM Soft a", "icon": "mdi:x"}
        )
        self.assertEqual(created["id"], "lm_soft_a")
        await client.update_helper("input_boolean", "lm_soft_a", {"name": "LM Soft a"})
        self.assertIsNone(await client.delete_helper("input_boolean", "lm_soft_a"))
        self.assertEqual(
            [{k: v for k, v in m.items() if k != "id"} for m in self.fake.received],
            [
                {"type": "input_boolean/list"},
                {"type": "input_boolean/create", "name": "LM Soft a", "icon": "mdi:x"},
                {
                    "type": "input_boolean/update",
                    "input_boolean_id": "lm_soft_a",
                    "name": "LM Soft a",
                },
                {"type": "input_boolean/delete", "input_boolean_id": "lm_soft_a"},
            ],
        )

    async def test_helper_commands_keep_their_own_keys(self):
        client = self.client()
        for call in (
            client.create_helper("input_boolean", {"name": "x", "type": "input_boolean/delete"}),
            client.update_helper("input_boolean", "a", {"input_boolean_id": "b"}),
            client.list_helpers("input_boolean/../x"),
        ):
            with self.assertRaises(ValueError):
                await call
        self.assertEqual(self.fake.received, [])

    async def test_connects_on_demand_and_only_once(self):
        client = self.client()
        await asyncio.gather(
            client.list_areas(), client.list_areas(), client.list_device_registry()
        )
        self.assertEqual(self.fake.connections, 1)

    async def test_a_failed_command_carries_has_code(self):
        with self.assertRaises(HACommandError) as caught:
            await self.client().command({"type": "test/fail"})
        self.assertEqual(caught.exception.code, "home_assistant_error")

    async def test_a_rejected_token(self):
        client = self.client(token="bad-token")
        with self.assertRaises(HAAuthError):
            await client.connect()
        self.assertFalse(client.connected)

    async def test_a_dropped_socket_fails_waiters_at_once(self):
        client = self.client(timeout=30)
        with self.assertRaises(HAError):
            # Well inside the client's own 30s timeout: the waiter is failed when the read
            # loop sees the close, not left to time out.
            await asyncio.wait_for(client.command({"type": "test/hang_up"}), 3)
        self.assertEqual(await client.list_areas(), AREAS)
        self.assertEqual(self.fake.connections, 2)

    async def test_an_unanswered_command_times_out(self):
        with self.assertRaises(HAError) as caught:
            await self.client(timeout=0.2).command({"type": "test/ignore"})
        self.assertIn("did not answer test/ignore", str(caught.exception))

    async def test_ids_are_the_clients_to_assign(self):
        with self.assertRaises(ValueError):
            await self.client().command({"id": 1, "type": "config/area_registry/list"})

    async def test_close_is_idempotent(self):
        client = self.client()
        await client.list_areas()
        await client.close()
        await client.close()
        self.assertFalse(client.connected)

    async def test_nothing_listening(self):
        self.server.close()
        await self.server.wait_closed()
        with self.assertRaises(HAError):
            await self.client(timeout=2).connect()


if __name__ == "__main__":
    unittest.main()
