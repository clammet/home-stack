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
    return load(overrides=['apps=' + json.dumps([dict(name='files', kind='seafile', **app)]),
                           'network.address="192.0.2.10/24"', 'network.gateway="192.0.2.1"'])


class SeafileConfigurationTests(unittest.TestCase):
    def test_minimal_preset_manages_all_images_without_embedded_bootstrap_secrets(self):
        config = configuration()
        app = config['apps'][0]
        refs = image_references(app)
        self.assertEqual(set(refs), {'server', 'database', 'redis'})
        self.assertTrue(refs['server'].endswith(':13.0-latest'))
        self.assertTrue(all('@' not in ref for ref in refs.values()))
        self.assertTrue(app['auto_update'])
        self.assertIsNone(app['settings']['admin_email'])
        self.assertIsNone(app['settings']['admin_password'])
        env = environment(config, app)
        self.assertEqual(env['CACHE_PROVIDER'], 'redis')
        self.assertEqual(env['SEAFILE_MYSQL_DB_HOST'], 'database')
        self.assertEqual(env['TIME_ZONE'], config['timezone'])
        self.assertEqual(env['ENABLE_SEADOC'], 'false')
        self.assertNotIn('JWT_PRIVATE_KEY', env)
        self.assertNotIn('INIT_SEAFILE_ADMIN_PASSWORD', env)

    def test_invalid_settings_and_managed_environment_cannot_break_the_preset(self):
        for values in (
            {'settings': {'web_port': True}}, {'settings': {'web_port': 0}},
            {'settings': {'server_url': 'https://example.com/subpath'}},
            {'settings': {'server_url': 'https://user:secret@example.com'}},
            {'settings': {'server_url': 'https://example.com?x=1'}},
            {'settings': {'server_url': 'http://example.com:99999'}},
            {'settings': {'server_url': 'http://example.com:0'}},
            {'settings': {'server_url': 'http://example.com;bad'}},
            {'settings': {'admin_email': 'admin@example.com'}},
            {'settings': {'admin_email': 'invalid', 'admin_password': 'long-enough-password'}},
            {'settings': {'admin_email': 'admin@example.com', 'admin_password': 'short'}},
            {'settings': {'database_image': 'mariadb:latest'}},
            {'settings': {'redis_image': 12}}, {'network': 'host'}, {'privileged': True},
            {'ports': ['8080:80/tcp']}, {'seed': {'files': {}}}, {'setup': {'version': 1}},
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                configuration(**values)
        for key in ('SEAFILE_MYSQL_DB_HOST', 'INIT_SEAFILE_ADMIN_EMAIL', 'MYSQL_HOST', 'MARIADB_USER',
                    'REDIS_URL', 'JWT_PRIVATE_KEY', 'CACHE_PROVIDER', 'SITE_ROOT', 'NON_ROOT',
                    'SERVER_IP', 'ENABLE_SEADOC', 'TIME_ZONE', 'CLUSTER_INIT_MODE'):
            with self.subTest(key=key), self.assertRaises(ValueError):
                configuration(environment={key: 'override'})

    def test_private_settings_survive_configuration_directory_transfer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'secrets').mkdir()
            (root / 'secrets' / 'admin.secret').write_text('literal-$HOME-`test`-password\n')
            config = {'apps': [{'name': 'files', 'kind': 'seafile', 'settings': {
                'admin_email': 'admin@example.com', 'admin_password': {'secret_file': 'secrets/admin.secret'},
                'server_url': 'https://files.example.com:8443/'}}]}
            path = root / 'config.json'
            path.write_text(json.dumps(config))
            resolved = load(path)
            path.write_text(json.dumps(resolved))
            (root / 'secrets' / 'admin.secret').unlink()
            self.assertEqual(load(path), resolved)
            self.assertEqual(resolved['apps'][0]['settings']['admin_password'], 'literal-$HOME-`test`-password')

    def test_port_and_dependency_names_are_reserved_in_either_order(self):
        for other in (
            {'name': 'dns', 'kind': 'pihole', 'settings': {'admin_password': 'synthetic-password', 'web_port': 8080}},
            {'name': 'files-database', 'kind': 'homeassistant'},
            {'name': 'files-redis', 'kind': 'homeassistant'},
            {'name': 'other', 'kind': 'seafile', 'settings': {'web_port': 8088}},
        ):
            for reverse in (False, True):
                apps = [{'name': 'files', 'kind': 'seafile'}, other]
                with self.subTest(other=other['name'], reverse=reverse), self.assertRaises(ValueError):
                    load(overrides=['apps=' + json.dumps(list(reversed(apps)) if reverse else apps)])

    def test_builder_bundles_all_arm64_images_and_only_opens_the_web_port(self):
        config = configuration(settings={'web_port': 8088})
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
            names = {'files', 'files-database', 'files-redis'}
            self.assertEqual(set(metadata), names)
            self.assertEqual({p.stem for p in archives.iterdir()}, names)
            for call in run.call_args_list:
                if call.args[1] == 'copy':
                    self.assertIn('arm64', call.args)
            render(config, root / 'render')
            script = (root / 'render' / 'script').read_text()
            self.assertIn('netavark aardvark-dns', script)
            for name in names:
                self.assertIn('archives/' + name + '.tar', script)
            self.assertIn('--add-port=8088/tcp', script)
            self.assertNotIn('--add-port=3306', script)
            self.assertNotIn('--add-port=6379', script)
            config['apps'][0]['enabled'] = False
            with patch('home_stack.build.run') as run:
                self.assertEqual(bundle_containers(config, archives), {})
                run.assert_not_called()


