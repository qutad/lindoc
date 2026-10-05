"""Plain-text reports shared by the CLI and terminal interface."""
import getpass
import os
import re
import socket
import unicodedata
from pathlib import Path

STATUS = {'healthy': ('OK', 'Check passed'), 'warning': ('!', 'Needs attention'), 'unknown': ('?', 'Not verified')}

# These often appear as useful diagnostic words as well as account/host names.
# Redacting their every occurrence would make a report misleading or unreadable.
GENERAL_IDENTITIES = {
    'admin', 'arch', 'chrome', 'computer', 'debian', 'default', 'desktop',
    'discord', 'fedora', 'firefox', 'flatpak', 'gnome', 'guest', 'host',
    'hostname', 'laptop', 'linux', 'localhost', 'pipewire', 'root', 'steam',
    'system', 'test', 'ubuntu', 'unknown', 'user', 'username', 'wayland',
    'xwayland',
}

SECRET_ASSIGNMENT = re.compile(
    r'''(?ix)
    (?P<prefix>(?<![\w-])["']?(?:[a-z][\w-]*[_-])?
      (?:password|passwd|pwd|token|(?:access|refresh|auth|id)[_-]?token|
         api[_-]?key|client[_-]?secret|secret[_-]access[_-]key|secret|authorization|cookie)
      ["']?\s*[:=]\s*)
    (?P<value>"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s]+)
    '''
)


def safe_text(value):
    """Render terminal control characters inert, including escapes in logs."""
    return ''.join(c if c in '\n\t' or not unicodedata.category(c).startswith('C') else f'\\u{ord(c):04x}' for c in str(value))


def redact_text(value):
    """Best-effort redaction for exports; never modify the live scan object."""
    text = str(value)
    text = re.sub(r'(?i)(\bauthorization["\']?\s*[:=]\s*["\']?)(?:bearer|basic)\s+[^\s"\']+', r'\1<redacted>', text)
    text = re.sub(r'(\b[a-zA-Z][a-zA-Z0-9+.-]*://)[^\s/@:]+:[^\s/]+@', r'\1<redacted>@', text)

    def secret(match):
        secret_value = match.group('value')
        quote = secret_value[0] if secret_value and secret_value[0] in '\"\'' else ''
        return match.group('prefix') + quote + '<redacted>' + quote

    text = SECRET_ASSIGNMENT.sub(secret, text)
    try:
        home = str(Path.home())
    except (RuntimeError, OSError):
        home = ''
    if home and home != '/':
        text = re.sub(re.escape(home) + r'(?=/|\s|$|["\'\),;:])', '<home>', text)
    # Also cover paths from other accounts that appear in journal output.
    text = re.sub(r'(/(?:var/)?home/)[\w.-]+', r'\1<user>', text)
    text = re.sub(r'/Users/[\w.-]+', '/Users/<user>', text)
    text = re.sub(r'/run/user/\d+', '/run/user/<user-id>', text)
    text = re.sub(r'\b[A-Za-z0-9.!#$%&\'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+\b', '<email>', text)
    for getter, placeholder in ((getpass.getuser, '<user>'), (socket.gethostname, '<host>')):
        try:
            identity = getter()
        except (OSError, KeyError):
            continue
        if len(identity) >= 4 and identity.lower() not in GENERAL_IDENTITIES:
            text = re.sub(r'(?<![\w.-])' + re.escape(identity) + r'(?![\w-])', placeholder, text)
    return text


def _all_checks(data):
    checks = list(data.get('checks', []))
    if data.get('sharing'):
        checks.append(data['sharing'])
    return checks


def _display(value):
    if isinstance(value, dict):
        return '; '.join(f'{key}: {_display(item)}' for key, item in sorted(value.items()))
    if isinstance(value, (list, tuple, set)):
        return ', '.join(str(item) for item in value) or 'None'
    return str(value) if value is not None else 'Not recorded'


