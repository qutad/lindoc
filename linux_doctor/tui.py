"""Keyboard-driven Linux diagnostics with no third-party or GUI dependencies."""
import curses
import textwrap
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from .diagnostics import diagnose_sharing, scan
from .report import STATUS, format_changes, safe_text, save_report


def cell_width(char):
    if unicodedata.combining(char):
        return 0
    return 2 if unicodedata.east_asian_width(char) in ('W', 'F') else 1


def cell_chunks(text, width):
    """Split a line without letting wide characters wrap into the next row."""
    chunk, cells = '', 0
    for char in text:
        size = cell_width(char)
        if chunk and cells + size > width:
            yield chunk
            chunk, cells = '', 0
        chunk += char
        cells += size
    yield chunk


def wrap_lines(paragraphs, width):
    lines = []
    for paragraph in paragraphs:
        for line in safe_text(paragraph).expandtabs(4).split('\n'):
            for wrapped in textwrap.wrap(line, max(1, width), replace_whitespace=False) or ['']:
                lines.extend(cell_chunks(wrapped, max(1, width)))
    return lines


def detail_lines(check, width):
    paragraphs = [check['summary'], '']
    if check.get('finding'):
        paragraphs.extend(['FINDING', check['finding'], f'Confidence: {check.get("confidence", "not established")}', ''])
    paragraphs.append(check['explanation'])
    if check['steps']:
        paragraphs.extend(['', 'NEXT STEPS'])
        paragraphs.extend(f'{i}. {step}' for i, step in enumerate(check['steps'], 1))
    paragraphs.extend(['', 'EVIDENCE', check['evidence'] or 'No evidence collected.'])
    return wrap_lines(paragraphs, width)