class PowerLoss(BaseException):
    pass


class SimulatedSeafile(controller.SeafileApp):
    running = None
    crash = False

    def stop(self):
        self.running = None

    def start(self, generation):
        directory = self.data(generation)
        (directory / 'mysql' / 'schema').write_text(generation['image']['database'])
        (directory / 'shared' / 'library').write_text(generation['image']['server'])
        self.running = generation
        if self.crash:
            raise PowerLoss()

    def matches(self, generation):
        return self.running == generation

    def healthy(self):
        return self.running is not None

    def wait_healthy(self):
        return self.healthy()


class SeafileDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = configuration()
        self.app = controller.SeafileApp(self.config, self.config['apps'][0], self.root)
        self.old = {component: component + '-old' for component in self.app.components}
        self.new = {component: component + '-new' for component in self.app.components}
        sync = patch.object(controller.os, 'sync', create=True)
        sync.start()
        self.addCleanup(sync.stop)

    def seed(self):
        generation = {'image': self.old, 'data': 'gen-' + 'a' * 32}
        directory = self.app.data(generation)
        directory.mkdir()
        self.app.seed(directory)
        return generation, directory

    def test_first_run_generates_private_stable_credentials_and_lan_url(self):
        _, directory = self.seed()
        admin = json.loads((self.app.root / 'initial-admin.json').read_text())
        self.assertEqual(admin['email'], 'admin@home-stack.local')
        self.assertGreaterEqual(len(admin['password']), 32)
        env = dict(line.split('=', 1) for line in (directory / 'server.env').read_text().splitlines())
        db = dict(line.split('=', 1) for line in (directory / 'database.env').read_text().splitlines())
        self.assertEqual(env['INIT_SEAFILE_ADMIN_PASSWORD'], admin['password'])
        self.assertEqual(env['SEAFILE_SERVER_HOSTNAME'], '192.0.2.10:8080')
        self.assertEqual(db['MYSQL_ROOT_PASSWORD'], env['INIT_SEAFILE_MYSQL_ROOT_PASSWORD'])
        self.assertNotEqual(db['MYSQL_ROOT_PASSWORD'], env['SEAFILE_MYSQL_DB_PASSWORD'])
        self.assertGreaterEqual(len(env['JWT_PRIVATE_KEY']), 32)
        self.assertTrue((directory / 'shared').stat().st_mode & 0o001, 'Nginx workers need shared-volume traversal')
        for path in (directory / 'server.env', directory / 'database.env', self.app.root / 'initial-admin.json'):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        retry = self.app.root / 'retry'
        retry.mkdir()
        self.app.seed(retry)
        self.assertIn('INIT_SEAFILE_ADMIN_PASSWORD=' + admin['password'], (retry / 'server.env').read_text())

    def test_configured_admin_url_and_dhcp_discovery(self):
        config = configuration(settings={'admin_email': 'owner@example.com',
            'admin_password': 'literal-$HOME-`test`-password', 'server_url': 'https://files.example.com/'})
        self.app = controller.SeafileApp(config, config['apps'][0], self.root)
        _, directory = self.seed()
        env = (directory / 'server.env').read_text()
        self.assertIn('INIT_SEAFILE_ADMIN_EMAIL=owner@example.com\n', env)
        self.assertIn('INIT_SEAFILE_ADMIN_PASSWORD=literal-$HOME-`test`-password\n', env)
        self.assertIn('SEAFILE_SERVER_PROTOCOL=https\n', env)
        self.assertIn('SEAFILE_SERVER_HOSTNAME=files.example.com\n', env)
        self.app.app['settings']['server_url'] = None
        self.app.config['network']['address'] = None
        with patch.object(controller.socket, 'socket') as sock:
            sock.return_value.__enter__.return_value.getsockname.return_value = ('192.0.2.20', 1234)
            self.assertEqual(self.app.server_url(), 'http://192.0.2.20:8080')
            self.app.app['settings']['web_port'] = 80
            self.assertEqual(self.app.server_url(), 'http://192.0.2.20')

    def test_start_waits_for_database_and_mounts_only_generation_data(self):
        generation, directory = self.seed()
        def command(*args, **kwargs):
            return 0 if 'healthcheck.sh' in args else ''
        with patch.object(self.app, 'inspect', return_value=None), patch.object(
            controller, 'command', side_effect=command) as cmd:
            self.app.start(generation)
        calls = [c.args for c in cmd.call_args_list]
        runs = [args for args in calls if args[0] == 'systemd-run']
        self.assertEqual([args[args.index('--name') + 1] for args in runs],
                         ['home-stack-files-database', 'home-stack-files-redis', 'home-stack-files'])
        db_ready = next(i for i, args in enumerate(calls) if 'healthcheck.sh' in args)
        self.assertLess(calls.index(runs[0]), db_ready)
        self.assertLess(db_ready, calls.index(runs[-1]))
        self.assertEqual(sum('--publish' in args for args in runs), 1)
        self.assertIn('8080:80/tcp', runs[-1])
        for component, args in zip(reversed(self.app.components), runs):
            folder, target = self.app.mounts[component]
            self.assertIn(str(directory / folder) + ':' + target + ':Z', args)
            self.assertIn(self.old[component], args)
            self.assertIn('--restart=always', args)
        self.assertNotIn(json.loads((self.app.root / 'initial-admin.json').read_text())['password'], repr(calls))
        shutil.rmtree(directory / 'mysql')
        with patch.object(controller, 'command') as cmd, self.assertRaisesRegex(RuntimeError, 'data missing'):
            self.app.start(generation)
        cmd.assert_not_called()

    def test_database_timeout_prevents_server_start(self):
        generation, _ = self.seed()
        with patch.object(self.app, 'inspect', return_value=None), patch.object(controller, 'command', return_value='') as cmd, \
                patch.object(self.app, 'database_ready', return_value=False), \
                patch.object(controller.time, 'monotonic', side_effect=[0, 9999]):
            with self.assertRaisesRegex(RuntimeError, 'database failed'):
                self.app.start(generation)
        runs = [call.args for call in cmd.call_args_list if call.args[0] == 'systemd-run']
        self.assertEqual(len(runs), 1)

    def test_health_requires_cache_database_web_and_file_server_without_login(self):
        def command(*args, **kwargs):
            return 'PONG' if 'redis-cli' in args else 0
        def response(request, timeout):
            self.assertTrue(request.full_url.startswith('http://127.0.0.1:8080/'))
            self.assertEqual(request.get_header('Host'), '192.0.2.10:8080')
            value = io.StringIO('"pong"' if request.full_url.endswith('/api2/ping/') else '{"version":2}')
            value.status = 200
            return value
        with patch.object(self.app, 'inspect', return_value={'State': {'Running': True}}), \
                patch.object(controller, 'command', side_effect=command), \
                patch.object(controller.urllib.request, 'build_opener') as opener:
            opener.return_value.open.side_effect = response
            self.assertTrue(self.app.healthy())
            for result in (1, 'bad response'):
                with patch.object(controller, 'command', return_value=result):
                    self.assertFalse(self.app.healthy())
            with patch.object(self.app, 'inspect', return_value={'State': {'Running': False}}):
                self.assertFalse(self.app.healthy())
            for content in ('null', '[]', '{"version":0}', 'not json'):
                value = io.StringIO(content)
                value.status = 200
                opener.return_value.open.side_effect = [response(
                    controller.urllib.request.Request('http://127.0.0.1:8080/api2/ping/',
                                                      headers={'Host': '192.0.2.10:8080'}), 5), value]
                with self.subTest(content=content):
                    self.assertFalse(self.app.healthy())

    def test_failed_and_interrupted_updates_restore_database_files_images_and_secrets(self):
        app = SimulatedSeafile(self.config, self.app.app, self.root)
        def command(*args, **kwargs):
            if args[0] == 'cp':
                self.assertIsNone(app.running)
                shutil.copytree(args[-2], args[-1])
            return 1 if kwargs.get('check') is False else ''
        with patch.object(controller, 'command', side_effect=command):
            app.deploy(self.old)
            old = copy.deepcopy(app.state['active'])
            env = (app.data(old) / 'server.env').read_bytes()
            (app.data(old) / 'shared' / 'ui-setting').write_text('preserve user edits')
            with patch.object(app, 'wait_healthy', side_effect=[False, True]), \
                    self.assertRaisesRegex(RuntimeError, 'Candidate failed'):
                app.deploy(self.new)
            self.assertEqual(app.running, old)
            self.assertTrue(app.cooldown(self.new))
            app.crash = True
            with self.assertRaises(PowerLoss):
                app.deploy(self.new)
            recovered = SimulatedSeafile(self.config, self.app.app, self.root)
            with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                recovered.reconcile()
            self.assertEqual(recovered.running, old)
            self.assertEqual((recovered.data(old) / 'mysql' / 'schema').read_text(), self.old['database'])
            self.assertEqual((recovered.data(old) / 'shared' / 'library').read_text(), self.old['server'])
            self.assertEqual((recovered.data(old) / 'server.env').read_bytes(), env)
            self.assertEqual((recovered.data(old) / 'shared' / 'ui-setting').read_text(), 'preserve user edits')
            app.crash = False
            app.state = recovered.state
            with patch.object(app, 'resolve_images', return_value=dict(self.old, redis='redis-new')):
                app.update()
            self.assertEqual(app.state['active']['image']['redis'], 'redis-new')
            self.assertEqual((app.data(app.state['active']) / 'server.env').read_bytes(), env)
            with patch.object(controller, 'command', side_effect=['', RuntimeError('offline')]), \
                    self.assertRaises(RuntimeError):
                app.update()
            self.assertEqual(app.running, app.state['active'])


if __name__ == '__main__':
    unittest.main()
