import copy
import json
from pathlib import Path
import tempfile
import unittest

from home_stack.config import load
from home_stack.provision import environment

EXAMPLE = Path(__file__).resolve().parents[1] / 'config.example.json'


class ConfigurationTests(unittest.TestCase):
    def parse(self, config):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.json'
            path.write_text(json.dumps(config))
            return load(path)

    def test_defaults_and_literal_secrets(self):
        raw = json.loads(EXAMPLE.read_text())
        password = 'literal-$HOME-`test`-"-password'
        raw['apps'][0]['settings']['admin_password'] = password
        c = self.parse(raw)
        env = environment(c, c['apps'][0])
        self.assertEqual(env['FTLCONF_webserver_api_password'], password)
        self.assertEqual(env['FTLCONF_database_maxDBdays'], '0')
        self.assertEqual(env['FTLCONF_files_database'], '/dev/shm/pihole-FTL.db')
        self.assertEqual(env['FTLCONF_dns_queryLogging'], 'false')
        self.assertEqual(env['FTLCONF_misc_privacylevel'], '3')
        self.assertEqual(c['apps'][1]['network'], 'host')
        self.assertIsNone(c['network']['interface'])
        raw['network']['interface'] = 'end0'
        self.assertEqual(self.parse(raw)['network']['interface'], 'end0')

    def test_rejects_invalid_configuration(self):
        raw = json.loads(EXAMPLE.read_text())
        mutations = [
            lambda c: c.update(typo=True),
            lambda c: c.update(image_size_mib=True),
            lambda c: c.update(timezone='../../etc'),
            lambda c: c['network'].update(interface='eth0\n[ipv4]'),
            lambda c: c['network'].update(address='192.168.1.2/24'),
            lambda c: c['network'].update(host_dns=['127.0.0.1']),
            lambda c: c['apps'][0].update(name='../escape'),
            lambda c: c['apps'][0].update(kind={}),
            lambda c: c['apps'][0].update(image='pihole:latest'),
            lambda c: c['apps'][0].update(environment={'FTLCONF_dns_queryLogging': 'true'}),
            lambda c: c['apps'][0]['settings'].update(admin_password='secret\nINJECT=value'),
            lambda c: c['apps'][0]['settings'].update(web_port=8123),
            lambda c: c['apps'][0]['settings'].update(web_port=53),
            lambda c: c['apps'][0]['settings'].update(local_records=['1.2.3.4 home-stack-health.test']),
            lambda c: c['apps'][0].update(health={'kind': 'tcp', 'port': 80}),
            lambda c: c['apps'].append(copy.deepcopy(c['apps'][0])),
            lambda c: c['notifications'].update(webhook_url='http://example.com'),
            lambda c: c['updates'].update(container_calendar='daily\nUnit=bad'),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                config = copy.deepcopy(raw)
                mutate(config)
                with self.assertRaises(ValueError):
                    self.parse(config)

    def test_pihole_dns_settings_reach_container(self):
        raw = json.loads(EXAMPLE.read_text())
        raw['apps'][0]['settings'].update(dnssec=True, domain_needed=True,
                                          cache_optimizer=30, web_session_timeout=900)
        c = self.parse(raw)
        env = environment(c, c['apps'][0])
        self.assertEqual(env['FTLCONF_dns_dnssec'], 'true')
        self.assertEqual(env['FTLCONF_dns_domainNeeded'], 'true')
        self.assertEqual(env['FTLCONF_dns_cache_optimizer'], '30')
        self.assertEqual(env['FTLCONF_webserver_session_timeout'], '900')
        raw['apps'][0]['settings']['cache_optimizer'] = -1
        c = self.parse(raw)
        self.assertEqual(environment(c, c['apps'][0])['FTLCONF_dns_cache_optimizer'], '-1')

    def test_rejects_invalid_pihole_dns_settings(self):
        for key, value in [('dnssec', 'true'), ('domain_needed', 1),
                           ('cache_optimizer', True), ('cache_optimizer', -2),
                           ('cache_optimizer', 86401), ('web_session_timeout', False),
                           ('web_session_timeout', 0), ('web_session_timeout', 31536001)]:
            with self.subTest(key=key, value=value):
                raw = json.loads(EXAMPLE.read_text())
                raw['apps'][0]['settings'][key] = value
                with self.assertRaises(ValueError):
                    self.parse(raw)

    def test_generic_app_and_overrides(self):
        raw = json.loads(EXAMPLE.read_text())
        raw['apps'].append({'name': 'example', 'kind': 'container', 'image': 'ghcr.io/example/service:stable',
                            'environment': {'MODE': 'safe'}, 'ports': ['8088:80/tcp'],
                            'health': {'kind': 'http', 'url': 'http://127.0.0.1:8088/'}})
        parsed = self.parse(raw)
        self.assertEqual(parsed['apps'][-1]['data_path'], '/data')
        self.assertEqual(parsed['apps'][-1]['environment'], {'MODE': 'safe'})
        self.assertIsNone(load(EXAMPLE, ['updates.os_calendar=null'])['updates']['os_calendar'])
