import copy
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from linux_doctor.report import format_changes, format_report, redact_text, save_report


CHECK = {
    'id': 'sharing', 'name': 'Screen sharing', 'status': 'unknown',
    'summary': 'Test needed', 'explanation': 'A running service is not an app test.',
    'finding': 'Possible cause', 'confidence': 'low',
    'steps': ['Start a screen share.'], 'evidence': 'No application test yet.',
}
DATA = {
    'scannedAt': '2026-10-04T09:00:00Z', 'system': 'Linux', 'kernel': 'test',
    'desktop': 'GNOME', 'session': 'wayland', 'checks': [], 'sharing': CHECK,
}


class ExportTests(unittest.TestCase):
    def test_personal_information_is_redacted_without_mutating_scan(self):
        data = copy.deepcopy(DATA)
        data['sharing']['evidence'] = (
            'aliceperson on dev-machine and dev-machine.local; /home/aliceperson/.config/discord; '
            '/home/anotherperson/log; /var/home/otherperson/app; '
            '/run/user/1000/bus; contact first.last+label@example.org'
        )
        original = copy.deepcopy(data)
        with patch('linux_doctor.report.Path.home', return_value=Path('/home/aliceperson')), \
                patch('linux_doctor.report.getpass.getuser', return_value='aliceperson'), \
                patch('linux_doctor.report.socket.gethostname', return_value='dev-machine'):
            report = format_report(data)
        for private in ('aliceperson', 'dev-machine', 'anotherperson', 'otherperson', '1000', 'first.last+label@example.org'):
            self.assertNotIn(private, report)
        self.assertIn('<home>/.config/discord', report)
        self.assertIn('/var/home/<user>/app', report)
        self.assertIn('<host>.local', report)
        self.assertIn('/run/user/<user-id>/bus', report)
        self.assertIn('best effort; review before sharing', report)
        self.assertEqual(data, original)

    def test_common_and_tiny_identity_names_do_not_destroy_report(self):
        for user, host in [('user', 'linux'), ('a', 'pc'), ('discord', 'gnome'), ('root', 'localhost')]:
            with self.subTest(user=user, host=host), \
                    patch('linux_doctor.report.Path.home', return_value=Path('/')), \
                    patch('linux_doctor.report.getpass.getuser', return_value=user), \
                    patch('linux_doctor.report.socket.gethostname', return_value=host):
                text = 'user linux a pc discord gnome root localhost; /home/user/.config/discord'
                redacted = redact_text(text)
                self.assertIn('user linux a pc discord gnome root localhost', redacted)
                self.assertIn('/home/<user>/.config/discord', redacted)
                self.assertIn('Screen sharing', format_report(DATA))

    def test_password_token_and_credential_punctuation_is_redacted(self):
        samples = [
            ('password=one!@#$%^&*()[]{};,', 'one!@#'),
            ('PASSWORD="two space; and punctuation!"', 'two space'),
            ("passwd='three \\' quote'", 'three'),
            ('{"access_token":"four.secret", "api_key": "five"}', 'four.secret'),
            ('{"access_token":"four.secret", "api_key": "five"}', 'five'),
            ('OPENAI_API_KEY=six!@#', 'six'),
            ('AWS_SECRET_ACCESS_KEY=seven/+=', 'seven'),
            ('GITHUB_TOKEN=eight', 'eight'),
            ('X-Auth-Token: nine', 'nine'),
            ('Authorization: Bearer ten.token.value', 'ten.token.value'),
            ('authorization="Basic eleven=="', 'eleven'),
            ('https://privateuser:twelve@chars@example.org/path', 'twelve'),
        ]
        for sample, private in samples:
            with self.subTest(sample=sample):
                output = redact_text(sample)
                self.assertNotIn(private, output)
                self.assertIn('<redacted>', output)

    def test_metadata_and_guided_observations_are_exported(self):
        data = {
            **DATA,
            'environment': {'context': 'SSH', 'warnings': ['Desktop session may not be accessible.']},
            'probes': {'portal': {'available': True, 'ok': False, 'outcome': 'timeout', 'output': 'raw tool output'},
                       'apps': {'available': False, 'ok': False, 'outcome': 'missing'}},
            'troubleshooting': {'app': 'Discord', 'packaging': 'Flatpak', 'picker': 'not_shown'},
            'changes': ['Portal service became active.'],
        }
        report = format_report(data)
        for expected in ('Context: SSH', 'Desktop session may not be accessible.',
                         'portal: available; timeout', 'apps: unavailable; missing',
                         'App: Discord', 'Packaging: Flatpak', 'Picker: not_shown',
                         'Finding: Possible cause', 'Confidence: low',
                         'Portal service became active.'):
            self.assertIn(expected, report)
        self.assertNotIn('raw tool output', report)

    def test_terminal_controls_in_all_fields_are_inert(self):
        data = {**DATA, 'system': '\x1b[31mLinux', 'environment': {'context': '\x07remote'}}
        report = format_report(data)
        self.assertNotIn('\x1b', report)
        self.assertNotIn('\x07', report)
        self.assertIn('\\u001b[31mLinux', report)

    def test_exports_are_private_and_never_overwrite(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'report.txt'
            self.assertEqual(save_report(DATA, path), path)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            original = path.read_text()
            with self.assertRaises(FileExistsError):
                save_report({**DATA, 'system': 'Changed'}, path)
            self.assertEqual(path.read_text(), original)

    def test_existing_symlink_is_not_followed(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'target.txt'
            target.write_text('keep me')
            link = Path(folder) / 'report.txt'
            link.symlink_to(target)
            with self.assertRaises(FileExistsError):
                save_report(DATA, link)
            self.assertEqual(target.read_text(), 'keep me')


class ChangeTests(unittest.TestCase):
    def test_first_scan_explains_how_to_compare(self):
        self.assertIn('No previous scan', format_changes(None, DATA)[0])

    def test_volatile_timestamps_logs_and_watts_do_not_count_as_changes(self):
        previous = copy.deepcopy(DATA)
        previous['checks'] = [{**CHECK, 'id': 'power', 'name': 'Power', 'summary': 'Discharging at 5.4 W'}]
        previous['probes'] = {'logs': {'available': True, 'ok': True, 'output': 'old log'}}
        current = copy.deepcopy(previous)
        current['scannedAt'] = '2026-10-04T10:00:00Z'
        current['checks'][0]['summary'] = 'Discharging at 7.8 W'
        current['sharing']['evidence'] = 'new logs'
        current['probes']['logs']['output'] = 'new log'
        self.assertEqual(format_changes(previous, current), ['No diagnostic changes since the previous scan.'])

    def test_semantic_check_and_environment_changes_are_described(self):
        previous = copy.deepcopy(DATA)
        previous['environment'] = {'context': 'desktop', 'warnings': []}
        current = copy.deepcopy(previous)
        current['sharing'].update(status='warning', summary='Sharing failed', finding='Picker did not appear')
        current['desktop'] = 'KDE'
        current['environment']['context'] = 'SSH'
        output = '\n'.join(format_changes(previous, current))
        self.assertIn('Desktop: GNOME -> KDE', output)
        self.assertIn('Environment context: desktop -> SSH', output)
        self.assertIn('Screen sharing: unknown; Test needed', output)
        self.assertIn('warning; Sharing failed; Picker did not appear', output)

    def test_app_changes_are_detected_without_order_noise(self):
        previous = {**DATA, 'apps': ['org.mozilla.firefox', 'com.discordapp.Discord']}
        current = {**DATA, 'apps': ['com.discordapp.Discord', 'org.mozilla.firefox']}
        self.assertEqual(format_changes(previous, current), ['No diagnostic changes since the previous scan.'])
        current['apps'] = ['com.discordapp.Discord']
        self.assertIn('Applications:', '\n'.join(format_changes(previous, current)))

    def test_probe_outcomes_and_added_removed_checks_are_visible(self):
        previous = {**DATA, 'checks': [{**CHECK, 'id': 'audio', 'name': 'Audio'}],
                    'probes': {'portal': {'available': True, 'ok': False, 'outcome': 'timeout'}}}
        current = {**DATA, 'checks': [{**CHECK, 'id': 'graphics', 'name': 'Graphics'}],
                   'probes': {'portal': {'available': True, 'ok': True, 'outcome': 'success'}}}
        output = '\n'.join(format_changes(previous, current))
        self.assertIn('Audio: check no longer available', output)
        self.assertIn('Graphics: check added', output)
        self.assertIn('Probe portal: available; timeout -> available; success', output)


if __name__ == '__main__':
    unittest.main()
