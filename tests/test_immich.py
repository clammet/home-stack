import base64
import copy
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from home_stack.build import bundle_containers
from home_stack.config import load
from home_stack.provision import environment, render
from runtime import controller
from runtime.images import image_references


def configuration(**app):
    return load(overrides=['apps=' + json.dumps([dict(name='photos', kind='immich', **app)])])


class ImmichConfigurationTests(unittest.TestCase):
    def test_defaults_leave_onboarding_and_ui_configuration_available(self):
        config = configuration()
        app = config['apps'][0]
        refs = image_references(app)
        self.assertEqual(refs['server'], 'ghcr.io/immich-app/immich-server:release')
        self.assertEqual(refs['machine-learning'], 'ghcr.io/immich-app/immich-machine-learning:release')
        self.assertIn('vectorchord', refs['database'])
        self.assertTrue(app['auto_update'])
        self.assertIsNone(app['setup'])
        env = environment(config, app)
        self.assertNotIn('IMMICH_CONFIG_FILE', env)
        self.assertNotIn('IMMICH_ALLOW_SETUP', env)
        self.assertNotIn('DB_PASSWORD', env)

    def test_matching_worker_tags_and_explicit_digest_pins(self):
        app = configuration(image='ghcr.io/immich-app/immich-server:v3.3.5')['apps'][0]
        self.assertTrue(image_references(app)['machine-learning'].endswith(':v3.3.5'))
        digest = 'ghcr.io/immich-app/immich-server@sha256:' + 'a' * 64
        with self.assertRaisesRegex(ValueError, 'machine_learning_image'):
            configuration(image=digest)
        app = configuration(image=digest, settings={
            'machine_learning_image': 'ghcr.io/immich-app/immich-machine-learning@sha256:' + 'b' * 64})['apps'][0]
        self.assertEqual(image_references(app)['server'], digest)

    def test_rejects_broken_mount_connections_ports_and_configuration(self):
        for values in (
            {'image': 12}, {'settings': {'web_port': True}}, {'settings': {'web_port': 0}},
            {'settings': {'database_image': 'postgres:latest'}},
            {'settings': {'machine_learning_image': 12}}, {'network': 'host'},
            {'settings': {'config_file': '../outside.json'}},
            {'settings': {'config_file': 'missing.json'}},
            {'setup': {'version': 1}},
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                configuration(**values)
        for key in ('DB_URL', 'DB_PASSWORD_FILE', 'REDIS_URL', 'POSTGRES_PASSWORD',
                    'IMMICH_PORT', 'IMMICH_HOST', 'IMMICH_CONFIG_FILE', 'IMMICH_MEDIA_LOCATION',
                    'IMMICH_WORKERS_EXCLUDE', 'CREDENTIALS_DIRECTORY', 'MACHINE_LEARNING_CACHE_FOLDER'):
            with self.subTest(key=key), self.assertRaises(ValueError):
                configuration(environment={key: 'override'})

    def test_port_and_dependency_name_collisions_are_rejected(self):
        for other in (
            {'name': 'web', 'kind': 'pihole', 'settings': {
                'admin_password': 'synthetic-password', 'web_port': 2283}},
            {'name': 'photos-database', 'kind': 'container', 'image': 'docker.io/example/app:1',
             'health': {'kind': 'tcp', 'port': 1234}},
            {'name': 'other', 'kind': 'immich'},
        ):
            for reverse in (False, True):
                apps = [{'name': 'photos', 'kind': 'immich'}, other]
                with self.subTest(other=other['name'], reverse=reverse), self.assertRaises(ValueError):
                    load(overrides=['apps=' + json.dumps(list(reversed(apps)) if reverse else apps)])

    def test_configuration_seed_is_portable_and_must_be_a_json_object(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'immich').mkdir()
            seed = root / 'immich' / 'immich.json'
            seed.write_text('{"passwordLogin":{"enabled":true}}')
            path = root / 'config.json'
            path.write_text(json.dumps({'apps': [{'name': 'photos', 'kind': 'immich',
                'seed': 'immich', 'settings': {'config_file': 'immich.json'}}]}))
            config = load(path)
            path.write_text(json.dumps(config))
            seed.unlink()
            self.assertEqual(load(path), config)
            env = environment(config, config['apps'][0])
            self.assertEqual(Path(env['IMMICH_CONFIG_FILE']).name, 'immich.json')
            for content in (b'[]', b'not json', b'\xff'):
                config['apps'][0]['seed']['files']['immich.json'] = base64.b64encode(content).decode()
                path.write_text(json.dumps(config))
                with self.subTest(content=content), self.assertRaises(ValueError):
                    load(path)

    def test_all_images_are_bundled_and_provisioned_with_private_dependencies(self):
        config = configuration(settings={'web_port': 8228})
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archives = root / 'archives'
            archives.mkdir()
            def skopeo(*args):
                if args[1] == 'copy':
                    Path(args[-1].split(':')[1]).write_bytes(b'archive')
                    return ''
                return json.dumps({'architecture': 'arm64', 'os': 'linux'})
            with patch('home_stack.build.run', side_effect=skopeo) as run:
                metadata = bundle_containers(config, archives)
            names = {'photos', 'photos-database', 'photos-redis', 'photos-machine-learning'}
            self.assertEqual(set(metadata), names)
            self.assertEqual({path.stem for path in archives.iterdir()}, names)
            for call in run.call_args_list:
                if call.args[1] == 'copy':
                    self.assertIn('arm64', call.args)
            render(config, root / 'render')
            script = (root / 'render' / 'script').read_text()
            for name in names:
                self.assertIn('archives/' + name + '.tar', script)
            self.assertIn('--add-port=8228/tcp', script)
            self.assertNotIn('--add-port=5432', script)
            self.assertNotIn('--add-port=6379', script)
            self.assertNotIn('--add-port=3003', script)
            self.assertTrue((root / 'render' / 'payload' / 'images.py').is_file())
            config['apps'][0]['enabled'] = False
            with patch('home_stack.build.run') as run:
                self.assertEqual(bundle_containers(config, archives), {})
                run.assert_not_called()


class PowerLoss(BaseException):
    pass


class SimulatedImmich(controller.ImmichApp):
    running = None
    unhealthy = False
    crash = False

    def stop(self):
        self.running = None

    def start(self, generation):
        # Model PostgreSQL migration and a media rewrite by each image set.
        directory = self.data(generation)
        version = json.dumps(generation['image'], sort_keys=True)
        (directory / 'postgres' / 'schema').write_text(version)
        (directory / 'media' / 'photo').write_text(version)
        self.running = generation
        if self.crash:
            raise PowerLoss()

    def matches(self, generation):
        return self.running == generation

    def healthy(self):
        return self.running is not None and not self.unhealthy

    def wait_healthy(self):
        return self.healthy()


class ImmichDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = configuration()
        self.app = SimulatedImmich(self.config, self.config['apps'][0], self.root)
        self.old = {key: key + '-old' for key in image_references(self.app.app)}
        self.new = {key: key + '-new' for key in self.old}
        def command(*args, **kwargs):
            if args[0] == 'cp':
                self.assertIsNone(self.app.running, 'All writers must stop before data is copied')
                shutil.copytree(args[-2], args[-1])
            return 1 if kwargs.get('check') is False else ''
        mock = patch.object(controller, 'command', side_effect=command)
        mock.start()
        self.addCleanup(mock.stop)
        sync = patch.object(controller.os, 'sync', create=True)
        sync.start()
        self.addCleanup(sync.stop)

    def test_failed_group_upgrade_restores_all_images_database_and_media(self):
        self.app.deploy(self.old)
        old = copy.deepcopy(self.app.state['active'])
        password = (self.app.data(old) / 'server.env').read_bytes()
        self.app.deploy(self.new)
        self.assertEqual((self.app.data(self.app.state['active']) / 'server.env').read_bytes(), password)
        # Rejected server/worker/database/cache changes restore one coherent pair.
        with patch.object(self.app, 'wait_healthy', side_effect=[False, True]):
            with self.assertRaisesRegex(RuntimeError, 'Candidate failed'):
                self.app.deploy({key: key + '-bad' for key in self.old})
        self.assertEqual(self.app.running['image'], self.new)
        self.assertEqual(self.app.state['previous'], old)
        for folder, name in (('postgres', 'schema'), ('media', 'photo')):
            self.assertEqual(json.loads((self.app.data(old) / folder / name).read_text()), self.old)
        self.assertEqual(len(list(self.app.root.glob('gen-*'))), 2)

    def test_interruption_restores_group_from_durable_journal(self):
        self.app.deploy(self.old)
        self.app.crash = True
        with self.assertRaises(PowerLoss):
            self.app.deploy(self.new)
        recovered = SimulatedImmich(self.config, self.app.app, self.root)
        with self.assertRaisesRegex(RuntimeError, 'interrupted'):
            recovered.reconcile()
        self.assertEqual(recovered.running['image'], self.old)
        self.assertEqual(json.loads((recovered.data(recovered.state['active']) / 'postgres' / 'schema').read_text()), self.old)
        self.assertTrue(recovered.cooldown(dict(reversed(list(self.new.items())))))
        self.assertIsNone(recovered.state['pending'])

    def test_partial_pull_failure_keeps_group_running(self):
        self.app.deploy(self.old)
        with patch.object(controller, 'command', side_effect=['', RuntimeError('offline')]):
            with self.assertRaises(RuntimeError):
                self.app.update()
        self.assertEqual(self.app.running['image'], self.old)

    def test_dependency_only_change_updates_group_and_cooldown_uses_full_set(self):
        self.app.deploy(self.old)
        changed = dict(self.old, redis='redis-new')
        with patch.object(self.app, 'pull_images'), patch.object(self.app, 'resolve_images', return_value=changed):
            self.app.update()
        self.assertEqual(self.app.state['active']['image'], changed)
        self.assertEqual(self.app.state['previous']['image'], self.old)
        with patch.object(self.app, 'wait_healthy', side_effect=[False, True]):
            with self.assertRaises(RuntimeError):
                self.app.deploy(self.new)
        self.assertTrue(self.app.cooldown(self.new))
        self.assertFalse(self.app.cooldown(dict(self.new, redis='redis-fixed')))

    def test_bootstrap_loads_every_archive_before_start_and_removes_on_success(self):
        for component in self.old:
            name = 'bootstrap.tar' if component == 'server' else 'bootstrap-' + component + '.tar'
            (self.app.root / name).write_bytes(b'archive')
        loaded = []
        def command(*args, **kwargs):
            if args[:2] == ('podman', 'load'):
                loaded.append(Path(args[-1]).name)
            return ''
        def deploy(images):
            self.assertEqual(len(loaded), 4)
            self.assertEqual(images, self.old)
        with patch.object(controller, 'command', side_effect=command), patch.object(
            self.app, 'resolve_images', return_value=self.old), patch.object(self.app, 'deploy', side_effect=deploy):
            self.app.bootstrap()
        self.assertFalse(list(self.app.root.glob('bootstrap*.tar')))

    def test_real_start_uses_generation_mounts_and_exposes_only_server(self):
        app = controller.ImmichApp(self.config, self.app.app, self.root)
        generation = {'image': self.old, 'data': 'gen-' + 'a' * 32}
        directory = app.data(generation)
        directory.mkdir()
        app.seed(directory)
        password = (directory / 'server.env').read_text().partition('=')[2].strip()
        self.assertGreaterEqual(len(password), 32)
        self.assertIn('POSTGRES_PASSWORD=' + password, (directory / 'database.env').read_text())
        self.assertEqual((directory / 'server.env').stat().st_mode & 0o777, 0o600)
        with patch.object(app, 'inspect', return_value=None), patch.object(controller, 'command', return_value='') as command:
            app.start(generation)
        runs = [call.args for call in command.call_args_list if call.args[0] == 'systemd-run']
        self.assertEqual(len(runs), 4)
        self.assertEqual([args[args.index('--name') + 1] for args in runs],
                         ['home-stack-photos-' + component for component in ('database', 'redis', 'machine-learning')] + ['home-stack-photos'])
        self.assertEqual(sum('--publish' in args for args in runs), 1)
        self.assertNotIn(password, repr(command.call_args_list))
        for component, args in zip(reversed(app.components), runs):
            folder, target = app.mounts[component]
            self.assertIn(str(directory / folder) + ':' + target + ':Z', args)
            self.assertIn(self.old[component], args)
            self.assertIn('--restart=always', args)

    def test_health_checks_do_not_require_an_account_and_detect_dependency_failure(self):
        app = controller.ImmichApp(self.config, self.app.app, self.root)
        def command(*args, **kwargs):
            return 'PONG' if 'valkey-cli' in args else 0
        with patch.object(app, 'inspect', return_value={'State': {'Running': True}}), patch.object(
            controller, 'command', side_effect=command), patch.object(controller.urllib.request, 'build_opener') as opener:
            response = io.StringIO('{"res":"pong"}')
            response.status = 200
            opener.return_value.open.return_value.__enter__.return_value = response
            self.assertTrue(app.healthy())
            self.assertTrue(opener.return_value.open.call_args.args[0].endswith('/api/server/ping'))
            with patch.object(controller, 'command', return_value=1):
                self.assertFalse(app.healthy())
            with patch.object(app, 'inspect', return_value={'State': {'Running': False}}):
                self.assertFalse(app.healthy())

    def test_missing_database_is_rejected_before_stopping_or_starting_containers(self):
        app = controller.ImmichApp(self.config, self.app.app, self.root)
        generation = {'image': self.old, 'data': 'gen-' + 'b' * 32}
        directory = app.data(generation)
        directory.mkdir()
        app.seed(directory)
        (directory / 'postgres').rmdir()
        with patch.object(controller, 'command') as command:
            with self.assertRaisesRegex(RuntimeError, 'data missing'):
                app.start(generation)
            command.assert_not_called()

    def test_initial_failure_removes_the_entire_group_and_keeps_retry_state(self):
        app = controller.ImmichApp(self.config, self.app.app, self.root)
        with patch.object(app, 'start'), patch.object(app, 'wait_healthy', return_value=False), patch.object(
            app, 'inspect', return_value={'State': {'Running': True}}), patch.object(controller, 'command') as command:
            with self.assertRaisesRegex(RuntimeError, 'Candidate failed'):
                app.deploy(self.old)
        removed = {call.args[-1] for call in command.call_args_list if call.args[:2] == ('podman', 'rm')}
        self.assertEqual(removed, {app.component_name(component) for component in app.components})
        self.assertIsNone(app.state['active'])
        self.assertIsNone(app.state['pending'])
        self.assertTrue(app.cooldown(self.old))
        self.assertFalse(list(app.root.glob('gen-*')))

    def test_other_app_cleanup_preserves_immich_dependency_images(self):
        self.app.deploy(self.old)
        config = load(overrides=['apps=' + json.dumps([
            {'name': 'photos', 'kind': 'immich'}, {'name': 'other', 'kind': 'homeassistant'}])])
        other = controller.App(config, config['apps'][1], self.root)
        other.state['images'] = list(self.old.values())
        with patch.object(controller, 'command', return_value=0) as command:
            other.cleanup()
        command.assert_not_called()
