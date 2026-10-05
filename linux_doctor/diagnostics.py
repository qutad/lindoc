"""Read-only probes. Never invoke a shell or attempt to change the host."""
import glob
import os
import platform
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path


def read(path):
    try:
        return Path(path).read_text(errors='replace').strip()
    except OSError:
        return ''


def run(argv):
    if not shutil.which(argv[0]):
        return {'available': False, 'ok': False, 'output': f'{argv[0]} is not installed.', 'outcome': 'missing'}
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=5, errors='replace', env={**os.environ, 'LC_ALL': 'C'})
        return {'available': True, 'ok': p.returncode == 0, 'output': (p.stdout + p.stderr).strip()[:14000], 'outcome': 'ok' if p.returncode == 0 else 'error'}
    except subprocess.TimeoutExpired:
        return {'available': True, 'ok': False, 'output': f'{argv[0]} timed out after 5 seconds.', 'outcome': 'timeout'}
    except OSError as error:
        return {'available': True, 'ok': False, 'output': str(error), 'outcome': 'error'}


def item(id, name, status, summary, explanation, evidence='', steps=None):
    return dict(id=id, name=name, status=status, summary=summary, explanation=explanation, evidence=evidence, steps=steps or [])


def environment_context():
    """Describe the scope of observations without requiring a desktop session."""
    contexts, warnings = [], []
    if any(os.environ.get(key) for key in ('SSH_CONNECTION', 'SSH_CLIENT', 'SSH_TTY')):
        contexts.append('SSH session')
        warnings.append('This scan runs through SSH; its session and user-service access may differ from the local desktop.')
    if os.environ.get('container') or Path('/.dockerenv').exists() or Path('/run/.containerenv').exists():
        contexts.append('container')
        warnings.append('This scan runs inside a container; devices and services may describe the container rather than the host desktop.')
    graphical = os.environ.get('XDG_SESSION_TYPE', '').lower() in ('wayland', 'x11') or os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')
    if not graphical:
        contexts.append('console or headless session')
        warnings.append('No graphical session was detected. Desktop-service checks may be unavailable from this session.')
    return {'context': ', '.join(contexts) if contexts else 'local graphical session', 'warnings': warnings}


def service_state(probe):
    """Only exact is-active replies count as evidence of a service state."""
    if not probe.get('available') or probe.get('outcome') in ('missing', 'timeout'):
        return 'unknown'
    state = probe.get('output', '').strip()
    if state == 'active' and probe.get('ok'):
        return 'active'
    if state in ('inactive', 'failed') and not probe.get('ok'):
        return state
    return 'unknown'


def portal_backends(probe):
    """Read loaded backend unit states, without activating any service."""
    if not probe.get('ok'):
        return []
    backends = []
    for line in probe.get('output', '').splitlines():
        fields = line.split()
        if len(fields) >= 4 and re.fullmatch(r'xdg-desktop-portal-[\w@.\-]+\.service', fields[0]):
            backends.append((fields[0], fields[2]))
    return backends


