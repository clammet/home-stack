import base64
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from home_stack.config import load
from home_stack.config_directory import seed_files
from home_stack.macos import source_archive
from home_stack.provision import render
from runtime import controller


class DirectoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "secrets").mkdir()
        (self.root / "secrets" / "admin.secret").write_text("synthetic-test-password\n")
        (self.root / "seed").mkdir()
        (self.root / "seed" / "configuration.yaml").write_text("default_config:\n")
        self.raw = {"apps": [{"name": "dns", "kind": "pihole", "settings": {
            "admin_password": {"secret_file": "secrets/admin.secret"}}},
            {"name": "ha", "kind": "homeassistant", "seed": "seed"}]}

    def parse(self):
        path = self.root / "config.json"
        path.write_text(json.dumps(self.raw))
        return load(path)

    def test_secret_resolution_and_seed_survive_macos_transfer(self):
        config = self.parse()
        self.assertEqual(config["apps"][0]["settings"]["admin_password"], "synthetic-test-password")
        encoded = config["apps"][1]["seed"]["files"]["configuration.yaml"]
        self.assertEqual(base64.b64decode(encoded), b"default_config:\n")
        # The transferred config is self-contained and needs no access to its source repo.
        path = self.root / "resolved.json"
        path.write_text(json.dumps(config))
        (self.root / "secrets" / "admin.secret").unlink()
        self.assertEqual(load(path), config)
        rendered = self.root / "render"
        render(config, rendered)
        self.assertTrue((rendered / "payload" / "ha_setup.py").is_file())

    def test_setup_manifest_resolves_secrets_and_rejects_bad_version(self):
        setup = {"version": 1, "owner": {"name": "Example", "username": "example", "language": "en",
                 "password": {"secret_file": "secrets/admin.secret"}}, "integrations": []}
        path = self.root / "setup.json"
        path.write_text(json.dumps(setup))
        self.raw["apps"][1]["setup"] = "setup.json"
        self.assertEqual(self.parse()["apps"][1]["setup"]["owner"]["password"], "synthetic-test-password")
        setup["version"] = True
        path.write_text(json.dumps(setup))
        with self.assertRaises(ValueError):
            self.parse()

    def test_private_overlay_is_copied_only_to_requested_filename(self):
        self.raw["apps"][1]["seed"] = {"directory": "seed", "private_files": {
            "secrets.yaml": "secrets/admin.secret"}}
        files = self.parse()["apps"][1]["seed"]["files"]
        self.assertEqual(set(files), {"configuration.yaml", "secrets.yaml"})

    def test_rejects_path_escape_symlinks_and_database_seeds(self):
        for filename in ("../outside", "secrets/../../outside", "admin.secret"):
            self.raw["apps"][0]["settings"]["admin_password"] = {"secret_file": filename}
            with self.subTest(filename=filename), self.assertRaises(ValueError):
                self.parse()
        self.raw["apps"][0]["settings"]["admin_password"] = {"secret_file": "secrets/admin.secret"}
        (self.root / "seed" / "linked").symlink_to(self.root / "secrets" / "admin.secret")
        with self.assertRaises(ValueError):
            self.parse()
        (self.root / "seed" / "linked").unlink()
        for name in ("zigbee.db", "home-assistant_v2.db", ".storage"):
            path = self.root / "seed" / name
            path.write_text("not migrated")
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.parse()
            path.unlink()

    def test_seeded_data_is_preserved_during_later_generation_copy(self):
        config = self.parse()
        app = controller.App(config, config["apps"][1], self.root / "runtime")
        directory = self.root / "fresh"
        directory.mkdir()
        app.seed(directory)
        self.assertEqual((directory / "configuration.yaml").read_text(), "default_config:\n")
        self.assertTrue((directory / "automations.yaml").is_file())

    def test_existing_short_mqtt_credential_is_preserved(self):
        self.raw["apps"] = [{"name": "broker", "kind": "mosquitto", "settings": {
            "users": [{"username": "example", "password": "short"}]}}]
        self.assertEqual(self.parse()["apps"][0]["settings"]["users"][0]["password"], "short")

    def test_mosquitto_hashes_without_password_in_argv_and_opens_port(self):
        self.raw["apps"] = [{"name": "broker", "kind": "mosquitto", "settings": {
            "users": [{"username": "example", "password": {"secret_file": "secrets/admin.secret"}}]}}]
        config = self.parse()
        app = controller.App(config, config["apps"][0], self.root / "runtime")
        directory = self.root / "mqtt"
        directory.mkdir()
        def convert(*args, **kwargs):
            self.assertNotIn("synthetic-test-password", repr(args))
            (directory / "config" / "passwords").write_text("example:HASH\n")
            return ""
        with patch.object(controller, "command", side_effect=convert), patch.object(controller.os, "chown"):
            app.seed(directory, "test-image-id")
        self.assertEqual((directory / "config" / "passwords").read_text(), "example:HASH\n")
        render(config, self.root / "render")
        self.assertIn("1883/tcp", (self.root / "render" / "script").read_text())


if __name__ == "__main__":
    unittest.main()
