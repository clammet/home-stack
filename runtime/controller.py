#!/usr/bin/env python3
"""Crash-recoverable multi-container updates. Uses only the Python standard library."""
import argparse
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import secrets
import socket
import struct
import subprocess
import time
import urllib.request
from urllib.parse import urlsplit
import uuid

if __package__:
    from .images import deployment_name, image_references
else:
    from images import deployment_name, image_references

ROOT = Path("/var/lib/home-stack")
CONFIG = Path("/etc/home-stack/config.json")
CANARY = "home-stack-health.test"
CANARY_IP = "192.0.2.1"


def image_ids(image):
    return set(image.values()) if isinstance(image, dict) else {image}


def image_key(image):
    if isinstance(image, dict):
        return "group:" + hashlib.sha256(json.dumps(image, sort_keys=True).encode()).hexdigest()
    return image


def command(*args, check=True, timeout=900):
    result = subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    if check and result.returncode:
        # Arguments can include paths to secrets, but never secret values.
        raise RuntimeError(f"{args[0]} failed (exit {result.returncode})")
    return result.stdout.strip() if check else result.returncode


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w") as stream:
        os.chmod(tmp, 0o600)
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def skip_name(data, offset):
    for _ in range(128):
        if offset >= len(data):
            raise ValueError("Truncated DNS name")
        size = data[offset]
        if size & 0xC0 == 0xC0:
            if offset + 1 >= len(data):
                raise ValueError("Truncated DNS pointer")
            return offset + 2
        if size & 0xC0:
            raise ValueError("Invalid DNS label")
        offset += 1
        if not size:
            return offset
        offset += size
    raise ValueError("Invalid DNS name")


def dns_answers(data, ident):
    if len(data) < 12:
        raise ValueError("Truncated DNS header")
    rid, flags, questions, answers, _, _ = struct.unpack("!6H", data[:12])
    if rid != ident or not flags & 0x8000 or flags & 0x020F or questions != 1:
        raise ValueError("Invalid, truncated, or failed DNS response")
    offset = skip_name(data, 12) + 4
    addresses = []
    for _ in range(answers):
        offset = skip_name(data, offset)
        kind, cls, _, size = struct.unpack("!HHIH", data[offset:offset + 10])
        offset += 10
        payload = data[offset:offset + size]
        if len(payload) != size:
            raise ValueError("Truncated DNS answer")
        if kind == 1 and cls == 1 and size == 4:
            addresses.append(socket.inet_ntoa(payload))
        offset += size
    return addresses


def receive(sock, size):
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ValueError("Truncated TCP DNS response")
        data += chunk
    return data


def dns_probe(tcp=False, domain=CANARY):
    ident = int.from_bytes(os.urandom(2), "big")
    name = b"".join(bytes([len(p)]) + p.encode() for p in domain.split(".")) + b"\0"
    packet = struct.pack("!6H", ident, 0x100, 1, 0, 0, 0) + name + struct.pack("!HH", 1, 1)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM if tcp else socket.SOCK_DGRAM) as sock:
        sock.settimeout(3)
        sock.connect(("127.0.0.1", 53))
        if tcp:
            sock.sendall(struct.pack("!H", len(packet)) + packet)
            data = receive(sock, struct.unpack("!H", receive(sock, 2))[0])
        else:
            sock.send(packet)
            data = sock.recv(4096)
    return CANARY_IP in dns_answers(data, ident)


