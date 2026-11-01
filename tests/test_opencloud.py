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
    return load(overrides=['apps=' + json.dumps([dict(name='files', kind='opencloud', **app)]),
                           'network.address="192.0.2.10/24"', 'network.gateway="192.0.2.1"'])


def initialize(*args, **kwargs):
    if args[0] == 'cp':
        shutil.copytree(args[-2], args[-1])
    if args[:2] == ('podman', 'run') and 'init' in args:
        mount = next(arg for arg in args if arg.endswith(':/etc/opencloud:Z'))
        (Path(mount.split(':')[0]) / 'opencloud.yaml').write_text('generated: secrets\n')
    return 1 if kwargs.get('check') is False else ''


class OpenCloudConfigurationTests(unittest.TestCase):
    def test_minimal_preset_and_managed_environment(self):
        config = configuration()
        app = config['apps'][0]
        self.assertEqual(image_references(app), {'server': 'docker.io/opencloudeu/opencloud:latest'})
        self.assertTrue(app['auto_update'])
        self.assertIsNone(app['settings']['admin_password'])
        env = environment(config, app)
        self.assertEqual(env['PROXY_HTTP_ADDR'], '0.0.0.0:9200')
        self.assertEqual(env['PROXY_TLS'], 'true')
        self.assertEqual(env['OC_INSECURE'], 'true')
        self.assertEqual(env['IDM_CREATE_DEMO_USERS'], 'false')
        self.assertNotIn('IDM_ADMIN_PASSWORD', env)
        self.assertNotIn('OC_URL', env)
        config = configuration(settings={'proxy_tls': False, 'insecure': False,
                                         'server_url': 'https://files.example.test/'},
                               environment={'OC_LOG_LEVEL': 'warn'})
        env = environment(config, config['apps'][0])
        self.assertEqual(env['PROXY_TLS'], 'false')
        self.assertEqual(env['OC_INSECURE'], 'false')
        self.assertEqual(env['OC_LOG_LEVEL'], 'warn')

    def test_invalid_settings_and_managed_overrides_are_rejected(self):
        for values in (
            {'settings': {'web_port': True}}, {'settings': {'web_port': 0}},
            {'settings': {'proxy_tls': 'false'}}, {'settings': {'insecure': 1}},
            {'settings': {'proxy_tls': False}}, {'settings': {'insecure': False}},
            {'settings': {'server_url': 'http://files.example.test'}},
            {'settings': {'server_url': 'https://example.test/subpath'}},
            {'settings': {'server_url': 'https://user:password@example.test'}},
            {'settings': {'server_url': 'https://example.test?x=1'}},
            {'settings': {'server_url': 'https://example.test:99999'}},
            {'settings': {'server_url': 'https://example.test:0'}},
            {'settings': {'server_url': 'https://example.test;bad'}},
            {'settings': {'admin_password': 'short'}},
            {'settings': {'admin_password': 'long-but\ninvalid-password'}},
            {'network': 'host'}, {'privileged': True}, {'data_path': '/data'},
            {'ports': ['9200:9200/tcp']}, {'seed': {'files': {}}}, {'setup': {}},
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                configuration(**values)
        for key in ('OC_URL', 'OC_CONFIG_DIR', 'OC_BASE_DATA_PATH', 'OC_FORCE_CONFIG_OVERWRITE',
                    'OC_RUN_SERVICES', 'PROXY_HTTP_ADDR', 'PROXY_TLS', 'IDM_ADMIN_PASSWORD',
                    'ADMIN_PASSWORD', 'IDM_CREATE_DEMO_USERS', 'IDP_LDAP_BIND_PASSWORD',
                    'STORAGE_USERS_POSIX_ROOT', 'WEB_UI_CONFIG_FILE', 'NATS_NATS_HOST'):
            with self.subTest(key=key), self.assertRaises(ValueError):
                configuration(environment={key: 'override'})

    def test_private_settings_survive_configuration_directory_transfer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'secrets').mkdir()
            secret = root / 'secrets' / 'admin.secret'
            secret.write_text('literal-$HOME-`test`-password\n')
            path = root / 'config.json'
            path.write_text(json.dumps({'apps': [{'name': 'files', 'kind': 'opencloud', 'settings': {
                'admin_password': {'secret_file': 'secrets/admin.secret'},
                'server_url': 'https://files.example.test:9443/'}}]}))
            resolved = load(path)
            path.write_text(json.dumps(resolved))
            secret.unlink()
            self.assertEqual(load(path), resolved)
            self.assertEqual(resolved['apps'][0]['settings']['admin_password'], 'literal-$HOME-`test`-password')

    def test_ports_and_duplicate_presets_are_rejected_in_either_order(self):
        for other in (
            {'name': 'dns', 'kind': 'pihole', 'settings': {'admin_password': 'synthetic-password', 'web_port': 9200}},
            {'name': 'other', 'kind': 'opencloud', 'settings': {'web_port': 9201}},
        ):
            apps = [{'name': 'files', 'kind': 'opencloud'}, other]
            for ordered in (apps, list(reversed(apps))):
                with self.subTest(other=other), self.assertRaises(ValueError):
                    load(overrides=['apps=' + json.dumps(ordered)])

    def test_builder_bundles_one_arm64_image_and_provisions_the_web_port(self):
        config = configuration(settings={'web_port': 9443})
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
            self.assertEqual(set(metadata), {'files'})
            self.assertEqual({p.stem for p in archives.iterdir()}, {'files'})
            self.assertTrue(any('arm64' in call.args for call in run.call_args_list))
            render(config, root / 'render')
            script = (root / 'render' / 'script').read_text()
            self.assertIn('netavark aardvark-dns', script)
            self.assertIn('archives/files.tar', script)
            self.assertIn('--add-port=9443/tcp', script)
            self.assertNotIn('--add-port=9205', script)
            self.assertNotIn('--add-port=3306', script)
            config['apps'][0]['enabled'] = False
            with patch('home_stack.build.run') as run:
                self.assertEqual(bundle_containers(config, archives), {})
                run.assert_not_called()


