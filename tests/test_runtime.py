import copy
import json
from pathlib import Path
import shutil
import struct
import tempfile
import time
import unittest
from unittest.mock import patch

from home_stack.config import load
from runtime import controller as runtime

EXAMPLE = Path(__file__).resolve().parents[1] / 'config.example.json'


class PowerLoss(BaseException):
    pass


class FakeApp(runtime.App):
    running = None
    unhealthy = set()
    crash = None

    def stop(self):
        self.running = None

    def start(self, generation):
        self.running = generation
        # Simulate an incompatible database migration by the new image.
        database = self.data(generation) / 'database'
        if generation['image'] == 'old' and database.exists() and database.read_text() != 'old':
            raise RuntimeError('Old release cannot read the migrated schema')
        database.write_text(generation['image'])
        if generation['image'] == self.crash:
            raise PowerLoss()

    def wait_healthy(self):
        return self.running['image'] not in self.unhealthy

    def healthy(self):
        return self.running is not None and self.wait_healthy()

    def matches(self, generation):
        return self.running == generation

    def image(self, reference):
        return 'new'


def fake_command(*args, **kwargs):
    if args[0] == 'cp':
        shutil.copytree(args[-2], args[-1])
    return 1 if kwargs.get('check') is False else ''


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = load(EXAMPLE)
        self.app = FakeApp(self.config, self.config['apps'][0], self.root)
        self.command = patch.object(runtime, 'command', side_effect=fake_command)
        self.command.start()
        self.addCleanup(self.command.stop)
        self.sync = patch.object(runtime.os, 'sync', create=True)
        self.sync.start()
        self.addCleanup(self.sync.stop)

    def test_success_preserves_previous_image_and_data(self):
        self.app.deploy('old')
        old = copy.deepcopy(self.app.state['active'])
        self.app.deploy('new')
        self.assertEqual(self.app.state['previous'], old)
        self.assertEqual((self.app.data(old) / 'database').read_text(), 'old')
        self.assertEqual(self.app.state['active']['image'], 'new')
        self.assertIsNone(self.app.state['pending'])

    def test_failed_migration_restores_previous_data(self):
        self.app.deploy('old')
        old = copy.deepcopy(self.app.state['active'])
        self.app.unhealthy = {'new'}
        with self.assertRaises(RuntimeError):
            self.app.deploy('new')
        self.assertEqual(self.app.state['active'], old)
        self.assertEqual((self.app.data(old) / 'database').read_text(), 'old')
        self.assertTrue(self.app.cooldown('new'))
        self.assertIsNone(self.app.state['pending'])
        self.assertEqual(len(list(self.app.root.glob('gen-*'))), 1)

    def test_power_loss_during_candidate_recovers_from_durable_journal(self):
        self.app.deploy('old')
        self.app.crash = 'new'
        with self.assertRaises(PowerLoss):
            self.app.deploy('new')
        recovered = FakeApp(self.config, self.config['apps'][0], self.root)
        self.assertEqual(recovered.state['pending']['image'], 'new')
        with self.assertRaisesRegex(RuntimeError, 'interrupted'):
            recovered.reconcile()
        self.assertEqual(recovered.state['active']['image'], 'old')
        self.assertIsNone(recovered.state['pending'])
        self.assertEqual((recovered.data(recovered.state['active']) / 'database').read_text(), 'old')

    def test_failed_rollback_keeps_pending_journal(self):
        self.app.deploy('old')
        self.app.unhealthy = {'new', 'old'}
        with self.assertRaisesRegex(RuntimeError, 'Rollback failed'):
            self.app.deploy('new')
        loaded = json.loads(self.app.path.read_text())
        self.assertEqual(loaded['pending']['image'], 'new')
        self.assertEqual(loaded['active']['image'], 'old')
        self.app.unhealthy = set()
        self.app.rollback_pending()
        self.assertIsNone(self.app.state['pending'])

    def test_copy_failure_restarts_old_without_candidate(self):
        self.app.deploy('old')
        old = copy.deepcopy(self.app.state['active'])
        def fail_copy(*args, **kwargs):
            if args[0] == 'cp':
                raise OSError('disk full')
            return fake_command(*args, **kwargs)
        with patch.object(runtime, 'command', side_effect=fail_copy), self.assertRaises(OSError):
            self.app.deploy('new')
        self.assertEqual(self.app.running, old)
        self.assertEqual(json.loads(self.app.path.read_text())['active'], old)

    def test_pull_failure_never_stops_active(self):
        self.app.deploy('old')
        old = copy.deepcopy(self.app.state['active'])
        with patch.object(runtime, 'command', side_effect=RuntimeError('offline')):
            with self.assertRaises(RuntimeError):
                self.app.update()
        self.assertEqual(self.app.running, old)
        self.assertEqual(self.app.state['active'], old)

    def test_rejected_image_is_not_redeployed_during_cooldown(self):
        self.app.deploy('old')
        self.app.state['failed']['new'] = time.time()
        with patch.object(self.app, 'deploy') as deploy:
            with self.assertRaisesRegex(RuntimeError, 'cooldown'):
                self.app.update()
            deploy.assert_not_called()
        self.assertEqual(self.app.state['active']['image'], 'old')

    def test_cleanup_retains_two_generations(self):
        for image in ('one', 'two', 'three'):
            self.app.deploy(image)
        self.assertEqual(len(list(self.app.root.glob('gen-*'))), 2)
        self.assertEqual(self.app.state['previous']['image'], 'two')

    def test_late_failure_rolls_back_once(self):
        self.app.deploy('old')
        self.app.deploy('new')
        self.app.unhealthy = {'new'}
        with self.assertRaisesRegex(RuntimeError, 'rolled back'):
            self.app.reconcile()
        self.assertEqual(self.app.state['active']['image'], 'old')
        self.assertIsNone(self.app.state['previous'])
        self.assertTrue(self.app.cooldown('new'))

    def test_invalid_state_cannot_escape_data_tree(self):
        with self.assertRaises(ValueError):
            self.app.data({'data': 'gen-../../outside'})

    def test_missing_data_is_not_silently_replaced(self):
        with patch.object(runtime, 'command') as command:
            with self.assertRaisesRegex(RuntimeError, 'data missing'):
                runtime.App.start(self.app, {'data': 'gen-' + 'a' * 32, 'image': 'old'})
            command.assert_not_called()

    def test_cleanup_protects_another_apps_rollback_image(self):
        self.app.deploy('old')
        other = FakeApp(self.config, self.config['apps'][1], self.root)
        other.deploy('shared')
        other.deploy('other-new')
        self.app.state['images'].append('shared')
        with patch.object(runtime, 'command', return_value=0) as command:
            self.app.cleanup()
        self.assertFalse(any(call.args[:3] == ('podman', 'image', 'rm') and call.args[3] == 'shared'
                             for call in command.call_args_list))

    def test_other_apps_continue_after_failure(self):
        visited = []
        def update(app):
            visited.append(app.app['name'])
            if app.app['name'] == 'pihole':
                raise RuntimeError('candidate unhealthy')
        with patch.object(runtime.App, 'update', update):
            self.assertEqual(runtime.operate(self.config, 'update', self.root), 1)
        self.assertEqual(visited, ['pihole', 'homeassistant', 'immich', 'seafile'])
        faults = json.loads((self.root / 'status.json').read_text())['faults']
        self.assertEqual(list(faults), ['pihole:update'])

    def test_reconcile_cannot_clear_rejected_update_fault(self):
        status = runtime.Status(self.config, self.root)
        status.record('pihole:update', 'bad version')
        with patch.object(runtime.App, 'reconcile'):
            runtime.operate(self.config, 'reconcile', self.root)
        self.assertIn('pihole:update', json.loads(status.path.read_text())['faults'])

    def test_notifications_are_change_only_and_retryable(self):
        status = runtime.Status(self.config, self.root)
        status.record('pihole:update', 'bad version')
        before = status.path.read_bytes()
        status.record('pihole:update', 'bad version')
        self.assertEqual(status.path.read_bytes(), before)
        config = copy.deepcopy(self.config)
        config['notifications']['webhook_url'] = 'https://example.test/hook'
        status.config = config
        with patch.object(runtime.urllib.request, 'build_opener') as opener:
            opener.return_value.open.side_effect = OSError('offline')
            status.flush()
        self.assertIn('pihole:update', status.state['outbox'])
        status.record('pihole:update')
        self.assertEqual(status.state['outbox']['pihole:update']['status'], 'recovered')
        self.assertFalse(status.state['faults'])


class DNSTests(unittest.TestCase):
    def test_parses_address_and_rejects_mismatched_or_truncated_response(self):
        packet = struct.pack('!6H', 42, 0x8180, 1, 1, 0, 0)
        packet += b'\x01a\x04test\0' + struct.pack('!HH', 1, 1)
        packet += b'\xc0\x0c' + struct.pack('!HHIH', 1, 1, 60, 4) + b'\xc0\x00\x02\x01'
        self.assertEqual(runtime.dns_answers(packet, 42), ['192.0.2.1'])
        with self.assertRaises(ValueError):
            runtime.dns_answers(packet, 43)
        with self.assertRaises(ValueError):
            runtime.dns_answers(packet[:-1], 42)