class DoctorTUI:
    def __init__(self, screen):
        self.screen = screen
        self.data = None
        self.previous = None
        self.selected = 0
        self.detail = False
        self.panel = None
        self.offset = 0
        self.message = ''
        self.future = None
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.colors = {}
        self.log_since = None
        self.answers = None
        self.wizard_phase = 'app'
        self.wizard_index = 0
        self.wizard_answers = {}
        self.app_name = ''
        self.wizard_previous_since = None

    def write(self, y, x, value, attr=0):
        height, width = self.screen.getmaxyx()
        if y < 0 or y >= height or x >= width - 1:
            return
        value = safe_text(value).replace('\n', ' ').expandtabs(4)
        value = next(cell_chunks(value, width - x - 1))
        if sum(cell_width(char) for char in value) > width - x - 1:
            return
        try:
            self.screen.addnstr(y, x, value, max(0, width - x - 1), attr)
        except curses.error:
            pass  # A resize can invalidate the dimensions between draw calls.

    def start_scan(self):
        if self.future is not None:
            self.message = 'A scan is already running.'
            return
        self.message = ''
        self.future = self.pool.submit(scan, since=self.log_since)

    def collect(self):
        if self.future is not None and self.future.done():
            try:
                collected = self.future.result()
                if self.answers:
                    collected['troubleshooting'] = dict(self.answers)
                    collected['sharing'] = diagnose_sharing(collected, self.answers)
                collected['changes'] = format_changes(self.data, collected)
                self.previous, self.data = self.data, collected
                self.message = 'Scan complete. No system settings changed.'
                self.offset = 0
            except Exception as error:
                self.message = f'Scan failed: {error}. Press r to retry.'
            self.future = None

    def checks(self):
        return [*self.data['checks'], self.data['sharing']] if self.data else []

    def mark_reproduction(self):
        self.log_since = datetime.now(timezone.utc).isoformat(timespec='seconds')
        self.message = 'Time marked. Reproduce the problem, then press r.'

    def draw(self):
        self.screen.erase()
        height, width = self.screen.getmaxyx()
        self.write(0, 2, 'LINUX DOCTOR', curses.A_BOLD)
        if height < 16 or width < 54:
            self.write(2, 1, 'Resize terminal to at least 54 x 16.')
            self.write(4, 1, 'q: quit')
            self.screen.refresh()
            return
        if self.future is not None:
            spinner = '|/-\\'[int(time.monotonic() * 8) % 4]
            self.write(1, 2, f'{spinner} Scanning read-only checks...')
        else:
            self.write(1, 2, 'Local diagnostics / read-only / no root needed', curses.A_DIM)
        if self.log_since:
            self.write(2, 2, f'Log window starts: {self.log_since}', curses.A_DIM)
        if self.data:
            self.write(3, 2, f'{self.data["system"]} | {self.data["desktop"]} | {self.data["session"]}')
            context = self.data.get('environment', {}).get('context', '')
            self.write(4, 2, context or f'Kernel {self.data["kernel"]}')
            if self.panel == 'wizard':
                self.draw_wizard(height, width)
            elif self.panel == 'environment':
                self.write(6, 2, 'ENVIRONMENT AND PROBE AVAILABILITY', curses.A_BOLD)
                self.draw_scroll(self.environment_lines(width - 5), height)
            elif self.panel == 'changes':
                self.write(6, 2, 'CHANGES SINCE PREVIOUS SCAN', curses.A_BOLD)
                self.draw_scroll(wrap_lines(self.data.get('changes', ['Run another scan to compare.']), width - 5), height)
            elif self.detail:
                check = self.checks()[self.selected]
                marker, label = STATUS[check['status']]
                self.write(6, 2, f'[{marker}] {check["name"]} - {label}', curses.A_BOLD)
                self.draw_scroll(detail_lines(check, width - 5), height)
            else:
                self.draw_overview(height, width)
        else:
            self.write(5, 2, 'Reading devices, services, and logs...' if self.future else 'No results. Press r to retry.')
        self.write(height - 3, 2, self.message, curses.A_DIM)
        if self.panel == 'wizard':
            help_text = 'Type app name  Enter: next  Esc: cancel' if self.wizard_phase == 'name' else 'j/k: select  Enter: next  Esc: cancel  q: quit'
        elif self.detail or self.panel in ('changes', 'environment'):
            help_text = 'j/k: scroll  PgUp/Dn  Esc: back  s: save  q: quit'
        else:
            help_text = 'j/k: select  Enter: open  r: scan  s: save  q: quit'
        self.write(height - 2, 2, help_text)
        self.screen.refresh()

    def draw_overview(self, height, width):
        checks = self.checks()
        counts = {status: sum(c['status'] == status for c in checks) for status in STATUS}
        self.write(6, 2, f'{counts["healthy"]} passed / {counts["warning"]} attention / {counts["unknown"]} unverified', curses.A_BOLD)
        self.write(7, 2, 't: diagnose sharing  m: mark  c: changes  i: tools')
        row_count = max(1, height - 13)
        first = max(0, self.selected - row_count + 1)
        for row, index in enumerate(range(first, min(len(checks), first + row_count)), 9):
            check = checks[index]
            marker, _ = STATUS[check['status']]
            prefix = '>' if index == self.selected else ' '
            text = f'{prefix} [{marker:2}] {check["name"]}'
            if width >= 85:
                text = f'{text:<28} {check["summary"]}'
            attr = curses.A_REVERSE if index == self.selected else self.colors.get(check['status'], 0)
            self.write(row, 2, text.ljust(width - 4), attr)
        self.write(height - 4, 2, checks[self.selected]['summary'])

    def draw_scroll(self, lines, height):
        page = max(1, height - 12)
        self.offset = min(self.offset, max(0, len(lines) - page))
        for y, line in enumerate(lines[self.offset:self.offset + page], 8):
            self.write(y, 2, line)
        self.write(height - 4, 2, f'Lines {self.offset + 1}-{min(len(lines), self.offset + page)} of {len(lines)}', curses.A_DIM)

    def environment_lines(self, width):
        environment = self.data.get('environment', {})
        paragraphs = [environment.get('context', 'Context unavailable'), *environment.get('warnings', []), '', 'PROBES']
        for name, probe in self.data.get('probes', {}).items():
            outcome = probe.get('outcome', 'ok' if probe.get('ok') else 'unavailable')
            paragraphs.extend([f'{name}: {outcome}', probe.get('output', '') if outcome != 'ok' else '', ''])
        if not self.data.get('probes'):
            paragraphs.append('No probe metadata available.')
        return wrap_lines(paragraphs, width)

    def wizard_options(self):
        if self.wizard_phase == 'app':
            return [*(self.data.get('apps', [])), 'Other application (enter name)']
        if self.wizard_phase == 'package':
            return ['Flatpak', 'Native package / AppImage', 'Browser / web app', 'Not sure']
        if self.wizard_phase == 'outcome':
            return ['No screen/window picker appeared', 'Picker appeared, but sharing failed', 'Screen sharing worked', 'I have not tried it yet']
        return []

    def draw_wizard(self, height, width):
        self.write(6, 2, 'TROUBLESHOOT SCREEN SHARING', curses.A_BOLD)
        titles = {'app': 'Which application is affected?', 'name': 'Enter the application name:', 'package': 'How do you run this application?', 'outcome': 'Try sharing now. What happened?'}
        self.write(7, 2, titles[self.wizard_phase])
        if self.wizard_phase == 'name':
            name_cells = sum(cell_width(c) for c in self.app_name)
            name_visible = self.app_name
            while name_cells > width - 8 and name_visible:
                name_cells -= cell_width(name_visible[0])
                name_visible = name_visible[1:]
            self.write(9, 2, '> ' + name_visible + '_', curses.A_REVERSE)
        else:
            options = self.wizard_options()
            rows = max(1, height - 13)
            first = max(0, self.wizard_index - rows + 1)
            for row, index in enumerate(range(first, min(len(options), first + rows)), 9):
                label = ('> ' if index == self.wizard_index else '  ') + options[index]
                self.write(row, 2, label, curses.A_REVERSE if index == self.wizard_index else 0)
        hints = {'app': 'Detected running Flatpaks are listed when available.', 'name': 'Use an app name or Flatpak ID, not a command.', 'package': self.wizard_answers.get('app', ''), 'outcome': 'Enter collects logs from the marked time.'}
        self.write(height - 4, 2, hints[self.wizard_phase], curses.A_DIM)

    def begin_wizard(self):
        if self.future is not None:
            self.message = 'Wait for the current scan before troubleshooting.'
            return
        self.wizard_previous_since = self.log_since
        self.panel = 'wizard'
        self.wizard_phase = 'app'
        self.wizard_index = 0
        self.wizard_answers = {}
        self.app_name = ''
        self.message = ''

    def begin_reproduction(self):
        self.wizard_phase = 'outcome'
        self.wizard_index = 0
        self.mark_reproduction()
        self.message = 'Switch to the app, try sharing, then select an outcome.'

    def handle_wizard(self, key, text_key):
        if key == 27:
            self.panel = None
            self.log_since = self.wizard_previous_since
            self.message = 'Troubleshooting cancelled.'
            return True
        if self.wizard_phase == 'name':
            if key in (10, 13, curses.KEY_ENTER) and self.app_name.strip():
                self.wizard_answers['app'] = self.app_name.strip()
                self.wizard_phase = 'package'
                self.wizard_index = 0
            elif key in (curses.KEY_BACKSPACE, 127, 8):
                self.app_name = self.app_name[:-1]
            elif text_key and text_key.isprintable() and len(self.app_name) < 120:
                self.app_name += text_key
            return True
        if key in (ord('q'), ord('Q')):
            return False
        options = self.wizard_options()
        if key in (curses.KEY_DOWN, ord('j'), curses.KEY_UP, ord('k')):
            delta = 1 if key in (curses.KEY_DOWN, ord('j')) else -1
            self.wizard_index = (self.wizard_index + delta) % len(options)
        elif key in (10, 13, curses.KEY_ENTER):
            if self.wizard_phase == 'app':
                apps = self.data.get('apps', [])
                if self.wizard_index < len(apps):
                    self.wizard_answers = {'app': apps[self.wizard_index], 'packaging': 'flatpak'}
                    self.begin_reproduction()
                else:
                    self.wizard_phase = 'name'
            elif self.wizard_phase == 'package':
                self.wizard_answers['packaging'] = ('flatpak', 'native', 'browser', 'unknown')[self.wizard_index]
                self.begin_reproduction()
            elif self.wizard_phase == 'outcome':
                self.wizard_answers['picker'] = ('absent', 'failed', 'worked', 'untried')[self.wizard_index]
                self.answers = dict(self.wizard_answers)
                self.panel = None
                self.selected = len(self.checks()) - 1
                self.detail = True
                self.offset = 0
                self.start_scan()
        return True

    def handle_key(self, key):
        text_key = key if isinstance(key, str) else chr(key) if 32 <= key < 127 else ''
        key = ord(key) if isinstance(key, str) else key
        if self.panel == 'wizard':
            return self.handle_wizard(key, text_key)
        if key in (ord('q'), ord('Q')):
            return False
        if key in (ord('r'), ord('R')):
            self.start_scan()
        if not self.data:
            return True
        if key == ord('t'):
            self.begin_wizard()
        elif key == ord('m'):
            if self.future is not None:
                self.message = 'Wait for the current scan before marking time.'
            else:
                self.mark_reproduction()
        elif key == ord('i'):
            self.panel = 'environment'
            self.offset = 0
        elif key == ord('c'):
            self.panel = 'changes'
            self.offset = 0
        elif key in (ord('s'), ord('S')):
            filename = f'linux-doctor-{datetime.now():%Y%m%d-%H%M%S-%f}.txt'
            try:
                save_report(self.data, Path.cwd() / filename)
                self.message = f'Saved ./{filename} (redacted; review before sharing)'
            except OSError as error:
                self.message = f'Cannot save report: {error}'
        elif key in (27, ord('b'), curses.KEY_LEFT):
            self.panel = None
            self.detail = False
            self.offset = 0
        elif key in (10, 13, curses.KEY_ENTER, curses.KEY_RIGHT) and self.panel is None:
            self.detail = True
        elif key in (curses.KEY_DOWN, ord('j'), curses.KEY_UP, ord('k')):
            delta = 1 if key in (curses.KEY_DOWN, ord('j')) else -1
            if self.detail or self.panel in ('changes', 'environment'):
                self.offset = max(0, self.offset + delta)
            else:
                self.selected = (self.selected + delta) % len(self.checks())
        elif self.detail or self.panel in ('changes', 'environment'):
            page = max(1, self.screen.getmaxyx()[0] - 12)
            if key in (curses.KEY_NPAGE, ord(' ')):
                self.offset += page
            elif key == curses.KEY_PPAGE:
                self.offset = max(0, self.offset - page)
            elif key == curses.KEY_HOME:
                self.offset = 0
            elif key == curses.KEY_END:
                self.offset = 10 ** 9  # Clamped to the final page on the next draw.
        return True

    def run(self):
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        if curses.has_colors():
            curses.start_color()
            try:
                curses.use_default_colors()
                for pair, (status, color) in enumerate((('healthy', curses.COLOR_GREEN), ('warning', curses.COLOR_YELLOW), ('unknown', curses.COLOR_CYAN)), 1):
                    curses.init_pair(pair, color, -1)
                    self.colors[status] = curses.color_pair(pair)
            except curses.error:
                pass
        self.screen.keypad(True)
        self.screen.timeout(100)
        self.start_scan()
        try:
            while True:
                self.collect()
                self.draw()
                try:
                    key = self.screen.get_wch()
                except curses.error:
                    key = -1
                if not self.handle_key(key):
                    break
        finally:
            if self.future is not None and not self.future.done():
                self.message = 'Finishing active scan before exit...'
                self.draw()
            self.pool.shutdown(wait=True, cancel_futures=True)


def run_tui():
    curses.wrapper(lambda screen: DoctorTUI(screen).run())