class App:
    def __init__(self, config, app, root=ROOT):
        self.config, self.app = config, app
        self.name = "home-stack-" + app["name"]
        self.root = root / app["name"]
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "state.json"
        self.state = json.loads(self.path.read_text()) if self.path.exists() else {
            "active": None, "previous": None, "pending": None, "failed": {}, "images": []}
        self.saved = json.dumps(self.state, sort_keys=True) if self.path.exists() else None

    def save(self):
        value = json.dumps(self.state, sort_keys=True)
        if value != self.saved:
            atomic_json(self.path, self.state)
            self.saved = value

    def data(self, generation):
        import re
        if not re.fullmatch(r"gen-[a-f0-9]{32}", generation["data"]):
            raise ValueError("Invalid generation path")
        path = self.root / generation["data"]
        if path.is_symlink():
            raise ValueError("Generation cannot be a symlink")
        return path

    def inspect(self, name=None):
        try:
            return json.loads(command("podman", "container", "inspect", name or self.name, timeout=30))[0]
        except (RuntimeError, ValueError, IndexError):
            return None

    def matches(self, generation):
        info = self.inspect()
        return bool(info and info["State"]["Running"] and info["Image"] == generation["image"]
                    and any(m.get("Source") == str(self.data(generation)) and m.get("Destination") == self.app["data_path"] for m in info.get("Mounts", [])))

    def stop(self):
        if self.inspect():
            command("podman", "stop", "--time", "60", self.name, timeout=90)

    def remove(self):
        command("podman", "rm", "--force", "--ignore", self.name, timeout=90)

    def start(self, generation):
        directory = self.data(generation)
        if not directory.is_dir():
            raise RuntimeError("Generation data missing; refusing an empty replacement")
        command("podman", "rm", "--force", "--ignore", self.name, timeout=90)
        a = self.app
        args = ["--network", a["network"], "--volume", f"{directory}:{a['data_path']}:Z",
                "--env-file", str(CONFIG.parent / (a["name"] + ".env"))]
        if a["privileged"]:
            args.append("--privileged")
        ports = a["ports"]
        if a["kind"] == "pihole":
            ports = ["53:53/udp", "53:53/tcp", f"{a['settings']['web_port']}:80/tcp"]
            for path, options in (("/var/log", "size=16m,mode=0755"), ("/run", "size=16m,mode=0755"),
                                  ("/tmp", "size=128m,mode=1777"), ("/var/tmp", "size=16m,mode=1777")):
                args.extend(("--tmpfs", f"{path}:rw,nosuid,nodev,{options}"))
        elif a["kind"] == "mosquitto":
            ports = ["1883:1883/tcp"]
        elif a["kind"] == "homeassistant":
            # Host D-Bus must not be relabelled; the upstream container requires host privileges.
            args.extend(("--volume", "/run/dbus:/run/dbus:ro"))
        for port in ports:
            args.extend(("--publish", port))
        # Detached conmon must survive the controller oneshot's service cgroup.
        command("systemd-run", "--scope", "--quiet", "--collect", "--slice=machine.slice",
                "--unit=" + self.name + "-" + uuid.uuid4().hex,
                "podman", "run", "--detach", "--name", self.name, "--pull=never",
                "--restart=always", "--log-driver=none", "--health-cmd=none",
                *args, generation["image"], timeout=180)

    def healthy(self):
        try:
            info = self.inspect()
            if not info or not info["State"]["Running"]:
                return False
            kind = self.app["kind"]
            if kind == "pihole":
                if not dns_probe() or not dns_probe(tcp=True):
                    return False
                url = f"http://127.0.0.1:{self.app['settings']['web_port']}/admin/"
            elif kind == "mosquitto":
                with socket.create_connection(("127.0.0.1", 1883), timeout=5):
                    return True
            elif kind == "homeassistant":
                url = "http://127.0.0.1:8123/"
            elif self.app["health"]["kind"] == "tcp":
                with socket.create_connection(("127.0.0.1", self.app["health"]["port"]), timeout=5):
                    return True
            else:
                url = self.app["health"]["url"]
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(url, timeout=5) as response:
                return response.status == 200
        except (OSError, ValueError, RuntimeError, struct.error, subprocess.TimeoutExpired):
            return False

    def wait_healthy(self):
        deadline = time.monotonic() + self.config["updates"]["health_timeout_seconds"]
        successes = 0
        while time.monotonic() < deadline:
            successes = successes + 1 if self.healthy() else 0
            if successes >= self.config["updates"]["health_successes"]:
                return True
            time.sleep(5)
        return False

    def image(self, reference):
        return json.loads(command("podman", "image", "inspect", reference, timeout=30))[0]["Id"]

    def resolve_images(self, references):
        return self.image(references["server"])

    def pull_images(self):
        for reference in image_references(self.app).values():
            command("podman", "pull", reference)

    def seed(self, directory, image=None):
        if self.app["kind"] == "pihole":
            lists = self.app["settings"]["adlists"]
            if lists is not None:
                (directory / "adlists.list").write_text("".join(item + "\n" for item in lists))
        elif self.app["kind"] == "homeassistant":
            (directory / "configuration.yaml").write_text("default_config:\nlogger:\n  default: warning\nautomation: !include automations.yaml\nscript: !include scripts.yaml\nscene: !include scenes.yaml\n")
            for name, content in (("automations.yaml", "[]\n"), ("scripts.yaml", "{}\n"), ("scenes.yaml", "[]\n")):
                (directory / name).write_text(content)
        elif self.app["kind"] == "mosquitto":
            root = Path(self.app["data_path"])
            for name in ("config", "data"):
                (directory / name).mkdir()
            configuration = ("listener 1883 0.0.0.0\nallow_anonymous false\n"
                             f"password_file {root / 'config' / 'passwords'}\n"
                             f"persistence true\npersistence_location {root / 'data'}/\n"
                             "autosave_interval 300\nlog_dest stdout\nlog_type error\nlog_type warning\n")
            (directory / "config" / "mosquitto.conf").write_text(configuration)
            passwords = directory / "config" / "passwords"
            passwords.write_text("".join(user["username"] + ":" + user["password"] + "\n"
                                         for user in self.app["settings"]["users"]))
            passwords.chmod(0o600)
            # Convert the private temporary plaintext file in place; secrets never
            # appear in argv, stdout, or the process environment.
            try:
                command("podman", "run", "--rm", "--network=none", "--user=0",
                        "--entrypoint=mosquitto_passwd", "--volume", f"{directory}:{root}:Z",
                        image or self.image(self.app["image"]), "-H", "sha512-pbkdf2", "-U",
                        str(root / "config" / "passwords"))
            except BaseException:
                passwords.unlink(missing_ok=True)
                raise
        for name, encoded in (self.app.get("seed") or {}).get("files", {}).items():
            # Build-time validation is repeated at the write boundary.
            relative = Path(name)
            if relative.is_absolute() or ".." in relative.parts or any(p.startswith(".") for p in relative.parts):
                raise ValueError("Invalid seed filename")
            target = directory / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(base64.b64decode(encoded, validate=True))
            target.chmod(0o600)
        if self.app["kind"] == "mosquitto":
            for path in [directory, *directory.rglob("*")]:
                os.chown(path, 1883, 1883)
                path.chmod(0o750 if path.is_dir() else 0o640)

    def setup(self):
        if self.app["kind"] != "homeassistant" or not self.app.get("setup"):
            return
        # The running HA image supplies aiohttp; no additional host packages.
        result = subprocess.run(
            ["podman", "exec", "--interactive", self.name, "python", "-c",
             (CONFIG.parent / "ha_setup.py").read_text()],
            input=json.dumps(self.app["setup"]), text=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=120)
        try:
            progress = json.loads(result.stdout)
            if result.returncode or progress.get("status") not in ("waiting", "complete", "error"):
                raise ValueError
        except (ValueError, AttributeError):
            raise RuntimeError("Home Assistant initialization failed; no private output was logged") from None
        atomic_json(self.root / "setup.json", progress)
        if progress["status"] == "error":
            raise RuntimeError("Home Assistant initialization needs attention; see setup status codes")


    def rollback_pending(self):
        pending = self.state["pending"]
        if not pending:
            return
        self.state["failed"][image_key(pending["image"])] = time.time()
        self.save()
        if self.state["active"]:
            self.start(self.state["active"])
            if not self.wait_healthy():
                raise RuntimeError("Rollback failed health checks; recovery will retry")
        else:
            self.stop()
            self.remove()
        self.state["pending"] = None
        self.save()
        self.cleanup()

    def deploy(self, image):
        old = self.state["active"]
        candidate = {"image": image, "data": "gen-" + uuid.uuid4().hex}
        self.state["images"] = sorted(set(self.state["images"]) | image_ids(image))
        self.save()
        try:
            self.stop()
            if old:
                command("cp", "--archive", "--reflink=auto", str(self.data(old)), str(self.data(candidate)))
            else:
                self.data(candidate).mkdir(mode=0o700)
                self.seed(self.data(candidate), image)
            os.sync()
            self.state["pending"] = candidate
            self.save()  # journal is durable before any candidate touches its database
            self.start(candidate)
            if not self.wait_healthy():
                raise RuntimeError("Candidate failed health probation; restored previous image and data")
            self.state.update(active=candidate, previous=old, pending=None)
            self.save()
        except Exception:
            if self.state["pending"]:
                self.rollback_pending()
            elif old:
                self.start(old)
            raise
        self.cleanup()

    def cooldown(self, image):
        return time.time() - self.state["failed"].get(image_key(image), 0) < self.config["updates"]["retry_failed_after_hours"] * 3600

    def bootstrap(self):
        references, archives = {}, []
        for component, reference in image_references(self.app).items():
            archive = self.root / ("bootstrap.tar" if component == "server" else "bootstrap-" + component + ".tar")
            if archive.exists():
                command("podman", "load", "--input", str(archive))
                reference = "localhost/home-stack-" + deployment_name(self.app, component) + ":build"
                archives.append(archive)
            else:
                command("podman", "pull", reference)
            references[component] = reference
        image = self.resolve_images(references)
        if self.cooldown(image):
            raise RuntimeError("Initial image failed; waiting for retry cooldown")
        if image_key(image) in self.state["failed"]:
            # An initially broken bundled release must not trap the appliance
            # on that release forever. Pull again after its retry cooldown.
            self.pull_images()
            image = self.resolve_images(image_references(self.app))
        self.deploy(image)
        for archive in archives:
            archive.unlink(missing_ok=True)
        for component in references:
            command("podman", "untag", "localhost/home-stack-" + deployment_name(self.app, component) + ":build", check=False)

    def reconcile(self):
        if self.state["pending"]:
            self.rollback_pending()
            # Surface the interruption even if the old generation recovered.
            raise RuntimeError("Recovered interrupted deployment; previous generation restored")
        if not self.state["active"]:
            self.bootstrap()
            return
        active = self.state["active"]
        if self.matches(active) and self.healthy():
            self.cleanup()
            return
        self.start(active)
        if self.wait_healthy():
            return
        previous = self.state["previous"]
        if previous:
            self.state.update(active=previous, previous=None, pending=active)
            self.save()
            self.rollback_pending()
            raise RuntimeError("Unhealthy active generation rolled back to previous image and data")
        raise RuntimeError("Unhealthy application without a rollback generation")

    def update(self):
        self.reconcile()
        self.pull_images()
        image = self.resolve_images(image_references(self.app))
        if image == self.state["active"]["image"]:
            return
        self.state["images"] = sorted(set(self.state["images"]) | image_ids(image))
        self.save()
        if self.cooldown(image):
            raise RuntimeError("Rejected image remains in retry cooldown")
        self.deploy(image)

    def cleanup(self):
        retained = [g for g in (self.state["active"], self.state["previous"], self.state["pending"]) if g]
        paths = {self.data(g) for g in retained}
        images = set().union(*(image_ids(g["image"]) for g in retained))
        protected = set(images)
        for app in self.config["apps"]:
            state_path = self.root.parent / app["name"] / "state.json"
            if state_path.exists():
                other = json.loads(state_path.read_text())
                for generation in (other["active"], other["previous"], other["pending"]):
                    if generation:
                        protected.update(image_ids(generation["image"]))
        for path in self.root.glob("gen-*"):
            if path not in paths and path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
        tracked = set(self.state["images"]) | {key for key in self.state["failed"] if not key.startswith("group:")}
        for image in tracked - protected:
            if command("podman", "image", "exists", image, check=False, timeout=30) == 0:
                if command("podman", "image", "rm", image, check=False, timeout=60) != 0:
                    images.add(image)
        self.state["images"] = sorted(images)
        self.state["failed"] = dict(sorted(self.state["failed"].items(), key=lambda p: p[1])[-32:])
        self.save()


