import copy
from pathlib import Path
import tempfile
import unittest

from runtime.ha_setup import Initializer, SetupError, Waiting, device_matches


class FakeAPI:
    def __init__(self):
        self.calls = []
        self.token = None
        self.flow = {"type": "form", "step_id": "user", "flow_id": "flow-1"}

    async def request(self, method, path, data=None, authenticated=True):
        self.calls.append((method, path, data))
        if path == "/api/onboarding":
            return [{"step": name, "done": True} for name in ("user", "core_config", "analytics", "integration")]
        if path.startswith("/api/config/config_entries/flow"):
            if method == "POST" and data == {}:
                return {"type": "create_entry", "result": {"entry_id": "entry-1"}}
            return self.flow
        return []

    async def exchange(self, data):
        return {"access_token": "fake-token", "refresh_token": "fake-refresh"}

    async def ws(self, kind, **values):
        self.calls.append(("WS", kind, values))
        return []


class InitializerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.api = FakeAPI()
        self.state = {"refresh_token": "fake-refresh"}
        self.spec = {"owner": {"name": "Example", "username": "example", "password": "test-password-only", "language": "en"}}
        self.saved = 0
        self.init = Initializer(self.api, self.spec, self.state, self.checkpoint, self.root)

    def checkpoint(self):
        self.saved += 1

    async def test_owner_is_created_once_and_retry_uses_refresh_token(self):
        class OnboardingAPI(FakeAPI):
            created = False
            async def request(self, method, path, data=None, authenticated=True):
                self.calls.append((method, path, data))
                if path == "/api/onboarding":
                    return [{"step": name, "done": self.created} for name in ("user", "core_config", "analytics", "integration")]
                if path == "/api/onboarding/users":
                    self.created = True
                    return {"auth_code": "test-code"}
                return {}
        self.init.api = api = OnboardingAPI()
        await self.init.authenticate()
        await self.init.authenticate()
        self.assertEqual(sum(path == "/api/onboarding/users" for _, path, _ in api.calls), 1)
        self.assertEqual(self.state["refresh_token"], "fake-refresh")
        self.assertEqual(api.token, "fake-token")

    async def test_preferences_apply_once_and_preserve_later_user_changes(self):
        self.spec["preferences"] = {"core": {"showAdvanced": True}}
        await self.init.preferences()
        await self.init.preferences()
        self.assertEqual(self.api.calls, [("WS", "frontend/set_user_data", {"key": "core", "value": {"showAdvanced": True}})])

    async def test_existing_radio_entry_is_never_recreated_or_reset(self):
        spec = {"key": "radio", "domain": "zha", "steps": {"user": {}}}
        await self.init.integration(spec, [{"entry_id": "existing", "domain": "zha"}], [])
        self.assertEqual(self.state["entries"]["radio"], "existing")
        self.assertEqual(self.api.calls, [])

    async def test_flow_resume_and_unknown_steps_fail_closed(self):
        spec = {"key": "sensor", "domain": "esphome", "steps": {"user": {}}}
        await self.init.integration(spec, [], [])
        self.assertEqual(self.state["entries"]["sensor"], "entry-1")
        self.assertNotIn("sensor", self.state["flows"])
        self.api.flow = {"type": "form", "step_id": "new_radio_reset_prompt", "flow_id": "flow-2"}
        with self.assertRaisesRegex(SetupError, "unrecognized_integration_step"):
            await self.init.integration(spec, [], [])
        self.assertEqual(self.state["flows"]["sensor"], "flow-2")

    async def test_device_identity_wins_over_discovery_order(self):
        self.spec["entities"] = [{"platform": "zha", "unique_id": "hardware-a", "entity_id": "light.example"}]
        rows = [{"platform": "zha", "unique_id": "hardware-b", "entity_id": "light.other"},
                {"platform": "zha", "unique_id": "hardware-a", "entity_id": "light.discovered_2"}]
        await self.init.registries([], rows)
        self.assertEqual(rows[1]["entity_id"], "light.example")
        before = len(self.api.calls)
        await self.init.registries([], rows)
        self.assertEqual(len(self.api.calls), before)
        self.assertTrue(device_matches({"connections": [["mac", "02:00:00:00:00:01"]]},
                                       {"connection": ["mac", "020000000001"]}))

    async def test_user_entity_rename_survives_later_reconciliation(self):
        self.spec["entities"] = [{"platform": "zha", "unique_id": "a", "entity_id": "light.example"}]
        rows = [{"id": "stable-registry-id", "platform": "zha", "unique_id": "a", "entity_id": "light.first"}]
        await self.init.registries([], rows)
        rows[0]["entity_id"] = "light.user_chosen"
        self.api.calls.clear()
        await self.init.registries([], rows)
        self.assertEqual(self.api.calls, [])
        self.assertEqual(rows[0]["entity_id"], "light.user_chosen")

    async def test_matching_discovery_flow_is_resumed_without_competing_flow(self):
        class DiscoveryAPI(FakeAPI):
            async def ws(self, kind, **data):
                if kind == "config_entries/flow/progress":
                    return [{"handler": "esphome", "flow_id": "discovered", "context": {"unique_id": "020000000001"}}]
                return []
        self.init.api = api = DiscoveryAPI()
        spec = {"key": "sensor", "domain": "esphome", "match_device": {"connection": ["mac", "02:00:00:00:00:01"]},
                "steps": {"user": {}}}
        await self.init.integration(spec, [], [])
        self.assertFalse(any(path == "/api/config/config_entries/flow" for _, path, _ in api.calls))
        self.assertTrue(any(path.endswith("/discovered") for _, path, _ in api.calls))
        self.assertEqual(self.state["entries"]["sensor"], "entry-1")

    async def test_collision_does_not_rename_an_unrelated_entity(self):
        self.spec["entities"] = [{"platform": "zha", "unique_id": "a", "entity_id": "light.example"}]
        await self.init.registries([], [{"platform": "zha", "unique_id": "a", "entity_id": "light.other"},
                                        {"platform": "zha", "unique_id": "b", "entity_id": "light.example"}])
        self.assertEqual(self.api.calls, [])
        self.assertEqual(self.init.errors, ["entity_name_collision:0"])

    async def test_automations_wait_for_pairing_then_load_only_once(self):
        (self.root / "source.yaml").write_text("- alias: Example\n")
        (self.root / "automations.yaml").write_text("[]\n")
        self.spec["automations"] = {"source": "source.yaml", "requires": ["light.example"]}
        await self.init.activate([])
        self.assertEqual((self.root / "automations.yaml").read_text(), "[]\n")
        await self.init.activate([{"entity_id": "light.example", "state": "off"}])
        self.assertTrue(self.state["automations_loaded"])
        (self.root / "automations.yaml").write_text("user edit\n")
        await self.init.activate([])
        self.assertEqual((self.root / "automations.yaml").read_text(), "user edit\n")

    async def test_device_settings_are_initial_only(self):
        self.spec["device_settings"] = [{"entity_id": "number.example", "action": "number.set_value", "data": {"value": 20}}]
        states = [{"entity_id": "number.example", "state": "10"}]
        await self.init.device_settings(states)
        await self.init.device_settings(states)
        self.assertEqual(len(self.api.calls), 1)


if __name__ == "__main__":
    unittest.main()
