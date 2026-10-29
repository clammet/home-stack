"""Strict JSON configuration; renderers never interpolate unvalidated shell text."""
import copy
import ipaddress
import json
from pathlib import Path, PurePosixPath
import os

from .config_directory import expand, validate_setup
import re
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULTS = {
    "hostname": "home-stack", "timezone": "Etc/UTC", "image_size_mib": 32768,
    "ssh_authorized_keys": [],
    "network": {"interface": None, "address": None, "gateway": None,
                "host_dns": ["1.1.1.1", "9.9.9.9"]},
    "logging": {"storage": "volatile", "max_level": "crit"},
    "updates": {"container_calendar": "*-*-* 03:00:00", "os_calendar": "Sun *-*-* 04:00:00",
                "random_delay_seconds": 600, "health_timeout_seconds": 600,
                "health_successes": 3, "retry_failed_after_hours": 24},
    "notifications": {"webhook_url": None},
    "apps": [{"name": "pihole", "kind": "pihole"},
             {"name": "homeassistant", "kind": "homeassistant"}],
}
COMMON = {"name": "", "kind": "container", "enabled": True, "auto_update": True,
          "image": "", "environment": {}, "ports": [], "network": "bridge",
          "data_path": "/data", "privileged": False, "health": {}, "settings": {},
          "seed": None, "setup": None}
PRESETS = {
    "pihole": {"image": "docker.io/pihole/pihole:latest", "data_path": "/etc/pihole",
               "settings": {"admin_password": "", "web_port": 80,
                            "upstream_dns": ["1.1.1.1", "9.9.9.9"], "adlists": None,
                            "local_records": [], "dnssec": False, "domain_needed": False,
                            "cache_optimizer": 3600, "web_session_timeout": 1800}},
    "homeassistant": {"image": "ghcr.io/home-assistant/home-assistant:stable",
                      "network": "host", "privileged": True, "data_path": "/config"},
    "mosquitto": {"image": "docker.io/eclipse-mosquitto:2",
                  "data_path": os.path.join(os.sep, "mosquitto"),
                  "settings": {"users": []}},
    "container": {},
}


def merge(base, override, prefix=""):
    if not isinstance(override, dict):
        raise ValueError(f"{prefix or 'config'} must be an object")
    result = copy.deepcopy(base)
    for key, value in override.items():
        if key not in base:
            raise ValueError(f"Unknown setting: {prefix}{key}")
        if isinstance(base[key], dict):
            result[key] = merge(base[key], value, prefix + key + ".")
        else:
            result[key] = value
    return result


def line(value, name, empty=False):
    if not isinstance(value, str) or (not value and not empty) or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError(f"{name} must be a single line of text")
    return value


def integer(value, low, high, name):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{name} must be an integer between {low} and {high}")


def strings(value, name):
    if not isinstance(value, list):
        raise ValueError(f"{name} must be an array")
    for item in value:
        line(item, name)


def ip_list(value, name):
    strings(value, name)
    if not value:
        raise ValueError(f"{name} cannot be empty")
    for item in value:
        address = ipaddress.IPv4Address(item)
        if address.is_loopback or address.is_unspecified or address.is_multicast:
            raise ValueError(f"{name} must use independent unicast DNS servers")


def url(value, name, https=False):
    line(value, name)
    parsed = urlsplit(value)
    if parsed.scheme not in (("https",) if https else ("http", "https")) or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError(f"Invalid {name} URL")
    return parsed