class GroupApp(App):
    """Resolve, stop and recover dependency containers as one generation."""
    def component_name(self, component):
        return "home-stack-" + deployment_name(self.app, component)

    def resolve_images(self, references):
        return {component: self.image(reference) for component, reference in references.items()}

    def stop(self):
        # Quiesce application writers before checkpointing their dependencies.
        for component in self.components:
            name = self.component_name(component)
            info = self.inspect(name)
            if info and info["State"]["Running"]:
                command("podman", "stop", "--time", "60", name, timeout=90)

    def remove(self):
        for component in self.components:
            command("podman", "rm", "--force", "--ignore", self.component_name(component), timeout=90)

    def matches(self, generation):
        for component in self.components:
            info = self.inspect(self.component_name(component))
            folder, target = self.mounts[component]
            if not (info and info["State"]["Running"] and info["Image"] == generation["image"][component]
                    and any(m.get("Source") == str(self.data(generation) / folder) and m.get("Destination") == target
                            for m in info.get("Mounts", []))):
                return False
        return True


class ImmichApp(GroupApp):
    """An Immich generation owns all dependency images and persistent data."""
    components = ("server", "machine-learning", "redis", "database")
    mounts = {"server": ("media", "/data"), "machine-learning": ("model-cache", "/cache"),
              "redis": ("redis", "/data"), "database": ("postgres", "/var/lib/postgresql/data")}

    def seed(self, directory, image=None):
        for folder in ("media", "model-cache", "redis", "postgres", "config"):
            (directory / folder).mkdir(mode=0o700)
        super().seed(directory / "config", image)
        # Generated on the appliance, retained with the DB across every copy.
        password = secrets.token_urlsafe(32)
        for name, content in {
            "server.env": "DB_PASSWORD=" + password + "\n",
            "database.env": ("POSTGRES_PASSWORD=" + password + "\nPOSTGRES_USER=postgres\n"
                             "POSTGRES_DB=immich\nPOSTGRES_INITDB_ARGS=--data-checksums\n"),
        }.items():
            path = directory / name
            path.write_text(content)
            path.chmod(0o600)

    def start(self, generation):
        directory = self.data(generation)
        required = [directory / folder for folder, _ in self.mounts.values()]
        if not all(path.is_dir() for path in [*required, directory / "config"]) or not all(
            (directory / name).is_file() for name in ("server.env", "database.env")
        ):
            raise RuntimeError("Generation data missing; refusing an empty replacement")
        self.stop()
        self.remove()
        network = self.name + "-network"
        command("podman", "network", "create", "--ignore", network)
        for component in reversed(self.components):
            folder, target = self.mounts[component]
            args = ["--network", network, "--network-alias", "immich-machine-learning" if component == "machine-learning" else component,
                    "--volume", f"{directory / folder}:{target}:Z"]
            tail = []
            if component == "server":
                args.extend(("--publish", f"{self.app['settings']['web_port']}:2283/tcp",
                             "--env-file", str(CONFIG.parent / (self.app["name"] + ".env")),
                             "--env-file", str(directory / "server.env"),
                             "--volume", f"{directory / 'config'}:/config:ro,Z"))
            elif component == "database":
                args.extend(("--env-file", str(directory / "database.env"), "--shm-size=128m"))
            elif component == "redis":
                tail = ["valkey-server", "--appendonly", "yes", "--save", ""]
            elif component == "machine-learning":
                # Only worker tuning values belong in the ML container.
                args.extend(("--env-file", str(CONFIG.parent / (self.app["name"] + "-machine-learning.env"))))
            name = self.component_name(component)
            command("systemd-run", "--scope", "--quiet", "--collect", "--slice=machine.slice",
                    "--unit=" + name + "-" + uuid.uuid4().hex,
                    "podman", "run", "--detach", "--name", name, "--pull=never",
                    "--restart=always", "--log-driver=none", "--health-cmd=none",
                    *args, generation["image"][component], *tail, timeout=180)

    def healthy(self):
        try:
            for component in self.components:
                info = self.inspect(self.component_name(component))
                if not info or not info["State"]["Running"]:
                    return False
            if command("podman", "exec", self.component_name("database"),
                       "pg_isready", "--username=postgres", "--dbname=immich", check=False, timeout=10):
                return False
            if command("podman", "exec", self.component_name("redis"), "valkey-cli", "ping", timeout=10) != "PONG":
                return False
            if command("podman", "exec", self.component_name("machine-learning"),
                       "python3", "healthcheck.py", check=False, timeout=10):
                return False
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(f"http://127.0.0.1:{self.app['settings']['web_port']}/api/server/ping", timeout=5) as response:
                return response.status == 200 and json.load(response).get("res") == "pong"
        except (OSError, ValueError, RuntimeError, AttributeError, subprocess.TimeoutExpired):
            return False