def diagnose_sharing(data, answers):
    """Combine observations and a user's reproduction; never infer permissions."""
    probes = data.get('probes', {})
    app = str(answers.get('app') or 'Not specified')
    packaging = answers.get('packaging', 'unknown')
    if packaging not in ('flatpak', 'native', 'browser', 'unknown'):
        packaging = 'unknown'
    picker = answers.get('picker', 'untried')
    if picker not in ('absent', 'failed', 'worked', 'untried'):
        picker = 'untried'
    failed = []
    for key, label in (('portal', 'Desktop portal'), ('pipewire', 'PipeWire')):
        if service_state(probes.get(key, {})) == 'failed':
            failed.append(label)
    failed.extend(name for name, state in portal_backends(probes.get('backends', {})) if state == 'failed')

    status, confidence = 'unknown', 'low'
    summary = 'An app-level test is needed'
    finding = 'The available service evidence does not establish whether this application can share a screen.'
    explanation = 'A running portal or PipeWire service alone cannot prove screen sharing works. Inactive services may start on demand. Linux Doctor does not test or change ScreenCast permission.'
    steps = ['Start a screen share in the affected application.', 'Record whether a screen/window picker appears and whether sharing starts.', 'Rescan after reproducing the issue, then inspect the related logs.']
    if failed:
        status, confidence = 'warning', 'high'
        summary = 'A screen-sharing service reports a failed state'
        finding = 'Observed failed service state: ' + ', '.join(failed) + '.'
        explanation = 'A failed service is a concrete issue to investigate. It does not establish which application operation failed or whether a permission was denied.'
        if picker == 'worked':
            explanation += ' You reported that sharing worked; that test and the failed service snapshot may describe different moments or paths.'
        steps = ['Read the failed service and backend log evidence below.', 'Repeat the share in the affected application and compare its time with the logs.', 'Use your distribution’s troubleshooting guidance for the named failed service before changing settings.']
    elif picker == 'worked':
        status, confidence = 'healthy', 'medium'
        summary = 'Screen sharing worked in your test'
        finding = 'You reported successful screen sharing in this application.'
        explanation = 'This result is based on your observation. Linux Doctor did not independently verify the shared image, portal permissions, or other applications.'
        steps = ['If the issue returns, reproduce it and run a new scan to capture fresh evidence.']
    elif picker == 'absent':
        status, confidence = 'warning', 'medium'
        summary = 'No screen picker appeared in your test'
        finding = 'You reported that the screen-sharing flow did not reach a screen/window picker.'
        explanation = 'This identifies where your attempt stopped, but does not establish the cause. Application support, its selected capture method, desktop integration, and portal handling still need investigation; a permission denial is not proven.'
        steps = ['Check that you requested screen/window sharing in the selected app, rather than camera sharing.', 'Inspect portal and backend logs from the reproduction time.', 'Compare with another screen-sharing application in the same desktop session.']
    elif picker == 'failed':
        status, confidence = 'warning', 'medium'
        summary = 'Sharing failed after the picker in your test'
        finding = 'You reported reaching the picker, then failing to start a usable share.'
        explanation = 'Reaching the picker narrows the reported failure to a later part of the flow. It does not prove that a capture stream started or that permissions were denied. App, portal, and PipeWire evidence must be compared.'
        steps = ['Inspect the portal, backend, and PipeWire logs from this attempt.', 'Note any message shown by the application after choosing a screen or window.', 'Compare the same screen/window selection in another application.']

    if packaging == 'flatpak' and app != 'Not specified' and probes.get('apps', {}).get('ok') and app not in data.get('apps', []):
        steps.append('The selected Flatpak app was not in the running-app snapshot. Open it and rescan; this does not mean it is uninstalled.')
    warnings = data.get('environment', {}).get('warnings', [])
    if warnings:
        steps.append('For desktop troubleshooting, also scan from a terminal inside the affected user’s graphical session.')
    steps.append('Review logs for personal information before sharing the report.')
    evidence = [f'Application: {app}\nPackaging: {packaging}\nUser-reported outcome: {picker}',
                f'Session: {data.get("session", "Unknown")}\nDesktop: {data.get("desktop", "Unknown")}',
                'Log window: ' + (str(data.get('logSince')) + ' onward (current boot, latest 60 entries)' if data.get('logSince') else 'current boot, latest 60 entries')]
    for key, label in (('portal', 'Portal service'), ('backends', 'Loaded portal backends'), ('pipewire', 'PipeWire service'), ('logs', 'Portal, backend, PipeWire and WirePlumber logs')):
        probe = probes.get(key, {})
        evidence.append(f'{label}:\n{probe.get("output") or "No evidence collected."}')
    if warnings:
        evidence.append('Environment limitations:\n' + '\n'.join(warnings))
    result = item('sharing', 'Screen sharing', status, summary, explanation, '\n\n'.join(evidence), steps)
    result.update(finding=finding, confidence=confidence)
    return result


def log_timestamp(since):
    if since is None:
        return None
    # The wizard uses ISO timestamps. Explicit UTC also avoids ambiguity when
    # journalctl runs under a different locale or timezone.
    timestamp = since if isinstance(since, datetime) else datetime.fromisoformat(str(since).replace('Z', '+00:00'))
    return timestamp.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')