def format_report(data):
    lines = [
        'LINUX DOCTOR - DIAGNOSTIC REPORT',
        'Read-only scan. Personal-data redaction is best effort; review before sharing.',
        f'Scanned: {data["scannedAt"]}',
        f'System: {data["system"]}',
        f'Kernel: {data["kernel"]}',
        f'Desktop: {data["desktop"]} | Session: {data["session"]}',
    ]
    environment = data.get('environment', {})
    if environment:
        lines.extend(['', 'Scan environment:'])
        lines.extend(f'{key.replace("_", " ").capitalize()}: {_display(value)}' for key, value in environment.items())
    answers = data.get('troubleshooting', {})
    if answers:
        lines.extend(['', 'Troubleshooting observations:'])
        lines.extend(f'{key.capitalize()}: {_display(value)}' for key, value in answers.items())
    if data.get('changes'):
        lines.extend(['', 'Changes since previous scan:'])
        lines.extend(f'- {change}' for change in data['changes'])
    if data.get('probes'):
        lines.extend(['', 'Diagnostic tool availability and outcomes:'])
        for key, probe in sorted(data['probes'].items()):
            availability = 'available' if probe.get('available') else 'unavailable'
            outcome = probe.get('outcome', 'success' if probe.get('ok') else 'not verified')
            lines.append(f'- {key}: {availability}; {outcome}')
    for check in _all_checks(data):
        marker, label = STATUS.get(check['status'], ('?', check['status']))
        if check.get('severity') == 'critical' or check['status'] == 'critical':
            marker, label = '!!', 'Critical warning'
        lines.extend(['', f'[{marker}] {check["name"]} - {label}', check['summary'], check['explanation']])
        if check.get('finding'):
            lines.append(f'Finding: {_display(check["finding"])}')
        if check.get('confidence'):
            lines.append(f'Confidence: {_display(check["confidence"])}')
        if check['steps']:
            lines.extend(['', 'Next steps:'])
            lines.extend(f'{i}. {step}' for i, step in enumerate(check['steps'], 1))
        lines.extend(['', 'Evidence:', check['evidence'] or 'No evidence collected.'])
    return safe_text(redact_text('\n'.join(lines))) + '\n'


def format_changes(previous, current):
    """Describe semantic scan changes, excluding volatile logs and measurements."""
    if previous is None:
        return ['No previous scan. Run another scan to compare.']
    changes = []
    for key in ('system', 'kernel', 'desktop', 'session'):
        if previous.get(key) != current.get(key):
            changes.append(f'{key.capitalize()}: {_display(previous.get(key))} -> {_display(current.get(key))}')
    old_environment = previous.get('environment', {})
    new_environment = current.get('environment', {})
    for key in sorted(set(old_environment) | set(new_environment)):
        if old_environment.get(key) != new_environment.get(key):
            changes.append(f'Environment {key}: {_display(old_environment.get(key))} -> {_display(new_environment.get(key))}')
    old_checks = {check['id']: check for check in _all_checks(previous)}
    new_checks = {check['id']: check for check in _all_checks(current)}
    for key in sorted(set(old_checks) | set(new_checks)):
        old = old_checks.get(key)
        new = new_checks.get(key)
        if old is None:
            changes.append(f'{new["name"]}: check added ({new["status"]}; {new["summary"]})')
        elif new is None:
            changes.append(f'{old["name"]}: check no longer available')
        elif any(_semantic_value(old, field) != _semantic_value(new, field) for field in ('status', 'summary', 'finding')):
            before = f'{old["status"]}; {old["summary"]}'
            after = f'{new["status"]}; {new["summary"]}'
            if old.get('finding') != new.get('finding'):
                before += f'; {_display(old.get("finding"))}'
                after += f'; {_display(new.get("finding"))}'
            changes.append(f'{new["name"]}: {before} -> {after}')
    for key in ('apps', 'applications'):
        if key in previous or key in current:
            before = previous.get(key)
            after = current.get(key)
            # Running application order is not significant.
            if isinstance(before, list) and isinstance(after, list):
                equal = sorted(map(str, before)) == sorted(map(str, after))
            else:
                equal = before == after
            if not equal:
                changes.append(f'Applications: {_display(before)} -> {_display(after)}')
    old_probes = previous.get('probes', {})
    new_probes = current.get('probes', {})
    for key in sorted(set(old_probes) | set(new_probes)):
        before = old_probes.get(key, {})
        after = new_probes.get(key, {})
        if any(before.get(field) != after.get(field) for field in ('available', 'ok', 'outcome')):
            changes.append(f'Probe {key}: {_probe_summary(before)} -> {_probe_summary(after)}')
    return changes or ['No diagnostic changes since the previous scan.']


def _semantic_value(check, field):
    value = check.get(field)
    if check.get('id') == 'power' and isinstance(value, str):
        return re.sub(r'\d+(?:\.\d+)?\s*(?:mW|W|watts?)\b', '<power reading>', value)
    return value


def _probe_summary(probe):
    if not probe:
        return 'not recorded'
    availability = 'available' if probe.get('available') else 'unavailable'
    outcome = probe.get('outcome', 'success' if probe.get('ok') else 'not verified')
    return f'{availability}; {outcome}'


def save_report(data, path):
    """Create a private report without overwriting an existing file."""
    path = Path(path)
    report = format_report(data)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as out:
        out.write(report)
    return path