class SeafileApp(GroupApp):
    """Seafile, MariaDB and Redis share one update and rollback boundary."""
    components = ("server", "redis", "database")
    mounts = {"server": ("shared", "/shared"), "redis": ("redis", "/data"),
              "database": ("mysql", "/var/lib/mysql")}

    def server_url(self):
        configured = self.app["settings"]["server_url"]
        if configured is not None:
            return configured.rstrip("/")
        # Derive the LAN address only at first installation. Seafile persists
        # absolute download URLs, so use a DHCP reservation for this address.
        address = self.config["network"]["address"]
        if address:
            host = address.split("/")[0]
        else:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.connect((self.config["network"]["host_dns"][0], 53))
                host = sock.getsockname()[0]
        port = self.app["settings"]["web_port"]
        return "http://" + host + ("" if port == 80 else ":" + str(port))

    def seed(self, directory, image=None):
        for folder, _ in self.mounts.values():
            (directory / folder).mkdir(mode=0o700)
        # Nginx workers traverse /shared to serve avatars/custom static files.
        # The enclosing generation stays private on the host.
        (directory / "shared").chmod(0o755)
        super().seed(directory / "shared", image)
        s = self.app["settings"]
        # Keep first-login credentials accessible even after generation cleanup,
        # and stable when an interrupted first installation is retried.
        credentials = self.root / "initial-admin.json"
        if credentials.exists():
            admin = json.loads(credentials.read_text())
        else:
            admin = {"email": s["admin_email"] or "admin@" + self.config["hostname"] + ".local",
                     "password": s["admin_password"] or secrets.token_urlsafe(32)}
            atomic_json(credentials, admin)
        parsed = urlsplit(self.server_url())
        root_password, password, jwt = (secrets.token_urlsafe(32) for _ in range(3))
        env = {
            "SEAFILE_SERVER_HOSTNAME": parsed.netloc, "SEAFILE_SERVER_PROTOCOL": parsed.scheme,
            "INIT_SEAFILE_ADMIN_EMAIL": admin["email"], "INIT_SEAFILE_ADMIN_PASSWORD": admin["password"],
            "INIT_SEAFILE_MYSQL_ROOT_PASSWORD": root_password,
            "SEAFILE_MYSQL_DB_PASSWORD": password, "JWT_PRIVATE_KEY": jwt,
        }
        for name, values in {
            "server.env": env,
            "database.env": {"MYSQL_ROOT_PASSWORD": root_password, "MYSQL_ROOT_HOST": "%",
                             "MYSQL_LOG_CONSOLE": "true", "MARIADB_AUTO_UPGRADE": "1"},
        }.items():
            path = directory / name
            path.write_text("".join(key + "=" + value + "\n" for key, value in values.items()))
            path.chmod(0o600)

    def database_ready(self):
        return command("podman", "exec", self.component_name("database"), "healthcheck.sh",
                       "--connect", "--mariadbupgrade", "--innodb_initialized", check=False, timeout=10) == 0

    def start(self, generation):
        directory = self.data(generation)
        if not all((directory / folder).is_dir() for folder, _ in self.mounts.values()) or not all(
            (directory / name).is_file() for name in ("server.env", "database.env")
        ):
            raise RuntimeError("Generation data missing; refusing an empty replacement")
        self.stop()
        self.remove()
        network = self.name + "-network"
        command("podman", "network", "create", "--ignore", network)
        for component in reversed(self.components):
            folder, target = self.mounts[component]
            args = ["--network", network, "--network-alias", component,
                    "--volume", f"{directory / folder}:{target}:Z"]
            tail = []
            if component == "database":
                args.extend(("--env-file", str(directory / "database.env")))
            elif component == "redis":
                # Cache is disposable; keep the server off the host network.
                tail = ["redis-server", "--save", "", "--appendonly", "no"]
            else:
                args.extend(("--publish", f"{self.app['settings']['web_port']}:80/tcp",
                             "--env-file", str(CONFIG.parent / (self.app["name"] + ".env")),
                             "--env-file", str(directory / "server.env")))
            name = self.component_name(component)
            command("systemd-run", "--scope", "--quiet", "--collect", "--slice=machine.slice",
                    "--unit=" + name + "-" + uuid.uuid4().hex,
                    "podman", "run", "--detach", "--name", name, "--pull=never",
                    "--restart=always", "--log-driver=none", "--health-cmd=none",
                    *args, generation["image"][component], *tail, timeout=180)
            if component == "database":
                deadline = time.monotonic() + self.config["updates"]["health_timeout_seconds"]
                while not self.database_ready():
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Seafile database failed startup health checks")
                    time.sleep(5)

    def healthy(self):
        try:
            for component in self.components:
                info = self.inspect(self.component_name(component))
                if not info or not info["State"]["Running"]:
                    return False
            if not self.database_ready():
                return False
            if command("podman", "exec", self.component_name("redis"), "redis-cli", "ping", timeout=10) != "PONG":
                return False
            # Check the published port locally even when public HTTPS terminates
            # upstream. The Host header matches Seafile's configured authority.
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            authority = urlsplit(self.server_url()).netloc
            def probe(path):
                request = urllib.request.Request(f"http://127.0.0.1:{self.app['settings']['web_port']}" + path,
                                                 headers={"Host": authority, "Accept": "application/json"})
                with opener.open(request, timeout=5) as response:
                    return json.load(response) if response.status == 200 else None
            return probe("/api2/ping/") == "pong" and probe("/seafhttp/protocol-version").get("version") == 2
        except (OSError, ValueError, RuntimeError, AttributeError, subprocess.TimeoutExpired):
            return False


