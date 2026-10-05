import os
import subprocess
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch
from linux_doctor.diagnostics import diagnose_sharing, environment_context, run, scan


def probe(output='', ok=True, available=True, outcome=None):
    return {'available': available, 'ok': ok, 'output': output,
            'outcome': outcome or ('missing' if not available else 'ok' if ok else 'error')}


def sharing_data(portal=None, pipewire=None, backends=None):
    return {'session': 'wayland', 'desktop': 'GNOME', 'apps': ['com.discordapp.Discord'],
            'logSince': '2026-10-04 09:00:00 UTC', 'environment': {'context': 'local graphical session', 'warnings': []},
            'probes': {'portal': portal or probe('active'), 'pipewire': pipewire or probe('active'),
                       'backends': backends or probe('xdg-desktop-portal-gnome.service loaded active running Portal service'),
                       'apps': probe('com.discordapp.Discord'), 'logs': probe('An observed log line')}}


class DiagnosticsTests(unittest.TestCase):
    def test_missing_command_is_not_success(self):
        with patch('linux_doctor.diagnostics.shutil.which', return_value=None):
            result = run(['absent'])
        self.assertFalse(result['available'])
        self.assertFalse(result['ok'])
        self.assertEqual(result['outcome'], 'missing')

    @patch('linux_doctor.diagnostics.shutil.which', return_value='/usr/bin/tool')
    def test_probe_records_timeout_separately_from_command_error(self, *_):
        with patch('linux_doctor.diagnostics.subprocess.run', side_effect=subprocess.TimeoutExpired(['tool'], 5)):
            result = run(['tool'])
        self.assertEqual(result['outcome'], 'timeout')
        self.assertTrue(result['available'])
        self.assertFalse(result['ok'])
        with patch('linux_doctor.diagnostics.subprocess.run', return_value=Mock(returncode=1, stdout='', stderr='Access denied')):
            result = run(['tool'])
        self.assertEqual(result['outcome'], 'error')
        self.assertEqual(result['output'], 'Access denied')

    @patch('linux_doctor.diagnostics.shutil.which', return_value='/usr/bin/tool')
    def test_probe_success_is_bounded_and_uses_no_shell(self, *_):
        with patch('linux_doctor.diagnostics.subprocess.run', return_value=Mock(returncode=0, stdout='x' * 20000, stderr='')) as command:
            result = run(['tool', 'argument'])
        self.assertEqual(result['outcome'], 'ok')
        self.assertEqual(len(result['output']), 14000)
        self.assertEqual(command.call_args.args[0], ['tool', 'argument'])
        self.assertEqual(command.call_args.kwargs['timeout'], 5)
        self.assertFalse(command.call_args.kwargs.get('shell', False))

    @patch('linux_doctor.diagnostics.glob.glob', return_value=[])
    @patch('linux_doctor.diagnostics.run', return_value={'available': False, 'ok': False, 'output': 'unavailable'})
    def test_missing_tools_do_not_invent_failures(self, *_):
        data = scan()
        checks = {c['id']: c for c in data['checks']}
        for id in ('graphics', 'portals', 'audio', 'integration', 'power'):
            self.assertEqual(checks[id]['status'], 'unknown')
        self.assertEqual(data['sharing']['status'], 'unknown')
        self.assertEqual(data['apps'], [])
        self.assertTrue(all(p['outcome'] == 'missing' for p in data['probes'].values()))

    @patch('linux_doctor.diagnostics.glob.glob', return_value=[])
    def test_active_services_do_not_prove_screen_sharing(self, *_):
        with patch('linux_doctor.diagnostics.run', return_value={'available': True, 'ok': True, 'output': 'active'}):
            data = scan()
        self.assertEqual(data['sharing']['status'], 'unknown')
        self.assertEqual(next(c for c in data['checks'] if c['id'] == 'portals')['status'], 'healthy')

    @patch('linux_doctor.diagnostics.glob.glob', return_value=[])
    def test_unrelated_driver_does_not_prove_graphics(self, *_):
        output = '00:02.0 VGA compatible controller: Example GPU\n00:03.0 Ethernet controller: Example NIC\n\tKernel driver in use: example_net'
        with patch('linux_doctor.diagnostics.run', return_value={'available': True, 'ok': True, 'output': output}):
            data = scan()
        graphics = next(c for c in data['checks'] if c['id'] == 'graphics')
        self.assertEqual(graphics['status'], 'unknown')
        self.assertNotIn('example_net', graphics['evidence'])

    @patch('linux_doctor.diagnostics.glob.glob', return_value=[])
    def test_scan_collects_backend_and_reproduction_log_evidence(self, *_):
        def command(argv):
            if argv[0] == 'flatpak':
                return probe('com.discordapp.Discord\norg.mozilla.firefox\ncom.discordapp.Discord\nApplication\n')
            if 'list-units' in argv:
                return probe('xdg-desktop-portal-gnome.service loaded active running Portal for GNOME')
            if 'is-active' in argv:
                return probe('active')
            return probe('evidence')
        with patch('linux_doctor.diagnostics.run', side_effect=command) as commands:
            data = scan('2026-10-04T12:00:00+03:00')
        self.assertEqual(data['apps'], ['com.discordapp.Discord', 'org.mozilla.firefox'])
        self.assertEqual(data['logSince'], '2026-10-04 09:00:00 UTC')
        journal = next(call.args[0] for call in commands.call_args_list if call.args[0][0] == 'journalctl')
        self.assertEqual(journal[journal.index('--since') + 1], data['logSince'])
        self.assertIn('xdg-desktop-portal-*.service', journal)
        self.assertIn('pipewire.service', journal)
        self.assertEqual(journal[journal.index('-n') + 1], '60')
        self.assertIn('xdg-desktop-portal-gnome.service', data['sharing']['evidence'])
        self.assertIn('An app-level test', data['sharing']['summary'])

    @patch('linux_doctor.diagnostics.glob.glob', return_value=[])
    def test_failed_portal_is_attention_but_inactive_is_not(self, *_):
        for state, expected in [('failed', 'warning'), ('inactive', 'unknown')]:
            def command(argv):
                return probe(state, ok=False) if 'is-active' in argv else probe('')
            with self.subTest(state=state), patch('linux_doctor.diagnostics.run', side_effect=command):
                data = scan()
                portals = next(c for c in data['checks'] if c['id'] == 'portals')
                self.assertEqual(portals['status'], expected)
                self.assertEqual(data['sharing']['status'], expected)

    def test_unknown_probe_states_do_not_invent_failed_services(self):
        cases = [probe('inactive', ok=False), probe('systemctl is not installed.', ok=False, available=False),
                 probe('systemctl timed out after 5 seconds.', ok=False, outcome='timeout'),
                 probe('Failed to connect to bus: No medium found', ok=False), probe('failed', ok=True)]
        for state in cases:
            with self.subTest(state=state):
                result = diagnose_sharing(sharing_data(portal=state), {})
                self.assertEqual(result['status'], 'unknown')
                self.assertEqual(result['confidence'], 'low')

    def test_failed_backend_is_concrete_service_evidence(self):
        data = sharing_data(backends=probe('xdg-desktop-portal-gnome.service loaded failed failed Portal for GNOME'))
        result = diagnose_sharing(data, {})
        self.assertEqual(result['status'], 'warning')
        self.assertEqual(result['confidence'], 'high')
        self.assertIn('xdg-desktop-portal-gnome.service', result['finding'])
        self.assertIn('does not establish', result['explanation'])

    def test_picker_outcomes_report_user_observation_not_permissions(self):
        for picker, status, summary in [('untried', 'unknown', 'An app-level test'),
                                        ('absent', 'warning', 'No screen picker'),
                                        ('failed', 'warning', 'Sharing failed after'),
                                        ('worked', 'healthy', 'worked in your test')]:
            answers = {'app': 'com.discordapp.Discord', 'packaging': 'flatpak', 'picker': picker}
            with self.subTest(picker=picker):
                result = diagnose_sharing(sharing_data(), answers)
                self.assertEqual(result['status'], status)
                self.assertIn(summary, result['summary'])
                self.assertIn('com.discordapp.Discord', result['evidence'])
                self.assertIn('User-reported outcome: ' + picker, result['evidence'])
                if picker == 'worked':
                    self.assertIn('did not independently verify', result['explanation'])
                elif picker in ('absent', 'failed'):
                    self.assertIn('permission', result['explanation'])

    def test_native_or_browser_answers_do_not_imply_flatpak_permission(self):
        for packaging in ('native', 'browser', 'unknown'):
            result = diagnose_sharing(sharing_data(), {'app': 'Example', 'packaging': packaging, 'picker': 'failed'})
            self.assertFalse(any('Flatpak app' in step for step in result['steps']))
            self.assertIn('Packaging: ' + packaging, result['evidence'])

    def test_successful_test_does_not_erase_observed_service_failure(self):
        result = diagnose_sharing(sharing_data(pipewire=probe('failed', ok=False)), {'picker': 'worked'})
        self.assertEqual(result['status'], 'warning')
        self.assertIn('You reported that sharing worked', result['explanation'])

    @patch('linux_doctor.diagnostics.Path.exists', return_value=False)
    def test_headless_and_ssh_environment_limits_are_explicit(self, *_):
        with patch.dict(os.environ, {'SSH_CONNECTION': 'example'}, clear=True):
            context = environment_context()
        self.assertIn('SSH', context['context'])
        self.assertIn('headless', context['context'])
        self.assertEqual(len(context['warnings']), 2)
        with patch.dict(os.environ, {'XDG_SESSION_TYPE': 'wayland', 'WAYLAND_DISPLAY': 'wayland-0'}, clear=True):
            context = environment_context()
        self.assertEqual(context['warnings'], [])

    @patch('linux_doctor.diagnostics.glob.glob', return_value=[])
    def test_datetime_marker_is_supported(self, *_):
        with patch('linux_doctor.diagnostics.run', return_value=probe('', ok=False, available=False)):
            data = scan(datetime(2026, 10, 4, 9, tzinfo=timezone.utc))
        self.assertEqual(data['logSince'], '2026-10-04 09:00:00 UTC')


if __name__ == '__main__':
    unittest.main()
