import contextlib
import curses
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from linux_doctor.__main__ import main


class CliTests(unittest.TestCase):
    def invoke(self, argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = main(argv)
        return result, stdout.getvalue(), stderr.getvalue()

    def test_default_opens_tui(self):
        with patch('sys.stdin.isatty', return_value=True), patch('sys.stdout.isatty', return_value=True):
            with patch('linux_doctor.tui.run_tui') as run_tui:
                self.assertEqual(main([]), 0)
        run_tui.assert_called_once_with()

    def test_noninteractive_default_has_actionable_error(self):
        stderr = io.StringIO()
        with patch('sys.stdin.isatty', return_value=False), contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as raised:
                main([])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn('--report', stderr.getvalue())

    def test_report_prints_one_scan(self):
        data = {'system': 'Example Linux'}
        with patch('linux_doctor.diagnostics.scan', return_value=data) as scan:
            with patch('linux_doctor.report.format_report', return_value='Example report') as format_report:
                result, stdout, stderr = self.invoke(['--report'])
        self.assertEqual((result, stdout, stderr), (0, 'Example report\n', ''))
        scan.assert_called_once_with()
        format_report.assert_called_once_with(data)

    def test_output_refuses_to_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / 'report.txt'
            destination.write_text('Keep this report', encoding='utf-8')
            with patch('linux_doctor.diagnostics.scan', return_value={}):
                with patch('linux_doctor.report.format_report', return_value='New report'):
                    result, _, stderr = self.invoke(['--output', str(destination)])
            self.assertEqual(result, 1)
            self.assertIn('already exists', stderr)
            self.assertEqual(destination.read_text(encoding='utf-8'), 'Keep this report')

    def test_output_saves_report_without_tui(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / 'report.txt'
            with patch('linux_doctor.diagnostics.scan', return_value={}):
                with patch('linux_doctor.report.format_report', return_value='Report: café\n'):
                    result, stdout, stderr = self.invoke(['--output', str(destination)])
            self.assertEqual((result, stderr), (0, ''))
            self.assertIn(str(destination), stdout)
            self.assertEqual(destination.read_text(encoding='utf-8'), 'Report: café\n')
            self.assertEqual(destination.stat().st_mode & 0o777, 0o600)

    def test_removed_browser_options_are_rejected(self):
        for argv in (['--web'], ['--port', '9000'], ['--no-browser']):
            with self.subTest(argv=argv):
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    with self.assertRaises(SystemExit) as raised:
                        main(argv)
                self.assertEqual(raised.exception.code, 2)
                self.assertIn('unrecognized arguments', stderr.getvalue())

    def test_help_only_describes_terminal_and_report_modes(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            with self.assertRaises(SystemExit) as raised:
                main(['--help'])
        self.assertEqual(raised.exception.code, 0)
        for removed_flag in ('--web', '--port', '--no-browser'):
            self.assertNotIn(removed_flag, stdout.getvalue())
        self.assertIn('redacted', stdout.getvalue())

    def test_terminal_failure_and_interrupt_are_clean(self):
        for error, expected_code in ((curses.error('terminal unavailable'), 1), (KeyboardInterrupt(), 130)):
            with self.subTest(error=type(error).__name__):
                stderr = io.StringIO()
                with patch('sys.stdin.isatty', return_value=True), patch('sys.stdout.isatty', return_value=True):
                    with patch('linux_doctor.tui.run_tui', side_effect=error), contextlib.redirect_stderr(stderr):
                        self.assertEqual(main([]), expected_code)
                self.assertNotIn('Traceback', stderr.getvalue())
                self.assertIn('Linux Doctor', stderr.getvalue())


if __name__ == '__main__':
    unittest.main()