def app_config(raw):
    if not isinstance(raw, dict) or not isinstance(raw.get("kind"), str) or raw["kind"] not in PRESETS:
        raise ValueError("Each app requires kind: pihole, homeassistant, mosquitto, or container")
    kind = raw["kind"]
    base = copy.deepcopy(COMMON)
    base.update(copy.deepcopy(PRESETS[kind]))
    # Environment and generic health have dynamic keys, validated separately.
    dynamic = {k: raw[k] for k in ("environment", "health", "seed", "setup") if k in raw}
    app = merge(base, {k: v for k, v in raw.items() if k not in dynamic}, "apps.")
    app.update(dynamic)
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", line(app["name"], "app name")):
        raise ValueError("Invalid app name")
    for key in ("enabled", "auto_update", "privileged"):
        if type(app[key]) is not bool:
            raise ValueError(f"apps.{key} must be boolean")
    ref = line(app["image"], "image")
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9.:-]*/[a-zA-Z0-9_/.:@-]+", ref) or not ("." in ref.split("/")[0] or ":" in ref.split("/")[0] or ref.startswith("localhost/")):
        raise ValueError("Container image must be a fully qualified registry reference")
    if app["network"] not in ("bridge", "host"):
        raise ValueError("network must be bridge or host")
    path = PurePosixPath(line(app["data_path"], "data_path"))
    if not path.is_absolute() or str(path) == "/" or ".." in path.parts or any(c in str(path) for c in ":, "):
        raise ValueError("data_path must be a container directory")
    if not isinstance(app["environment"], dict):
        raise ValueError("environment must be an object of strings")
    for key, value in app["environment"].items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError("Invalid environment name")
        line(value, "environment value", empty=True)
        if key == "TZ" or (kind == "pihole" and (key.startswith("FTLCONF_") or key == "TAIL_FTL_LOG")):
            raise ValueError("Use timezone/settings for managed environment values")
    strings(app["ports"], "ports")
    for port in app["ports"]:
        match = re.fullmatch(r"(\d+):(\d+)/(tcp|udp)", port)
        if not match or not all(1 <= int(p) <= 65535 for p in match.groups()[:2]):
            raise ValueError("Ports must be HOST:CONTAINER/tcp or HOST:CONTAINER/udp")
    if app["network"] == "host" and app["ports"]:
        raise ValueError("Host networking cannot use published ports")
    if kind != "container":
        # Preset health, mounts and ports are fixed contracts; don't silently ignore custom values.
        for key in ("data_path", "network", "privileged"):
            if app[key] != base[key]:
                raise ValueError(f"{kind}.{key} is fixed by the preset")
        if app["ports"] or app["health"]:
            raise ValueError("Preset ports and health checks are managed; use pihole.settings.web_port")
    if kind == "pihole":
        p = app["settings"]
        if len(line(p["admin_password"], "Pi-hole admin_password")) < 12:
            raise ValueError("Pi-hole admin_password must contain at least 12 characters")
        integer(p["web_port"], 1, 65535, "web_port")
        ip_list(p["upstream_dns"], "upstream_dns")
        for key in ("dnssec", "domain_needed"):
            if type(p[key]) is not bool:
                raise ValueError(f"pihole.{key} must be boolean")
        integer(p["cache_optimizer"], -1, 86400, "pihole.cache_optimizer")
        integer(p["web_session_timeout"], 1, 31536000, "pihole.web_session_timeout")
        if p["adlists"] is not None:
            strings(p["adlists"], "adlists")
            for item in p["adlists"]:
                url(item, "adlist", https=True)
        strings(p["local_records"], "local_records")
        for record in p["local_records"]:
            fields = record.split()
            if len(fields) < 2:
                raise ValueError("local_records require IP HOSTNAME [HOSTNAME ...]")
            ipaddress.ip_address(fields[0])
            if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", host) or host.lower().rstrip(".") == "home-stack-health.test" for host in fields[1:]):
                raise ValueError("Invalid/reserved local record hostname")
    if kind == "container":
        h = app["health"]
        if not isinstance(h, dict):
            raise ValueError("health must be an object")
        if h.get("kind") == "http" and set(h) == {"kind", "url"}:
            parsed = url(h["url"], "health")
            if parsed.hostname != "127.0.0.1":
                raise ValueError("HTTP health must check 127.0.0.1 on a host-accessible port")
        elif h.get("kind") == "tcp" and set(h) == {"kind", "port"}:
            integer(h["port"], 1, 65535, "health.port")
        else:
            raise ValueError("Generic container requires HTTP url or TCP port health check")
    if app["kind"] == "mosquitto":
        users = app["settings"]["users"]
        if not isinstance(users, list) or not users:
            raise ValueError("Mosquitto requires at least one password-authenticated user")
        names = set()
        for user in users:
            if not isinstance(user, dict) or set(user) != {"username", "password"}:
                raise ValueError("Mosquitto users require username and password")
            name = line(user["username"], "MQTT username")
            line(user["password"], "MQTT password")
            if name in names or ":" in name:
                raise ValueError("MQTT usernames must be unique and passwords nonempty")
            names.add(name)
    if app["seed"] is not None:
        from .config_directory import seed_files
        seed_files(Path.cwd(), app["seed"])
    if app["setup"] is not None:
        if kind != "homeassistant":
            raise ValueError("Only Home Assistant supports setup")
        validate_setup(app["setup"])
        automation = app["setup"].get("automations")
        if automation and automation["source"] not in (app.get("seed") or {}).get("files", {}):
            raise ValueError("Automation activation source is missing from the seed")
    return app


