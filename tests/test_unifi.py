import base64
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from home_stack.build import bundle_containers
from home_stack.config import load
from home_stack.config_directory import MAX_UNIFI_BACKUP
from home_stack.macos import source_archive
from home_stack.provision import environment, render
from runtime import controller
from runtime.images import unifi_ports


BACKUP = b'\x00\xff\r\nopaque-network-backup\x80'


def configuration(**app):
    return load(overrides=['apps=' + json.dumps([dict(name='network', kind='unifi', **app)])])


def encoded_backup():
    return {'content': base64.b64encode(BACKUP).decode()}


class UnifiConfigurationTests(unittest.TestCase):
    def test_preset_and_managed_environment(self):
        config = configuration(settings={'inform_host': '192.0.2.10'})
        app = config['apps'][0]
        self.assertEqual(app['image'], 'docker.io/jacobalberty/unifi:latest')
        self.assertIsNone(app['settings']['backup'])
        self.assertTrue(app['auto_update'])
        self.assertEqual(app['network'], 'bridge')
        env = environment(config, app)
        self.assertEqual(env['RUNAS_UID0'], 'false')
        self.assertEqual(env['UNIFI_HTTPS_PORT'], '8443')
        self.assertEqual(env['SYSTEM_IP'], '192.0.2.10')
        self.assertNotIn('SYSTEM_IP', environment(configuration(), configuration()['apps'][0]))

    def test_binary_backup_survives_macos_transfer_without_source_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'secrets').mkdir()
            source = root / 'secrets' / 'controller.unf'
            source.write_bytes(BACKUP)
            path = root / 'config.json'
            path.write_text(json.dumps({'apps': [{'name': 'network', 'kind': 'unifi',
                'settings': {'backup': 'secrets/controller.unf'}}]}))
            config = load(path)
            self.assertEqual(config['apps'][0]['settings']['backup'], encoded_backup())
            archive = root / 'source.tar'
            source_archive(archive, config)
            import tarfile
            with tarfile.open(archive) as stream:
                transferred = json.load(stream.extractfile('config.json'))
                self.assertFalse(any('controller.unf' in name for name in stream.getnames()))
            source.unlink()
            path.write_text(json.dumps(transferred))
            self.assertEqual(load(path), config)
            render(config, root / 'render')
            payload = json.loads((root / 'render' / 'payload' / 'config.json').read_text())
            self.assertEqual(payload['apps'][0]['settings']['backup'], encoded_backup())
            self.assertEqual((root / 'render').stat().st_mode & 0o777, 0o700)

    def test_backup_rejects_escapes_symlinks_wrong_format_empty_and_oversize(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / 'config.json'
            def parse(backup):
                path.write_text(json.dumps({'apps': [{'name': 'network', 'kind': 'unifi',
                    'settings': {'backup': backup}}]}))
                return load(path)
            (root / 'valid.unf').write_bytes(BACKUP)
            (root / 'empty.unf').touch()
            (root / 'console.unifi').write_bytes(BACKUP)
            (root / 'link.unf').symlink_to(root / 'valid.unf')
            (root / 'folder').symlink_to(root)
            for backup in ('../valid.unf', str(root / 'valid.unf'), 'link.unf', 'folder/valid.unf',
                           'missing.unf', 'empty.unf', 'console.unifi', False,
                           {'content': ''}, {'content': '$not-base64'}, {'content': 3},
                           {'content': encoded_backup()['content'], 'filename': 'ignored'}):
                with self.subTest(backup=backup), self.assertRaises(ValueError):
                    parse(backup)
            # Backups have their own bound, independent of small text seed files.
            with patch('home_stack.config_directory.MAX_UNIFI_BACKUP', len(BACKUP) - 1):
                for backup in ('valid.unf', encoded_backup()):
                    with self.subTest(backup=backup), self.assertRaises(ValueError):
                        parse(backup)
            with (root / 'large.unf').open('wb') as stream:
                stream.truncate(MAX_UNIFI_BACKUP + 1)
            with self.assertRaises(ValueError):
                parse('large.unf')

    def test_invalid_settings_and_managed_overrides_are_rejected(self):
        for values in ({'settings': {'web_port': True}}, {'settings': {'web_port': 0}},
                       {'settings': {'guest_portal': 'true'}}, {'settings': {'inform_host': 'https://host'}},
                       {'settings': {'inform_host': 'host\nother'}}, {'network': 'host'},
                       {'privileged': True}, {'data_path': '/data'}, {'seed': {'files': {}}},
                       {'setup': {}}, {'ports': ['8081:8080/tcp']}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                configuration(**values)
        for key in ('DB_URI', 'MONGOPORT', 'UNIFI_HTTP_PORT', 'DATADIR', 'RUNDIR', 'SYSTEM_IP',
                    'RUNAS_UID0', 'BIND_PRIV', 'UNIFI_UID', 'JVM_EXTRA_OPTS', 'JAVA_TOOL_OPTIONS', 'PKGURL'):
            with self.subTest(key=key), self.assertRaises(ValueError):
                configuration(environment={key: 'override'})

    def test_ports_include_conflicts_in_either_order_and_optional_portal(self):
        app = configuration()['apps'][0]
        self.assertEqual(set(unifi_ports(app)), {'8443:8443/tcp', '8080:8080/tcp',
                                               '3478:3478/udp', '10001:10001/udp'})
        for port in (8080, 8443, 3478, 10001):
            protocol = 'udp' if port in (3478, 10001) else 'tcp'
            other = {'name': 'other', 'kind': 'container', 'image': 'docker.io/example/test:1',
                     'ports': [f'{port}:80/{protocol}'], 'health': {'kind': 'tcp', 'port': port}}
            for apps in ([app, other], [other, app]):
                with self.subTest(port=port), self.assertRaisesRegex(ValueError, 'Conflicting host port'):
                    load(overrides=['apps=' + json.dumps(apps)])
        load(overrides=['apps=' + json.dumps([app, {'name': 'files', 'kind': 'opencloud'}])])
        with self.assertRaisesRegex(ValueError, 'Conflicting host port'):
            load(overrides=['apps=' + json.dumps([app, {'name': 'files', 'kind': 'opencloud',
                                                      'settings': {'web_port': 8080}}])])
        for web_port in (8080, 8843, 8880):
            with self.subTest(web_port=web_port), self.assertRaisesRegex(ValueError, 'Conflicting host port'):
                configuration(settings={'web_port': web_port, 'guest_portal': True})

    def test_builder_bundles_arm64_and_provisioning_opens_matching_ports(self):
        config = configuration(settings={'web_port': 9443, 'guest_portal': True})
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
            self.assertEqual(set(metadata), {'network'})
            self.assertTrue(any('arm64' in c.args for c in run.call_args_list))
            render(config, root / 'render')
            script = (root / 'render' / 'script').read_text()
            for port in ('9443/tcp', '8080/tcp', '3478/udp', '10001/udp', '8880/tcp', '8843/tcp'):
                self.assertIn('--add-port=' + port, script)
            self.assertNotIn('--add-port=27117', script)


class FakeOpener:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        result = next(self.responses)
        if isinstance(result, BaseException):
            raise result
        stream = io.StringIO(json.dumps(result))
        stream.status = 200
        return stream


class UnifiRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = configuration(settings={'backup': encoded_backup()})
        self.app = controller.UnifiApp(self.config, self.config['apps'][0], self.root)
        self.generation = {'image': 'server-old', 'data': 'gen-' + 'a' * 32}
        self.directory = self.app.data(self.generation)
        self.directory.mkdir()
        self.app.seed(self.directory)
        self.marker = self.directory / '.home-stack-restore.json'
        self.initial = {'State': {'Running': True, 'StartedAt': 'before'}}
        self.restarted = {'State': {'Running': True, 'StartedAt': 'after'}}

    def test_start_uses_generation_mount_published_ports_and_embedded_database(self):
        self.marker.unlink()
        with patch.object(controller, 'command') as cmd:
            self.app.start(self.generation)
        runs = [c.args for c in cmd.call_args_list if c.args[0] == 'systemd-run']
        self.assertEqual(len(runs), 1)
        args = runs[0]
        self.assertIn(str(self.directory) + ':/unifi:Z', args)
        self.assertIn('--init', args)
        self.assertIn('--image-volume=ignore', args)
        self.assertIn('8080:8080/tcp', args)
        self.assertNotIn('27117:27117/tcp', args)
        self.assertNotIn(encoded_backup()['content'], repr(cmd.call_args_list))
        self.assertEqual(self.directory.stat().st_mode & 0o777, 0o755)

    def test_restore_uploads_exact_binary_and_waits_for_restart_and_admin(self):
        for details in ({'backup_id': 'private-id'}, [{'backup_id': 'private-id'}]):
            controller.atomic_json(self.marker, {'status': 'pending'})
            opener = FakeOpener({'meta': {'rc': 'ok'}, 'data': details}, {'meta': {'rc': 'ok'}})
            with patch.object(controller.App, 'start'), patch.object(self.app, 'wait_healthy', return_value=True), \
                    patch.object(self.app, 'opener', return_value=opener), \
                    patch.object(self.app, 'inspect', side_effect=[self.initial, self.initial, self.restarted]), \
                    patch.object(self.app, 'healthy', return_value=True), \
                    patch.object(self.app, 'admin_count', side_effect=[0, 1]), \
                    patch.object(controller.time, 'sleep'):
                self.app.start(self.generation)
            self.assertEqual(json.loads(self.marker.read_text()), {'status': 'complete'})
            self.assertEqual(self.marker.stat().st_mode & 0o777, 0o600)
            upload, restore = opener.requests
            self.assertEqual(upload.full_url, 'https://127.0.0.1:8443/api/cmd/backup')
            self.assertEqual(upload.get_header('X-requested-with'), 'XMLHttpRequest')
            self.assertEqual(upload.data.split(b'\r\n\r\n', 1)[1].rsplit(b'\r\n--', 1)[0], BACKUP)
            self.assertEqual(json.loads(restore.data), {'cmd': 'restore', 'backup': 'private-id'})
            self.assertEqual(restore.full_url, 'https://127.0.0.1:8443/api/cmd/system')
            # Completion follows the copied data, so neither restarts nor updates
            # reapply the backup over administrator changes made through UniFi.
            copy = dict(self.generation, data='gen-' + 'b' * 32)
            shutil.copytree(self.directory, self.app.data(copy))
            (self.app.data(copy) / 'ui-edit').write_text('preserve')
            with patch.object(controller.App, 'start'), patch.object(self.app, 'request') as request:
                self.app.start(copy)
                request.assert_not_called()
            shutil.rmtree(self.app.data(copy))

    def test_unsafe_restore_or_invalid_upload_is_never_accepted(self):
        for count, details in ((1, {'backup_id': 'id'}), (0, {}), (0, None), (0, []),
                               (0, {'backup_id': 3})):
            opener = FakeOpener({'meta': {'rc': 'ok'}, 'data': details})
            with self.subTest(count=count, details=details), patch.object(controller.App, 'start'), \
                    patch.object(self.app, 'wait_healthy', return_value=True), \
                    patch.object(self.app, 'opener', return_value=opener), \
                    patch.object(self.app, 'inspect', return_value=self.initial), \
                    patch.object(self.app, 'admin_count', return_value=count), self.assertRaises(RuntimeError):
                self.app.start(self.generation)
            self.assertEqual(json.loads(self.marker.read_text())['status'], 'pending')
            self.assertLessEqual(len(opener.requests), 1)

    def test_restore_acknowledgment_without_restart_never_marks_complete(self):
        opener = FakeOpener({'meta': {'rc': 'ok'}, 'data': {'backup_id': 'id'}}, {'meta': {'rc': 'ok'}})
        with patch.object(controller.App, 'start'), patch.object(self.app, 'wait_healthy', return_value=True), \
                patch.object(self.app, 'opener', return_value=opener), \
                patch.object(self.app, 'inspect', return_value=self.initial), \
                patch.object(self.app, 'admin_count', return_value=0), \
                patch.object(controller.time, 'monotonic', side_effect=[0, 9999]):
            with self.assertRaisesRegex(RuntimeError, 'did not restart'):
                self.app.start(self.generation)
        self.assertEqual(json.loads(self.marker.read_text())['status'], 'submitted')
        with patch.object(controller.App, 'start'), patch.object(self.app, 'request') as request:
            with self.assertRaisesRegex(RuntimeError, 'refusing to replay'):
                self.app.start(self.generation)
            request.assert_not_called()

    def test_private_api_errors_are_sanitized(self):
        opener = FakeOpener(ValueError('private-backup-metadata'))
        with patch.object(controller.App, 'start'), patch.object(self.app, 'wait_healthy', return_value=True), \
                patch.object(self.app, 'opener', return_value=opener), \
                patch.object(self.app, 'inspect', return_value=self.initial), \
                patch.object(self.app, 'admin_count', return_value=0):
            with self.assertRaises(RuntimeError) as error:
                self.app.start(self.generation)
            self.assertNotIn('private-backup-metadata', str(error.exception))

    def test_health_requires_running_controller_and_ready_json(self):
        for meta, expected in (({'rc': 'ok', 'up': True}, True), ({'rc': 'ok', 'up': False}, False),
                               ({'rc': 'error', 'up': True}, False), ({'rc': 'ok'}, False), (None, False)):
            opener = FakeOpener({'meta': meta})
            with self.subTest(meta=meta), patch.object(self.app, 'inspect', return_value=self.initial), \
                    patch.object(self.app, 'opener', return_value=opener):
                self.assertEqual(self.app.healthy(), expected)
                self.assertEqual(opener.requests[0].full_url, 'https://127.0.0.1:8443/status')
        with patch.object(self.app, 'inspect', return_value=None):
            self.assertFalse(self.app.healthy())

    def test_database_guard_does_not_treat_unknown_output_as_empty(self):
        with patch.object(controller, 'command', return_value='0') as cmd:
            self.assertEqual(self.app.admin_count(), 0)
            self.assertIn('db.admin.count()', cmd.call_args.args)
        with patch.object(controller, 'command', return_value='connection failed'):
            with self.assertRaisesRegex(RuntimeError, 'Cannot verify'):
                self.app.admin_count()

    def test_failed_initial_restore_rolls_back_and_discards_partial_database(self):
        # Exercise the real deployment journal around the restore operation.
        self.marker.unlink()
        shutil.rmtree(self.directory)
        with patch.object(controller.os, 'sync', create=True), patch.object(controller.App, 'start'), \
                patch.object(self.app, 'stop'), patch.object(self.app, 'remove'), \
                patch.object(self.app, 'wait_healthy', return_value=True), \
                patch.object(self.app, 'admin_count', return_value=0), \
                patch.object(self.app, 'inspect', return_value=self.initial), \
                patch.object(self.app, 'opener', return_value=FakeOpener({'meta': {'rc': 'error'}})), \
                patch.object(controller, 'command', return_value=1):
            with self.assertRaises(RuntimeError):
                self.app.deploy('server-old')
        self.assertIsNone(self.app.state['active'])
        self.assertIsNone(self.app.state['pending'])
        self.assertIn('server-old', self.app.state['failed'])
        self.assertEqual(list(self.app.root.glob('gen-*')), [])

    def test_power_loss_after_restore_submission_discards_generation_before_retry(self):
        class PowerLoss(BaseException):
            pass
        shutil.rmtree(self.directory)
        opener = FakeOpener({'meta': {'rc': 'ok'}, 'data': {'backup_id': 'id'}}, PowerLoss())
        with patch.object(controller.os, 'sync', create=True), patch.object(controller.App, 'start'), \
                patch.object(self.app, 'stop'), patch.object(self.app, 'wait_healthy', return_value=True), \
                patch.object(self.app, 'admin_count', return_value=0), \
                patch.object(self.app, 'inspect', return_value=self.initial), \
                patch.object(self.app, 'opener', return_value=opener):
            with self.assertRaises(PowerLoss):
                self.app.deploy('server-old')
        pending = self.app.state['pending']
        self.assertIsNotNone(pending)
        marker = self.app.data(pending) / '.home-stack-restore.json'
        self.assertEqual(json.loads(marker.read_text())['status'], 'submitted')
        recovered = controller.UnifiApp(self.config, self.app.app, self.root)
        with patch.object(recovered, 'stop'), patch.object(recovered, 'remove'), \
                patch.object(recovered, 'request') as request, \
                patch.object(controller, 'command', return_value=1):
            with self.assertRaisesRegex(RuntimeError, 'Recovered interrupted deployment'):
                recovered.reconcile()
            request.assert_not_called()
        self.assertIsNone(recovered.state['active'])
        self.assertIsNone(recovered.state['pending'])
        self.assertFalse(marker.parent.exists())

    def test_update_copies_restored_database_and_ui_edits_without_restoring_again(self):
        controller.atomic_json(self.marker, {'status': 'complete'})
        (self.directory / 'data').mkdir()
        (self.directory / 'data' / 'ui-edit').write_text('new-settings')
        self.app.state['active'] = self.generation
        self.app.save()
        def command(*args, **kwargs):
            if args[0] == 'cp':
                shutil.copytree(args[-2], args[-1])
            return 1
        with patch.object(controller.os, 'sync', create=True), patch.object(self.app, 'stop'), \
                patch.object(controller, 'command', side_effect=command), \
                patch.object(self.app, 'wait_healthy', return_value=True), \
                patch.object(self.app, 'request') as request:
            self.app.deploy('server-new')
            request.assert_not_called()
        active = self.app.data(self.app.state['active'])
        self.assertEqual((active / 'data' / 'ui-edit').read_text(), 'new-settings')
        self.assertEqual(json.loads((active / self.marker.name).read_text())['status'], 'complete')
        self.assertEqual(self.app.state['previous'], self.generation)


if __name__ == '__main__':
    unittest.main()
