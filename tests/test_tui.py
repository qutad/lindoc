import curses
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from linux_doctor.report import format_report, safe_text, save_report
from linux_doctor.tui import DoctorTUI, cell_width, check_style, detail_lines

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

    def test_selection_keeps_status_color_and_plain_section_name(self):
        self.ui.colors = {'healthy': 256, 'cyan': 512}
        self.ui.data = {**DATA, 'checks': [{**CHECK, 'id': 'system', 'name': 'Operating system', 'status': 'healthy'}]}
        self.ui.draw()
        calls = [call.args for call in self.screen.addnstr.call_args_list if call.args[0] == 9]
        marker = next(args for args in calls if args[2] == '[OK] ')
        name = next(args for args in calls if args[2] == 'Operating system')
        self.assertTrue(marker[4] & 256)
        self.assertFalse(name[4] & 512)
        self.assertTrue(name[4] & curses.A_BOLD)
        self.assertTrue(marker[4] & curses.A_REVERSE)
        self.assertTrue(name[4] & curses.A_REVERSE)

    def test_critical_warning_is_explicit_in_details_and_export(self):
        check = {**CHECK, 'status': 'warning', 'severity': 'critical'}
        self.ui.colors = {'critical': 256}
        self.ui.data = {**DATA, 'sharing': check}
        self.ui.detail = True
        self.ui.draw()
        self.assertEqual(check_style(check), ('critical', '!!', 'Critical warning'))
        summary = next(call.args for call in self.screen.addnstr.call_args_list if call.args[0] == 8)
        self.assertTrue(summary[4] & 256)
        self.assertIn('[!!] Screen sharing - Critical warning', format_report(self.ui.data))
        self.assertEqual(check_style({**CHECK, 'status': 'warning'})[0], 'warning')
        self.assertEqual(check_style(CHECK)[0], 'unknown')

    def test_color_initialization_falls_back_when_default_background_is_unsupported(self):
        with patch('linux_doctor.tui.curses.has_colors', return_value=True), \
                patch('linux_doctor.tui.curses.start_color'), \
                patch('linux_doctor.tui.curses.use_default_colors', side_effect=curses.error), \
                patch('linux_doctor.tui.curses.init_pair') as init_pair, \
                patch('linux_doctor.tui.curses.color_pair', side_effect=lambda pair: pair << 8):
            self.ui.init_colors()
        self.assertEqual(len(self.ui.colors), 7)
        self.assertTrue(all(call.args[2] == curses.COLOR_BLACK for call in init_pair.call_args_list))

    def test_detail_styles_preserve_wrapping_and_inert_evidence(self):
        self.ui.colors = {'cyan': 256, 'unknown': 512}
        lines = self.ui.styled_detail_lines(CHECK, 20)
        self.assertEqual([line for line, _ in lines], detail_lines(CHECK, 20))
        evidence_index = next(i for i, (line, _) in enumerate(lines) if line == 'EVIDENCE')
        self.assertTrue(lines[evidence_index][1] & 256)
        self.assertTrue(all(attr == 0 for _, attr in lines[evidence_index + 1:]))

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
