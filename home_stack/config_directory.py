"""Expand a portable configuration directory without copying its Git checkout."""
import base64
import copy
import json
import re
from pathlib import Path, PurePosixPath

MAX_FILE = 8 * 1024 * 1024
MAX_SEED = 32 * 1024 * 1024


def relative(value):
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError("Expected a relative configuration filename")
    path = PurePosixPath(value)
    if path.is_absolute() or path.as_posix() != value or any(part in (".", "..") for part in value.split("/")):
        raise ValueError("Configuration paths must stay inside the configuration directory")
    if any(ord(char) < 32 for char in value):
        raise ValueError("Invalid configuration filename")
    return path


def contained(root, value, secret=False):
    path = relative(value)
    if secret and (path.parts[0] != "secrets" or len(path.parts) < 2):
        raise ValueError("secret_file references must point inside secrets/")
    candidate = root
    for part in path.parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise ValueError("Configuration inputs cannot contain symlinks")
    return candidate


def read_file(root, value, secret=False):
    path = contained(root, value, secret)
    try:
        if not path.is_file() or path.stat().st_size > MAX_FILE:
            raise ValueError("Configuration input is missing, not a regular file, or too large")
        return path.read_bytes()
    except OSError:
        raise ValueError("Cannot read a configuration input") from None


def resolve(root, value):
    if isinstance(value, dict):
        if set(value) == {"secret_file"}:
            try:
                return read_file(root, value["secret_file"], secret=True).decode().rstrip("\r\n")
            except UnicodeError:
                raise ValueError("Secret text must be UTF-8") from None
        return {key: resolve(root, item) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve(root, item) for item in value]
    return value


def seed_name(name):
    path = relative(name)
    if any(part.startswith(".") for part in path.parts) or any(
        part.lower().endswith((".db", ".db-wal", ".db-shm", ".db-journal", ".sqlite", ".sqlite3"))
        for part in path.parts
    ):
        raise ValueError("Seeds cannot contain hidden state, Git files, or databases")
    return path


def seed_files(root, specification):
    if specification is None:
        return {}
    # Resolved configurations also pass through the Linux builder after macOS transfer.
    if isinstance(specification, dict) and set(specification) == {"files"}:
        files = specification["files"]
        if not isinstance(files, dict):
            raise ValueError("Invalid compiled seed")
        total = 0
        for name, value in files.items():
            seed_name(name)
            try:
                data = base64.b64decode(value, validate=True)
            except (ValueError, TypeError):
                raise ValueError("Invalid compiled seed encoding") from None
            if len(data) > MAX_FILE:
                raise ValueError("Seed file too large")
            total += len(data)
        if total > MAX_SEED:
            raise ValueError("Seed too large")
        return files
    if isinstance(specification, str):
        specification = {"directory": specification}
    if not isinstance(specification, dict) or "directory" not in specification or set(specification) - {"directory", "private_files"}:
        raise ValueError("seed requires directory and optional private_files")
    files = {}
    if relative(specification["directory"]).parts[0] == "secrets":
        raise ValueError("Use private_files to copy explicitly named secret seed files")
    directory = contained(root, specification["directory"])
    if not directory.is_dir():
        raise ValueError("Seed directory does not exist")
    for path in sorted(directory.rglob("*")):
        name = path.relative_to(directory).as_posix()
        seed_name(name)
        if path.is_symlink() or (not path.is_dir() and not path.is_file()):
            raise ValueError("Seed inputs must be regular files/directories")
        if path.is_file():
            files[name] = base64.b64encode(read_file(root, path.relative_to(root).as_posix())).decode()
    private_files = specification.get("private_files", {})
    if not isinstance(private_files, dict):
        raise ValueError("seed.private_files must be a filename mapping")
    for name, source in private_files.items():
        seed_name(name)
        if name in files:
            raise ValueError("Private seed file collides with a public seed file")
        files[name] = base64.b64encode(read_file(root, source, secret=True)).decode()
    return seed_files(root, {"files": files})


def expand(raw, root):
    raw = copy.deepcopy(raw)
    for app in raw.get("apps", []):
        if not isinstance(app, dict):
            continue
        if app.get("seed") is not None:
            app["seed"] = {"files": seed_files(root, app["seed"])}
        if isinstance(app.get("setup"), str):
            try:
                app["setup"] = json.loads(read_file(root, app["setup"]))
            except (ValueError, UnicodeError):
                raise ValueError("Cannot parse setup manifest") from None
    return resolve(root, raw)