def scan(since=None):
    since = log_timestamp(since)
    log_command = ['journalctl', '--user', '-b', '-u', 'xdg-desktop-portal.service', '-u', 'xdg-desktop-portal-*.service', '-u', 'pipewire.service', '-u', 'wireplumber.service', '-n', '60', '--no-pager', '-o', 'short-iso']
    if since:
        log_command.extend(['--since', since])
    commands = {
        'gpu': ['lspci', '-nnk'],
        'audio': ['wpctl', 'status'],
        'portal': ['systemctl', '--user', 'is-active', 'xdg-desktop-portal.service'],
        'pipewire': ['systemctl', '--user', 'is-active', 'pipewire.service'],
        'backends': ['systemctl', '--user', 'list-units', '--all', '--type=service', '--no-legend', '--plain', '--no-pager', 'xdg-desktop-portal-*.service'],
        'logs': log_command,
        'apps': ['flatpak', 'ps', '--columns=application'],
    }
    with ThreadPoolExecutor(max_workers=len(commands)) as pool:
        results = dict(zip(commands, pool.map(run, commands.values())))
    # Older callers may supply minimal probe records; expose one stable schema.
    results = {key: {**probe, 'outcome': probe.get('outcome', 'missing' if not probe['available'] else 'ok' if probe['ok'] else 'error')} for key, probe in results.items()}
    release = dict(re.findall(r'^([A-Z_]+)=["\']?(.*?)["\']?$', read('/etc/os-release'), re.M))
    session = os.environ.get('XDG_SESSION_TYPE', 'Unknown')
    desktop = os.environ.get('XDG_CURRENT_DESKTOP', 'Unknown')
    gpu_lines = []
    in_gpu = False
    for line in results['gpu']['output'].splitlines():
        if line and not line[0].isspace():
            in_gpu = bool(re.search(r'VGA|3D controller|Display controller', line))
        if in_gpu:
            gpu_lines.append(line)
    gpu = '\n'.join(gpu_lines)
    driver = 'Kernel driver in use:' in gpu
    portal = results['portal']
    audio = results['audio']
    portal_state = service_state(portal)
    failed_backends = [name for name, state in portal_backends(results['backends']) if state == 'failed']
    portal_status = 'warning' if portal_state == 'failed' or failed_backends else 'healthy' if portal_state == 'active' else 'unknown'
    portal_summary = 'Portal service or backend reports a failure' if portal_status == 'warning' else 'Portal service is running' if portal_status == 'healthy' else 'Portal state not confirmed'
    checks = [
        item('system', 'Operating system', 'healthy' if platform.system() == 'Linux' else 'unknown', release.get('PRETTY_NAME', platform.system()), 'Distribution and kernel identify the environment in which your applications run.', f'Kernel: {platform.release()}\nDesktop: {desktop}\nSession: {session}\nArchitecture: {platform.machine()}'),
        item('graphics', 'Graphics & display', 'healthy' if driver else 'unknown', 'Graphics driver detected' if driver else 'Graphics needs a closer look', 'A loaded kernel driver is a useful starting point. This check does not test Vulkan, OpenGL, acceleration, or rendering inside individual applications.', gpu or results['gpu']['output'], ['If an app renders incorrectly, compare its behavior with other applications.', 'Check your distribution’s graphics troubleshooting guide before changing drivers.']),
        item('portals', 'Desktop portals', portal_status, portal_summary, 'Portals let sandboxed applications request screen sharing, file selection, and other desktop features. A running service does not confirm that every portal interface works. Inactive portals can start on demand. A failed service or backend is an observed issue, but does not identify an app permission problem. The backend list shows loaded units, not every installed backend.', f'Portal service:\n{portal["output"]}\n\nLoaded backends:\n{results["backends"]["output"]}', ['Open screen sharing in the affected application and check whether a selection dialog appears.', 'Use the screen sharing diagnostic to inspect the available service evidence.']),
        item('audio', 'Audio & devices', 'healthy' if audio['ok'] else 'unknown', 'WirePlumber is reachable' if audio['ok'] else 'Audio state not confirmed', 'The device list comes from WirePlumber. A successful connection does not confirm that your speakers or microphone produce sound.', audio['output'], ['Inspect the listed sinks (speakers) and sources (microphones).', 'Use desktop sound settings to test the selected output and microphone.']),
        item('integration', 'App integration', 'unknown', 'Per-app rendering needs verification', 'Wayland session detection alone cannot tell whether an individual application uses XWayland, or whether scaling makes it blurry.', f'Session: {session}\nDesktop: {desktop}\nRunning Flatpak applications:\n{results["apps"]["output"]}', ['Check the affected app’s display settings and supported Wayland options.', 'Compare rendering at 100% scale using your desktop settings.']),
    ]
    power = []
    for supply in glob.glob('/sys/class/power_supply/*'):
        if read(f'{supply}/type') == 'Battery':
            rate = read(f'{supply}/power_now')
            state = read(f'{supply}/status')
            power.append(f'{Path(supply).name}: {state}; ' + (f'{int(rate)/1e6:.1f} W instantaneous battery power' if rate.isdigit() else 'power rate unavailable'))
    checks.append(item('power', 'Power & battery', 'unknown', 'Battery telemetry available' if power else 'No battery telemetry', 'One power reading is not an idle-power diagnosis. Normal consumption depends on hardware, brightness, workload, and charging state. We do not apply a universal wattage threshold.', '\n'.join(power) or 'No readable battery power measurements were found.', ['Measure while unplugged after the machine has settled at idle.', 'Compare readings with the same screen brightness and workload.']))
    apps = sorted(set(line.strip() for line in results['apps']['output'].splitlines() if re.fullmatch(r'[A-Za-z_][\w-]*(?:\.[A-Za-z_][\w-]*){2,}', line.strip()))) if results['apps']['ok'] else []
    data = {'mode': 'live', 'scannedAt': datetime.now(timezone.utc).isoformat(), 'system': release.get('PRETTY_NAME', platform.system()), 'kernel': platform.release(), 'desktop': desktop, 'session': session, 'checks': checks, 'probes': results, 'apps': apps, 'environment': environment_context(), 'logSince': since}
    data['sharing'] = diagnose_sharing(data, {})
    return data
