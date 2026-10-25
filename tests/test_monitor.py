"""Exercise DNS parsing and outage state transitions, without desktop side effects."""
import os
import plistlib
from pathlib import Path
import subprocess
import tempfile
import sys
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/macos/pihole-monitor.sh'


class MonitorTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == 'darwin', 'requires native plutil')
    def test_install_reconfigure_and_uninstall_in_private_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data, agents = root / 'data with spaces', root / 'agents'
            env = dict(os.environ, HOME_STACK_MONITOR_DIRECTORY=str(data), HOME_STACK_AGENT_DIRECTORY=str(agents), MONITOR_SOURCE=str(SCRIPT))
            setup = 'source "$MONITOR_SOURCE"\nlaunchctl_command() { return 0; }\n'
            def run(body):
                result = subprocess.run(['bash', '-c', setup + body], env=env, text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)
            run('install_monitor --server 192.0.2.53 --interval 90 --failures 4')
            plist = agents / 'local.home-stack.pihole-monitor.plist'
            definition = plistlib.loads(plist.read_bytes())
            self.assertEqual(definition['StartInterval'], 90)
            self.assertEqual(definition['ProgramArguments'][1:], [str(data / 'monitor.sh'), 'check'])
            self.assertEqual((data / 'config').stat().st_mode & 0o777, 0o600)
            self.assertEqual((data / 'monitor.sh').read_bytes(), SCRIPT.read_bytes())
            (data / 'state').write_text('3 1 1000\n')
            run('install_monitor --server 192.0.2.54')
            self.assertFalse((data / 'state').exists())
            self.assertEqual((data / 'config').read_text().splitlines()[0], '192.0.2.54')
            (data / 'unrelated').write_text('preserve me')
            run('uninstall_monitor')
            self.assertFalse(plist.exists())
            self.assertFalse((data / 'monitor.sh').exists())
            self.assertEqual((data / 'unrelated').read_text(), 'preserve me')

    def run_shell(self, body):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, HOME_STACK_MONITOR_DIRECTORY=tmp, MONITOR_SOURCE=str(SCRIPT))
            setup = '''source "$MONITOR_SOURCE"
server=192.0.2.53 domain=example.com expected='' interval=60 threshold=3
failures=0 announced=0 last=1000 now=1060
notify() { printf '%s\\n' notification >> "$MONITOR_DIRECTORY/events"; }
'''
            result = subprocess.run(['bash', '-c', setup + body], env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout

    def test_threshold_single_outage_and_recovery(self):
        self.run_shell('''
record_result failed
[[ "$failures" == 1 && "$announced" == 0 ]]
last=$now; now=1120; record_result failed
[[ "$failures" == 2 && ! -f "$MONITOR_DIRECTORY/events" ]]
last=$now; now=1180; record_result failed
[[ "$announced" == 1 ]]
last=$now; now=1240; record_result failed
[[ $(wc -l < "$MONITOR_DIRECTORY/events") -eq 1 ]]
last=$now; now=1300; record_result healthy
[[ "$failures" == 0 && "$announced" == 0 ]]
[[ $(wc -l < "$MONITOR_DIRECTORY/events") -eq 2 ]]
read -r saved_failures saved_announced saved_time < "$MONITOR_DIRECTORY/state"
[[ "$saved_failures" == 0 && "$saved_announced" == 0 && "$saved_time" == 1300 ]]
''')

    def test_success_and_sleep_reset_consecutive_failures(self):
        self.run_shell('''
record_result failed
record_result failed
record_result healthy
record_result failed
[[ "$failures" == 1 && "$announced" == 0 ]]
failures=2; now=5000; record_result failed
[[ "$failures" == 1 && "$announced" == 0 ]]
[[ ! -f "$MONITOR_DIRECTORY/events" ]]
''')

    def test_failed_notification_is_retried(self):
        self.run_shell('''
notify() { return 1; }
failures=2; record_result failed
[[ "$announced" == 0 ]]
notify() { return 0; }
record_result failed
[[ "$announced" == 1 ]]
''')

    def test_dns_requires_valid_answer_from_both_transports(self):
        self.run_shell('''
dns_query() { printf 'alias.example.com.\\n192.0.2.1\\n'; }
probe
expected=192.0.2.2
if probe; then exit 20; fi
expected=192.0.2.1
dns_query() { if [[ "$*" == *+tcp* ]]; then return 1; fi; printf '192.0.2.1\\n'; }
if probe; then exit 21; fi
dns_query() { printf ';; connection timed out\\n999.0.0.1\\n'; }
if probe; then exit 22; fi
dns_query() { return 0; }
if probe; then exit 23; fi
''')