def validate_setup(setup):
    if setup is None:
        return
    allowed = {"version", "owner", "integrations", "floors", "areas", "devices", "entities",
               "device_settings", "automations", "preferences"}
    if not isinstance(setup, dict) or set(setup) - allowed or type(setup.get("version")) is not int or setup.get("version") != 1:
        raise ValueError("Home Assistant setup requires version 1 and recognized fields")
    owner = setup.get("owner", {})
    if not isinstance(owner, dict) or set(owner) != {"name", "username", "password", "language"} or not all(
        isinstance(value, str) and value for value in owner.values()
    ) or len(owner["password"]) < 12:
        raise ValueError("Home Assistant owner requires name, username, language, and a private password of at least 12 characters")
    for field in ("integrations", "floors", "areas", "devices", "entities", "device_settings"):
        if not isinstance(setup.get(field, []), list) or not all(isinstance(item, dict) for item in setup.get(field, [])):
            raise ValueError(f"setup.{field} must be an array of objects")
    if not isinstance(setup.get("preferences", {}), dict):
        raise ValueError("setup.preferences must be an object")
    keys = set()
    for item in setup.get("integrations", []):
        if set(item) - {"key", "domain", "match_device", "steps", "options_steps"} or not all(
            isinstance(item.get(key), str) and item[key] for key in ("key", "domain")
        ) or not isinstance(item.get("steps"), dict):
            raise ValueError("Invalid integration setup flow")
        if item["key"] in keys:
            raise ValueError("Integration setup keys must be unique")
        keys.add(item["key"])
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", item["key"]) or not re.fullmatch(r"[a-z_]+", item["domain"]):
            raise ValueError("Invalid integration key/domain")
        for step_mapping in (item["steps"], item.get("options_steps", {})):
            if not isinstance(step_mapping, dict) or not all(isinstance(k, str) and isinstance(v, dict) for k, v in step_mapping.items()):
                raise ValueError("Integration steps must map step names to objects")
            for submission in step_mapping.values():
                if "serial_by_id" in submission and len(relative(submission["serial_by_id"]).parts) != 1:
                    raise ValueError("serial_by_id must be a device basename")
        matcher = item.get("match_device")
        if matcher is not None:
            validate_matcher(matcher, keys, allow_entry=False)
    for kind in ("floors", "areas"):
        room_keys = set()
        for room in setup.get(kind, []):
            if not isinstance(room.get("key"), str) or not room["key"] or not isinstance(room.get("name"), str) or not room["name"] or room["key"] in room_keys:
                raise ValueError("Rooms need unique keys and nonempty names")
            room_keys.add(room["key"])
    for item in setup.get("devices", []):
        if not isinstance(item.get("match"), dict) or set(item["match"]) not in ({"identifier"}, {"connection"}, {"entry"}):
            raise ValueError("Devices need exactly one identifier, connection, or entry matcher")
        validate_matcher(item["match"], keys)
    for item in setup.get("entities", []):
        if not all(isinstance(item.get(key), str) and item[key] for key in ("platform", "unique_id", "entity_id")):
            raise ValueError("Entities need platform, unique_id, and entity_id")
        if not re.fullmatch(r"[a-z_]+\.[a-z0-9_]+", item["entity_id"]):
            raise ValueError("Invalid entity_id")
    for item in setup.get("device_settings", []):
        if item.get("action") not in ("number.set_value", "select.select_option", "switch.turn_on", "switch.turn_off"):
            raise ValueError("Unsupported initial device-setting action")
        if not isinstance(item.get("entity_id"), str) or not isinstance(item.get("data", {}), dict):
            raise ValueError("Invalid initial device setting")
    automation = setup.get("automations")
    if automation is not None:
        if set(automation) != {"source", "requires"} or not isinstance(automation["requires"], list):
            raise ValueError("automations needs source and requires")
        seed_name(automation["source"])
        if not all(isinstance(name, str) and re.fullmatch(r"[a-z_]+\.[a-z0-9_]+", name) for name in automation["requires"]):
            raise ValueError("automations.requires must contain entity IDs")


def validate_matcher(matcher, entries, allow_entry=True):
    if not isinstance(matcher, dict) or len(matcher) != 1:
        raise ValueError("Invalid device matcher")
    kind = next(iter(matcher))
    if kind == "entry" and allow_entry:
        if matcher[kind] not in entries:
            raise ValueError("Device matcher refers to an unknown integration key")
    elif kind in ("identifier", "connection"):
        pair = matcher[kind]
        if not isinstance(pair, list) or len(pair) != 2 or not all(isinstance(value, str) and value for value in pair):
            raise ValueError("Device matchers need a namespace and identity")
    else:
        raise ValueError("Invalid device matcher kind")