def load(path=None, overrides=()):
    raw = json.loads(path.read_text()) if path else {}
    if not isinstance(raw, dict):
        raise ValueError("Configuration must be an object")
    for override in overrides:
        key, sep, value = override.partition("=")
        if not sep:
            raise ValueError("Override must be KEY=JSON")
        node = raw
        parts = key.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
            if not isinstance(node, dict):
                raise ValueError("Override path must traverse objects")
        node[parts[-1]] = json.loads(value)
    raw = expand(raw, path.parent if path else Path.cwd())
    config = merge(DEFAULTS, raw)
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", line(config["hostname"], "hostname")):
        raise ValueError("Invalid hostname")
    try:
        ZoneInfo(line(config["timezone"], "timezone"))
    except (ValueError, ZoneInfoNotFoundError) as error:
        raise ValueError("Unknown IANA timezone") from error
    integer(config["image_size_mib"], 16384, 1048576, "image_size_mib")
    strings(config["ssh_authorized_keys"], "ssh_authorized_keys")
    for key in config["ssh_authorized_keys"]:
        if not re.fullmatch(r"(?:ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(?:256|384|521)) [A-Za-z0-9+/]+={0,3}(?: .*)?", key):
            raise ValueError("Expected an OpenSSH public key without options")
    n = config["network"]
    if n["interface"] is not None and not re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", line(n["interface"], "interface")):
        raise ValueError("Invalid interface")
    if (n["address"] is None) != (n["gateway"] is None):
        raise ValueError("Static address and gateway must be specified together")
    if n["address"] is not None:
        interface = ipaddress.IPv4Interface(n["address"])
        gateway = ipaddress.IPv4Address(n["gateway"])
        if gateway not in interface.network or gateway == interface.ip:
            raise ValueError("Gateway must be a different address in the local subnet")
    ip_list(n["host_dns"], "host_dns")
    if n["address"] and str(interface.ip) in n["host_dns"]:
        raise ValueError("Host DNS must not depend on this Pi")
    if config["logging"]["storage"] not in ("volatile", "none") or config["logging"]["max_level"] not in ("emerg", "alert", "crit", "err", "warning", "notice", "info", "debug"):
        raise ValueError("Invalid logging policy")
    u = config["updates"]
    for key in ("container_calendar", "os_calendar"):
        if u[key] is not None:
            line(u[key], key)
            if "%" in u[key]:
                raise ValueError("Calendar cannot contain systemd specifiers")
    for key, low, high in (("random_delay_seconds", 0, 86400), ("health_timeout_seconds", 30, 3600),
                           ("health_successes", 1, 20), ("retry_failed_after_hours", 1, 8760)):
        integer(u[key], low, high, key)
    if config["notifications"]["webhook_url"]:
        url(config["notifications"]["webhook_url"], "webhook", https=True)
    elif config["notifications"]["webhook_url"] is not None:
        raise ValueError("webhook_url must be an HTTPS URL or null")
    if not isinstance(config["apps"], list) or not config["apps"]:
        raise ValueError("apps must be a nonempty array")
    config["apps"] = [app_config(app) for app in config["apps"]]
    names, presets, ports = set(), set(), set()
    for app in config["apps"]:
        if app["name"] in names:
            raise ValueError("App names must be unique")
        names.add(app["name"])
        if not app["enabled"]:
            continue
        kind = app["kind"]
        if kind != "container":
            if kind in presets:
                raise ValueError("Only one enabled instance of each preset is supported")
            presets.add(kind)
        bindings = app["ports"]
        if kind == "pihole":
            bindings = ["53:53/tcp", "53:53/udp", f"{app['settings']['web_port']}:80/tcp"]
        if kind == "mosquitto":
            bindings = ["1883:1883/tcp"]
        if kind == "homeassistant":
            bindings = ["8123:8123/tcp"]
        for binding in bindings:
            host, _, protocol = re.split(r"[:/]", binding)
            pair = (int(host), protocol)
            if pair in ports or (config["ssh_authorized_keys"] and pair == (22, "tcp")):
                raise ValueError("Conflicting host port")
            ports.add(pair)
    if not any(app["enabled"] for app in config["apps"]):
        raise ValueError("At least one app must be enabled")
    return config
