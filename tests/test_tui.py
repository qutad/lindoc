import curses
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from linux_doctor.report import format_report, safe_text, save_report
from linux_doctor.tui import DoctorTUI, cell_width, detail_lines

CHECK = {'id': 'sharing', 'name': 'Screen sharing', 'status': 'unknown', 'summary': 'Test needed', 'explanation': 'Services alone do not prove screen sharing works.', 'evidence': '\x1b[2JAn untrusted log\x07', 'steps': ['Start a screen share.']}
DATA = {'scannedAt': '2026-10-03T20:00:00Z', 'system': 'Linux', 'kernel': 'test', 'desktop': 'GNOME', 'session': 'Wayland', 'checks': [], 'sharing': CHECK}


class ReportTests(unittest.TestCase):
    def test_reports_include_evidence_advice_and_uncertainty(self):
        report = format_report(DATA)
        self.assertIn('[?] Screen sharing - Not verified', report)
        self.assertIn('1. Start a screen share.', report)
        self.assertIn('An untrusted log', report)
        self.assertNotIn('\x1b', report)
        self.assertNotIn('\x07', report)

    def test_report_is_private_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'report.txt'
            save_report(DATA, path)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                save_report(DATA, path)
            self.assertEqual(path.read_text(), format_report(DATA))

    def test_wrapping_preserves_evidence_without_terminal_controls(self):
        lines = detail_lines(CHECK, 20)
        self.assertTrue(all(len(line) <= 20 for line in lines))
        self.assertNotIn('\x1b', '\n'.join(lines))
        self.assertIn('EVIDENCE', lines)
        self.assertEqual(safe_text('a\nb\tc'), 'a\nb\tc')

    def test_wide_evidence_fits_terminal_cells(self):
        evidence = '界' * 27
        lines = detail_lines({**CHECK, 'evidence': evidence}, 20)
        self.assertTrue(all(sum(cell_width(c) for c in line) <= 20 for line in lines))
        self.assertEqual(''.join(lines[lines.index('EVIDENCE') + 1:]), evidence)


class TUITests(unittest.TestCase):
    def setUp(self):
        self.screen = Mock()
        self.screen.getmaxyx.return_value = (24, 80)
        self.ui = DoctorTUI(self.screen)
        self.ui.data = DATA
        self.addCleanup(self.ui.pool.shutdown)

    def test_navigation_opens_scrolls_and_returns(self):
        self.ui.handle_key(10)
        self.assertTrue(self.ui.detail)
        self.ui.handle_key(curses.KEY_NPAGE)
        self.assertEqual(self.ui.offset, 12)
        self.ui.handle_key(27)
        self.assertFalse(self.ui.detail)
        self.assertEqual(self.ui.offset, 0)
        self.assertFalse(self.ui.handle_key(ord('q')))

    def test_small_terminal_remains_usable(self):
        self.screen.getmaxyx.return_value = (8, 30)
        self.ui.draw()
        self.assertTrue(self.screen.refresh.called)
        self.assertFalse(self.ui.handle_key(ord('q')))

    def test_scan_error_preserves_previous_data(self):
        self.ui.future = Mock()
        self.ui.future.done.return_value = True
        self.ui.future.result.side_effect = RuntimeError('scan error')
        self.ui.collect()
        self.assertIs(self.ui.data, DATA)
        self.assertIn('Scan failed', self.ui.message)
        self.assertIsNone(self.ui.future)

    def test_guided_flatpak_flow_marks_logs_and_collects_outcome(self):
        self.ui.data = {**DATA, 'apps': ['com.example.Chat']}
        self.ui.handle_key('t')
        self.assertEqual(self.ui.panel, 'wizard')
        self.ui.handle_key('\n')
        self.assertEqual(self.ui.wizard_phase, 'outcome')
        self.assertIsNotNone(self.ui.log_since)
        self.ui.handle_key('j')
        with patch.object(self.ui, 'start_scan') as start_scan:
            self.ui.handle_key('\n')
        start_scan.assert_called_once()
        self.assertEqual(self.ui.answers, {'app': 'com.example.Chat', 'packaging': 'flatpak', 'picker': 'failed'})
        self.assertTrue(self.ui.detail)
        self.assertIsNone(self.ui.panel)

    def test_manual_app_name_treats_shortcuts_as_text(self):
        self.ui.handle_key('t')
        self.ui.handle_key('\n')
        self.assertEqual(self.ui.wizard_phase, 'name')
        for key in 'qchat界':
            self.assertTrue(self.ui.handle_key(key))
        self.ui.handle_key('\n')
        self.assertEqual(self.ui.wizard_answers['app'], 'qchat界')
        self.assertEqual(self.ui.wizard_phase, 'package')

    def test_cancel_restores_previous_log_window(self):
        self.ui.log_since = '2026-10-04T00:00:00+00:00'
        self.ui.data = {**DATA, 'apps': ['com.example.Chat']}
        self.ui.handle_key('t')
        self.ui.handle_key('\n')
        self.ui.handle_key(27)
        self.assertEqual(self.ui.log_since, '2026-10-04T00:00:00+00:00')
        self.assertIsNone(self.ui.answers)

    def test_rescan_uses_marked_time_and_keeps_answers(self):
        self.ui.log_since = '2026-10-04T00:00:00+00:00'
        with patch.object(self.ui.pool, 'submit') as submit:
            self.ui.start_scan()
        self.assertEqual(submit.call_args.kwargs, {'since': self.ui.log_since})
        self.ui.answers = {'app': 'Chat', 'packaging': 'native', 'picker': 'worked'}
        self.ui.future = Mock()
        self.ui.future.done.return_value = True
        self.ui.future.result.return_value = dict(DATA)
        diagnosed = {**CHECK, 'summary': 'Sharing worked in your test'}
        with patch('linux_doctor.tui.diagnose_sharing', return_value=diagnosed):
            self.ui.collect()
        self.assertEqual(self.ui.data['sharing']['summary'], diagnosed['summary'])
        self.assertEqual(self.ui.data['troubleshooting'], self.ui.answers)
        self.assertIn('changes', self.ui.data)

    def test_environment_and_changes_panels_return_to_overview(self):
        self.ui.data = {**DATA, 'environment': {'context': 'SSH', 'warnings': ['No desktop session']}, 'probes': {'portal': {'outcome': 'missing', 'output': 'systemctl missing'}}}
        self.ui.handle_key('i')
        self.assertEqual(self.ui.panel, 'environment')
        self.ui.draw()
        self.assertIn('portal: missing', self.ui.environment_lines(50))
        self.ui.handle_key(27)
        self.ui.handle_key('c')
        self.assertEqual(self.ui.panel, 'changes')
        self.ui.handle_key(27)
        self.assertIsNone(self.ui.panel)


if __name__ == '__main__':
    unittest.main()