class Status:
    """Durable faults independent of volatile logs, with retryable change notifications."""
    def __init__(self, config, root=ROOT):
        self.config = config
        self.path = root / "status.json"
        self.state = json.loads(self.path.read_text()) if self.path.exists() else {"faults": {}, "outbox": {}}

    def record(self, component, error=None):
        prior = self.state["faults"].get(component)
        if prior == error or (prior is None and error is None):
            return
        if error:
            self.state["faults"][component] = error
        else:
            self.state["faults"].pop(component, None)
        self.state["outbox"][component] = {"hostname": self.config["hostname"], "component": component,
            "status": "problem" if error else "recovered", "message": error or "Recovered", "time": int(time.time())}
        atomic_json(self.path, self.state)

    def flush(self):
        url = self.config["notifications"]["webhook_url"]
        if not url:
            return
        # Do not follow redirects with notification contents or URL credentials.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        opener = urllib.request.build_opener(NoRedirect())
        for component, event in list(self.state["outbox"].items()):
            request = urllib.request.Request(url, data=json.dumps(event).encode(), headers={"Content-Type": "application/json"}, method="POST")
            try:
                with opener.open(request, timeout=15) as response:
                    if not 200 <= response.status < 300:
                        continue
            except OSError:
                continue
            del self.state["outbox"][component]
            atomic_json(self.path, self.state)