class PowerLoss(BaseException):
    pass


class SimulatedOpenCloud(controller.OpenCloudApp):
    running = None
    crash = False

    def stop(self):
        self.running = None

    def start(self, generation):
        directory = self.data(generation)
        (directory / 'config' / 'schema').write_text(generation['image'])
        (directory / 'data' / 'file').write_text(generation['image'])
        self.running = generation
        if self.crash:
            raise PowerLoss()

    def matches(self, generation):
        return self.running == generation

    def healthy(self):
        return self.running is not None

    def wait_healthy(self):
        return self.healthy()


class OpenCloudDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = configuration()
        self.app = controller.OpenCloudApp(self.config, self.config['apps'][0], self.root)
        commands = patch.object(controller, 'command', side_effect=initialize)
        commands.start()
        self.addCleanup(commands.stop)
        sync = patch.object(controller.os, 'sync', create=True)
        sync.start()
        self.addCleanup(sync.stop)

    def seed(self):
        generation = {'image': 'old', 'data': 'gen-' + 'a' * 32}
        directory = self.app.data(generation)
        directory.mkdir()
        self.app.seed(directory, generation['image'])
        return generation, directory

    def test_first_run_initializes_once_with_private_stable_credentials(self):
        with patch.object(controller, 'command', side_effect=initialize) as commands:
            generation, directory = self.seed()
        calls = [call.args for call in commands.call_args_list]
        self.assertIn(('chown', '1000:1000', str(directory / 'config'), str(directory / 'data')), calls)
        init = next(args for args in calls if 'init' in args)
        self.assertIn('--network=none', init)
        self.assertIn('/etc/opencloud', init)
        admin = json.loads((self.app.root / 'initial-admin.json').read_text())
        self.assertEqual(admin['username'], 'admin')
        self.assertGreaterEqual(len(admin['password']), 32)
        self.assertNotIn(admin['password'], repr(calls))
        env = (directory / 'server.env').read_text()
        self.assertIn('OC_URL=https://192.0.2.10:9200\n', env)
        self.assertIn('IDM_ADMIN_PASSWORD=' + admin['password'] + '\n', env)
        for path in (directory / 'server.env', self.app.root / 'initial-admin.json'):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        retry = self.app.root / 'retry'
        retry.mkdir()
        self.app.seed(retry, 'old')
        self.assertEqual((retry / 'server.env').read_text(), env)
        with patch.object(self.app, 'inspect', return_value=None), patch.object(controller, 'command') as commands:
            self.app.start(generation)
        self.assertFalse(any('init' in call.args for call in commands.call_args_list))

    def test_configured_credentials_and_url_and_dhcp_discovery(self):
        self.config = configuration(settings={'admin_password': 'literal-$HOME-`test`-password',
                                              'server_url': 'https://files.example.test/'})
        self.app = controller.OpenCloudApp(self.config, self.config['apps'][0], self.root)
        _, directory = self.seed()
        self.assertEqual((directory / 'server.env').read_text(),
                         'OC_URL=https://files.example.test\nIDM_ADMIN_PASSWORD=literal-$HOME-`test`-password\n')
        self.app.app['settings']['server_url'] = None
        self.app.config['network']['address'] = None
        with patch.object(controller.socket, 'socket') as sock:
            sock.return_value.__enter__.return_value.getsockname.return_value = ('192.0.2.20', 1234)
            self.assertEqual(self.app.server_url(), 'https://192.0.2.20:9200')
            self.app.app['settings']['web_port'] = 443
            self.assertEqual(self.app.server_url(), 'https://192.0.2.20')

    def test_failed_initialization_is_not_ignored_or_started_as_a_server(self):
        with patch.object(controller, 'command') as commands, self.assertRaisesRegex(RuntimeError, 'initialization'):
            self.seed()
        self.assertFalse(any('server' in call.args for call in commands.call_args_list))

    def test_start_and_matches_require_both_persistent_mounts(self):
        generation, directory = self.seed()
        with patch.object(self.app, 'inspect', return_value=None), patch.object(controller, 'command') as commands:
            self.app.start(generation)
        run = next(call.args for call in commands.call_args_list if call.args[0] == 'systemd-run')
        self.assertIn('9200:9200/tcp', run)
        self.assertIn('1000:1000', run)
        self.assertEqual(run[-2:], ('old', 'server'))
        mounts = [{'Source': str(directory / folder), 'Destination': target} for folder, target in self.app.mounts.items()]
        for mount in mounts:
            self.assertIn(mount['Source'] + ':' + mount['Destination'] + ':Z', run)
        info = {'State': {'Running': True}, 'Image': 'old', 'Mounts': mounts}
        with patch.object(self.app, 'inspect', return_value=info):
            self.assertTrue(self.app.matches(generation))
            info['Mounts'] = mounts[:1]
            self.assertFalse(self.app.matches(generation))
        (directory / 'config' / 'opencloud.yaml').unlink()
        with patch.object(controller, 'command') as commands, self.assertRaisesRegex(RuntimeError, 'data missing'):
            self.app.start(generation)
        commands.assert_not_called()

    def test_health_checks_status_and_identity_using_the_persisted_url(self):
        generation, _ = self.seed()
        self.app.state['pending'] = generation
        # Health must not discover a different DHCP address or contact this URL.
        self.app.config['network']['address'] = '192.0.2.99/24'
        issuer = 'https://192.0.2.10:9200'
        scheme = 'https'
        def response(request, timeout):
            self.assertTrue(request.full_url.startswith(scheme + '://127.0.0.1:9200/'))
            self.assertEqual(request.get_header('Host'), '192.0.2.10:9200')
            content = {'installed': True, 'maintenance': False} if request.full_url.endswith('/status.php') else {
                'issuer': issuer, 'authorization_endpoint': issuer + '/signin', 'token_endpoint': issuer + '/token'}
            value = io.StringIO(json.dumps(content))
            value.status = 200
            return value
        info = {'State': {'Running': True}, 'Config': {'Env': ['OC_URL=' + issuer]}}
        with patch.object(self.app, 'inspect', return_value=info), \
                patch.object(controller.urllib.request, 'build_opener') as opener:
            opener.return_value.open.side_effect = response
            self.assertTrue(self.app.healthy())
            with patch.object(self.app, 'inspect', return_value={'State': {'Running': False}}):
                self.assertFalse(self.app.healthy())
            for content in ('null', '[]', '{"installed":false}', 'not json', '{"maintenance":true}'):
                value = io.StringIO(content)
                value.status = 200
                opener.return_value.open.side_effect = [value, response(
                    controller.urllib.request.Request('https://127.0.0.1:9200/.well-known/openid-configuration',
                                                      headers={'Host': '192.0.2.10:9200'}), 5)]
                with self.subTest(content=content):
                    self.assertFalse(self.app.healthy())
            issuer = 'https://wrong.example.test'
            opener.return_value.open.side_effect = response
            self.assertFalse(self.app.healthy())
            issuer = 'https://192.0.2.10:9200'
            self.app.app['settings']['proxy_tls'] = False
            scheme = 'http'
            opener.return_value.open.side_effect = response
            self.assertTrue(self.app.healthy())
            # During rollback, the failed pending generation may be missing.
            self.app.state['pending'] = {'image': 'bad', 'data': 'gen-' + 'b' * 32}
            self.assertTrue(self.app.healthy())
            info['Config']['Env'] = []
            self.assertFalse(self.app.healthy())

    def test_failed_and_interrupted_updates_restore_configuration_files_and_credentials(self):
        app = SimulatedOpenCloud(self.config, self.app.app, self.root)
        app.deploy('old')
        old = copy.deepcopy(app.state['active'])
        env = (app.data(old) / 'server.env').read_bytes()
        (app.data(old) / 'config' / 'user-setting').write_text('preserve user edits')
        with patch.object(app, 'wait_healthy', side_effect=[False, True]), \
                self.assertRaisesRegex(RuntimeError, 'Candidate failed'):
            app.deploy('new')
        self.assertEqual(app.running, old)
        self.assertTrue(app.cooldown('new'))
        app.crash = True
        with self.assertRaises(PowerLoss):
            app.deploy('new')
        recovered = SimulatedOpenCloud(self.config, self.app.app, self.root)
        with self.assertRaisesRegex(RuntimeError, 'interrupted'):
            recovered.reconcile()
        self.assertEqual(recovered.running, old)
        self.assertEqual((recovered.data(old) / 'config' / 'schema').read_text(), 'old')
        self.assertEqual((recovered.data(old) / 'data' / 'file').read_text(), 'old')
        self.assertEqual((recovered.data(old) / 'server.env').read_bytes(), env)
        self.assertEqual((recovered.data(old) / 'config' / 'user-setting').read_text(), 'preserve user edits')
        with patch.object(recovered, 'resolve_images', return_value='updated'), \
                patch.object(recovered, 'seed', side_effect=AssertionError('must not reinitialize')):
            recovered.update()
        self.assertEqual(recovered.state['active']['image'], 'updated')
        self.assertEqual((recovered.data(recovered.state['active']) / 'server.env').read_bytes(), env)


if __name__ == '__main__':
    unittest.main()
