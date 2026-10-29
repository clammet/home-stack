"""Resumable first-install setup, executed inside the HA container (aiohttp included).

Input is private JSON on stdin; stdout contains only sanitized progress. State and
authentication stay in this installation's configuration directory, never in Git.
"""
import asyncio
import json
import os
from pathlib import Path
import shutil
import sys

BASE = "http://127.0.0.1:8123"
CLIENT = BASE + "/"


class SetupError(Exception):
    """Only fixed, nonsecret error codes may be reported outside this module."""


class Waiting(Exception):
    pass


def save(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as stream:
        os.chmod(temporary, 0o600)
        json.dump(state, stream)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


class API:
    def __init__(self, session):
        self.session = session
        self.token = None
        self.socket = None
        self.ident = 0

    async def request(self, method, path, data=None, authenticated=True):
        headers = {"Authorization": "Bearer " + self.token} if authenticated and self.token else {}
        try:
            async with self.session.request(method, BASE + path, json=data, headers=headers) as response:
                if response.status not in (200, 201):
                    raise SetupError("http_request_failed")
                return await response.json()
        except SetupError:
            raise
        except Exception:
            raise SetupError("api_unavailable") from None

    async def exchange(self, data):
        try:
            async with self.session.post(BASE + "/auth/token", data=data) as response:
                if response.status != 200:
                    raise SetupError("authentication_failed")
                return await response.json()
        except SetupError:
            raise
        except Exception:
            raise SetupError("api_unavailable") from None

    async def ws(self, kind, **data):
        try:
            if self.socket is None:
                self.socket = await self.session.ws_connect(BASE.replace("http:", "ws:") + "/api/websocket", receive_timeout=15)
                hello = await self.socket.receive_json()
                if hello.get("type") != "auth_required":
                    raise SetupError("websocket_handshake_failed")
                await self.socket.send_json({"type": "auth", "access_token": self.token})
                if (await self.socket.receive_json()).get("type") != "auth_ok":
                    raise SetupError("authentication_failed")
            self.ident += 1
            await self.socket.send_json({"id": self.ident, "type": kind, **data})
            while True:
                result = await self.socket.receive_json()
                if result.get("id") != self.ident:
                    continue
                if not result.get("success"):
                    raise SetupError("registry_request_failed")
                return result.get("result")
        except SetupError:
            raise
        except Exception:
            raise SetupError("websocket_unavailable") from None


def normalized(value):
    return str(value).lower().replace(":", "").replace("-", "")


def device_matches(device, matcher):
    for field, key in (("identifier", "identifiers"), ("connection", "connections")):
        if field in matcher:
            namespace, value = matcher[field]
            return any(kind == namespace and normalized(candidate) == normalized(value)
                       for kind, candidate in device.get(key, []))
    return False


class Initializer:
    def __init__(self, api, specification, state, checkpoint, directory):
        self.api, self.spec = api, specification
        self.state, self.checkpoint = state, checkpoint
        self.directory = directory
        self.pending, self.errors = [], []
        for key in ("entries", "flows", "areas", "floors", "devices", "entities", "settings", "options", "preferences"):
            self.state.setdefault(key, {})

    async def authenticate(self):
        onboarding = await self.api.request("GET", "/api/onboarding", authenticated=False)
        steps = {step["step"]: step["done"] for step in onboarding}
        if not steps.get("user", True):
            owner = self.spec["owner"]
            result = await self.api.request("POST", "/api/onboarding/users",
                                            dict(owner, client_id=CLIENT), authenticated=False)
            tokens = await self.api.exchange({"grant_type": "authorization_code",
                                              "code": result["auth_code"], "client_id": CLIENT})
            self.state["refresh_token"] = tokens["refresh_token"]
            self.checkpoint()
        elif self.state.get("refresh_token"):
            tokens = await self.api.exchange({"grant_type": "refresh_token",
                                              "refresh_token": self.state["refresh_token"], "client_id": CLIENT})
        else:
            # Recover a lost bootstrap checkpoint through the real auth flow, never
            # by modifying the account store or creating a second owner.
            flow = await self.api.request("POST", "/auth/login_flow",
                                          {"client_id": CLIENT, "handler": ["homeassistant", None],
                                           "redirect_uri": CLIENT}, authenticated=False)
            result = await self.api.request("POST", "/auth/login_flow/" + flow["flow_id"],
                                            {"client_id": CLIENT, "username": self.spec["owner"]["username"],
                                             "password": self.spec["owner"]["password"]}, authenticated=False)
            if result.get("type") != "create_entry":
                raise SetupError("owner_login_required")
            tokens = await self.api.exchange({"grant_type": "authorization_code", "code": result["result"],
                                              "client_id": CLIENT})
            self.state["refresh_token"] = tokens["refresh_token"]
            self.checkpoint()
        self.api.token = tokens["access_token"]
        if not steps.get("core_config", True):
            await self.api.request("POST", "/api/onboarding/core_config", {})
        if not steps.get("analytics", True):
            await self.api.request("POST", "/api/onboarding/analytics", {})
        if not steps.get("integration", True):
            await self.api.request("POST", "/api/onboarding/integration",
                                   {"client_id": CLIENT, "redirect_uri": CLIENT})

    async def integration(self, item, entries, devices):
        key, domain = item["key"], item["domain"]
        entry_id = self.state["entries"].get(key)
        candidates = [entry for entry in entries if entry["domain"] == domain]
        if entry_id and any(entry["entry_id"] == entry_id for entry in candidates):
            return
        if "match_device" in item:
            matching_ids = {entry for device in devices if device_matches(device, item["match_device"])
                            for entry in device.get("config_entries", [])}
            candidates = [entry for entry in candidates if entry["entry_id"] in matching_ids]
        elif len(candidates) > 1:
            raise SetupError("ambiguous_integration")
        if len(candidates) == 1:
            self.state["entries"][key] = candidates[0]["entry_id"]
            self.state["flows"].pop(key, None)
            self.checkpoint()
            return
        flow_id = self.state["flows"].get(key)
        if flow_id:
            try:
                flow = await self.api.request("GET", "/api/config/config_entries/flow/" + flow_id)
            except SetupError:
                self.state["flows"].pop(key, None)
                self.checkpoint()
                flow_id = None
        if not flow_id:
            # USB/zeroconf may have already started the same integration's flow.
            # Reuse only an unambiguous singleton or an exact device/host match.
            progress = await self.api.ws("config_entries/flow/progress")
            matches = []
            for candidate in progress:
                handler = candidate.get("handler")
                if handler != domain:
                    continue
                context = candidate.get("context", {})
                if "match_device" in item:
                    pair = item["match_device"].get("connection") or item["match_device"].get("identifier")
                    unique = context.get("unique_id")
                    host = item["steps"].get("user", {}).get("host")
                    announced_host = context.get("title_placeholders", {}).get("host")
                    if not ((pair and unique and normalized(pair[1]) == normalized(unique)) or
                            (host and host == announced_host)):
                        continue
                matches.append(candidate)
            if len(matches) == 1:
                flow_id = matches[0]["flow_id"]
                flow = await self.api.request("GET", "/api/config/config_entries/flow/" + flow_id)
        if not flow_id:
            flow = await self.api.request("POST", "/api/config/config_entries/flow",
                                          {"handler": domain, "show_advanced_options": True})
        for _ in range(20):
            if flow.get("flow_id"):
                self.state["flows"][key] = flow["flow_id"]
                self.checkpoint()
            kind, step = flow.get("type"), flow.get("step_id")
            if kind == "create_entry":
                result = flow.get("result") or {}
                if isinstance(result, dict) and result.get("entry_id"):
                    self.state["entries"][key] = result["entry_id"]
                self.state["flows"].pop(key, None)
                self.checkpoint()
                return
            if kind == "abort":
                self.state["flows"].pop(key, None)
                self.checkpoint()
                if flow.get("reason") in ("already_configured", "single_instance_allowed"):
                    return
                if flow.get("reason") == "already_in_progress":
                    raise Waiting()
                raise SetupError("integration_flow_aborted")
            if flow.get("errors"):
                raise Waiting()
            path = "/api/config/config_entries/flow/" + flow["flow_id"]
            if kind in ("show_progress", "show_progress_done"):
                await asyncio.sleep(0.2)
                flow = await self.api.request("GET", path)
                continue
            if step not in item["steps"]:
                # Stop on new/unexpected UI steps instead of guessing (especially
                # important for radio reset and backup/migration choices).
                raise SetupError("unrecognized_integration_step")
            submission = item["steps"][step]
            if isinstance(submission, dict) and set(submission) == {"serial_by_id"}:
                serial = str(Path(os.path.sep) / "dev" / "serial" / "by-id" / submission["serial_by_id"])
                if step == "choose_serial_port":
                    # Older HA lists display strings plus a manual option; newer
                    # serial-port selectors accept the actual stable device path.
                    manual = '"manual"' in json.dumps(flow.get("data_schema", []))
                    submission = {"path": "manual" if manual else serial}
                else:
                    submission = {"path": serial, "baudrate": 115200, "flow_control": "hardware"}
            if isinstance(submission, dict) and set(submission) == {"radio_type_name"}:
                from zha.application.const import RadioType
                submission = {"radio_type": next(radio.description for radio in RadioType
                                                   if radio.name == submission["radio_type_name"])}
            flow = await self.api.request("POST", path, submission)
        raise Waiting()

    async def options(self, item):
        key = item["key"]
        if not item.get("options_steps") or self.state["options"].get(key):
            return
        entry_id = self.state["entries"].get(key)
        if not entry_id:
            raise Waiting()
        flow_key = key + ":options"
        flow_id = self.state["flows"].get(flow_key)
        if flow_id:
            try:
                flow = await self.api.request("GET", "/api/config/config_entries/options/flow/" + flow_id)
            except SetupError:
                flow_id = None
        if not flow_id:
            flow = await self.api.request("POST", "/api/config/config_entries/options/flow", {"handler": entry_id})
        for _ in range(8):
            if flow.get("flow_id"):
                self.state["flows"][flow_key] = flow["flow_id"]
                self.checkpoint()
            if flow.get("type") == "create_entry":
                self.state["options"][key] = True
                self.state["flows"].pop(flow_key, None)
                self.checkpoint()
                return
            step = flow.get("step_id")
            if flow.get("errors"):
                raise Waiting()
            if step not in item["options_steps"]:
                raise SetupError("unrecognized_options_step")
            flow = await self.api.request("POST", "/api/config/config_entries/options/flow/" + flow["flow_id"],
                                          item["options_steps"][step])
        raise Waiting()

    async def preferences(self):
        for key, value in self.spec.get("preferences", {}).items():
            if self.state["preferences"].get(key):
                continue
            await self.api.ws("frontend/set_user_data", key=key, value=value)
            self.state["preferences"][key] = True
            self.checkpoint()

    async def rooms(self, kind):
        current = await self.api.ws("config/" + kind + "_registry/list")
        for item in self.spec.get(kind + "s", []):
            key = item["key"]
            if key in self.state[kind + "s"]:
                continue
            record = next((row for row in current if row["name"] == item["name"]), None)
            values = {name: value for name, value in item.items() if name != "key"}
            if kind == "area" and "floor_id" in values:
                values["floor_id"] = self.state["floors"].get(values["floor_id"])
            if record:
                ident = record[kind + "_id"]
                await self.api.ws("config/" + kind + "_registry/update", **{kind + "_id": ident}, **values)
            else:
                record = await self.api.ws("config/" + kind + "_registry/create", **values)
                ident = record[kind + "_id"]
                current.append(record)
            self.state[kind + "s"][key] = ident
            self.checkpoint()

    async def registries(self, devices, entities):
        for index, item in enumerate(self.spec.get("devices", [])):
            key = str(index)
            if "entry" in item["match"]:
                entry_id = self.state["entries"].get(item["match"]["entry"])
                record = next((row for row in devices if entry_id and entry_id in row.get("config_entries", [])), None)
            else:
                record = next((row for row in devices if device_matches(row, item["match"])), None)
            if not record:
                if item.get("required", True):
                    self.pending.append("device:" + str(index))
                continue
            if self.state["devices"].get(key) == record["id"]:
                continue
            values = {"device_id": record["id"]}
            if "area" in item:
                values["area_id"] = self.state["areas"].get(item["area"])
            if "name" in item:
                values["name_by_user"] = item["name"]
            await self.api.ws("config/device_registry/update", **values)
            self.state["devices"][key] = record["id"]
            self.checkpoint()
        for index, item in enumerate(self.spec.get("entities", [])):
            key = str(index)
            record = next((row for row in entities if row["platform"] == item["platform"]
                           and row["unique_id"] == item["unique_id"]), None)
            if not record:
                if item.get("required", False):
                    self.pending.append("entity:" + str(index))
                continue
            identity = record.get("id", record["platform"] + ":" + record["unique_id"])
            applied = self.state["entities"].get(key)
            if isinstance(applied, dict) and applied.get("id") == identity:
                continue
            target = item["entity_id"]
            if any(row is not record and row["entity_id"] == target for row in entities):
                self.errors.append("entity_name_collision:" + str(index))
                continue
            values = {"entity_id": record["entity_id"]}
            if target != record["entity_id"]:
                values["new_entity_id"] = target
            for field in ("name", "icon", "disabled_by"):
                if field in item:
                    values[field] = item[field]
            if "area" in item:
                values["area_id"] = self.state["areas"].get(item["area"])
            await self.api.ws("config/entity_registry/update", **values)
            record["entity_id"] = target
            self.state["entities"][key] = {"id": identity, "entity_id": target}
            self.checkpoint()

    async def device_settings(self, states):
        available = {row["entity_id"] for row in states if row["state"] not in ("unknown", "unavailable")}
        for index, item in enumerate(self.spec.get("device_settings", [])):
            key = str(index)
            if self.state["settings"].get(key):
                continue
            if item["entity_id"] not in available:
                self.pending.append("device_setting:" + str(index))
                continue
            domain, service = item["action"].split(".")
            await self.api.request("POST", "/api/services/" + domain + "/" + service,
                                   dict(item.get("data", {}), entity_id=item["entity_id"]))
            self.state["settings"][key] = True
            self.checkpoint()

    async def activate(self, states):
        settings = self.spec.get("automations")
        if not settings or self.state.get("automations_loaded"):
            return
        available = {row["entity_id"] for row in states if row["state"] not in ("unknown", "unavailable")}
        missing = set(settings["requires"]) - available
        if missing or self.errors:
            self.pending.append("automations:waiting_for_devices")
            return
        source = self.directory / settings["source"]
        target = self.directory / "automations.yaml"
        if not source.is_file():
            raise SetupError("automation_seed_missing")
        # Refuse to overwrite user-created automations while waiting for pairing.
        if target.exists() and target.read_text().strip() not in ("", "[]", source.read_text().strip()):
            raise SetupError("automation_file_changed_before_initialization")
        temporary = target.with_suffix(".tmp")
        shutil.copyfile(source, temporary)
        os.chmod(temporary, 0o600)
        temporary.replace(target)
        await self.api.request("POST", "/api/services/automation/reload", {})
        self.state["automations_loaded"] = True
        self.checkpoint()

    async def run(self):
        await self.authenticate()
        await self.preferences()
        await self.rooms("floor")
        await self.rooms("area")
        entries = await self.api.request("GET", "/api/config/config_entries/entry")
        devices = await self.api.ws("config/device_registry/list")
        for item in self.spec.get("integrations", []):
            try:
                await self.integration(item, entries, devices)
                await self.options(item)
            except Waiting:
                self.pending.append("integration:" + item["key"])
            except SetupError as error:
                self.errors.append("integration:" + item["key"] + ":" + str(error))
        devices = await self.api.ws("config/device_registry/list")
        entities = await self.api.ws("config/entity_registry/list")
        await self.registries(devices, entities)
        states = await self.api.request("GET", "/api/states")
        await self.device_settings(states)
        await self.activate(states)
        return {"status": "error" if self.errors else ("waiting" if self.pending else "complete"),
                "pending": self.pending, "errors": self.errors}


async def main():
    import aiohttp
    os.umask(0o077)
    directory = Path(os.path.sep) / "config"
    state_path = directory / ".home-stack-setup.json"
    specification = json.load(sys.stdin)
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            initializer = Initializer(API(session), specification, state,
                                      lambda: save(state_path, state), directory)
            result = await initializer.run()
    except SetupError as error:
        result = {"status": "error", "pending": [], "errors": [str(error)]}
    except Exception:
        result = {"status": "error", "pending": [], "errors": ["initialization_failed"]}
    print(json.dumps(result))


if __name__ == "__main__":
    asyncio.run(main())