def operate(config, action, root=ROOT):
    apps = [{"immich": ImmichApp, "seafile": SeafileApp}.get(a["kind"], App)(config, a, root)
            for a in config["apps"] if a["enabled"]]
    status = Status(config, root)
    if action == "check":
        # A fresh installation has no accepted baseline to roll the OS back to.
        return int(any(a.state["active"] and not a.healthy() for a in apps))
    if action == "stop":
        for app in apps:
            app.stop()
        return 0
    if action == "status":
        print(json.dumps({"faults": status.state["faults"], "apps": {
            a.app["name"]: {"healthy": a.healthy(), "active": a.state["active"], "pending": a.state["pending"],
                              "setup": json.loads((a.root / "setup.json").read_text()) if (a.root / "setup.json").exists() else None} for a in apps}}, indent=2))
        return int(bool(status.state["faults"]))
    if action == "os-update":
        if any(not a.state["active"] or not a.healthy() for a in apps):
            status.record("os", "OS update deferred: applications are unhealthy or not initialized")
            status.flush()
            return 1
        try:
            command("transactional-update", "--non-interactive", "--drop-if-no-change", "cleanup", "dup", "reboot", timeout=10800)
            status.record("os")
        except (RuntimeError, OSError, subprocess.TimeoutExpired):
            status.record("os", "Transactional OS update failed; previous system retained")
            status.flush()
            return 1
        status.flush()
        return 0
    failed = False
    for app in apps:
        name = app.app["name"]
        try:
            # Updating one application must not prevent recovery of another.
            if action == "update" and app.app["auto_update"]:
                app.update()
                status.record(name + ":update")
            else:
                app.reconcile()
            status.record(name + ":runtime")
            try:
                app.setup()
                status.record(name + ":setup")
            except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired):
                failed = True
                status.record(name + ":setup", "Home Assistant initialization needs attention; see setup status")
        except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as error:
            failed = True
            # Runtime exceptions never include command output/environment secrets.
            status.record(name + (":update" if action == "update" else ":runtime"), str(error))
    status.flush()
    return int(failed)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("reconcile", "update", "check", "stop", "status", "os-update"))
    args = parser.parse_args()
    os.umask(0o077)
    config = json.loads(CONFIG.read_text())
    # OS and container maintenance share this lock, including health-checker.
    with open("/run/home-stack.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        raise SystemExit(operate(config, args.action))


if __name__ == "__main__":
    main()
