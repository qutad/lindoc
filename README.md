# Linux Doctor

A simple terminal application that helps Linux desktop users understand system problems. Read-only checks turn hardware and service evidence into plain-language explanations, practical next steps, and a report you can share.

## Run

Requires Python 3.10 or newer with the standard-library `curses` module, normally included with Linux Python installations. No third-party Python packages or build step are required.

```bash
python3 -m linux_doctor
```

This opens Linux Doctor in your terminal. Select a system check to read its explanation, evidence, and suggested next steps. Use a terminal at least 54 columns wide and 16 rows tall.

| Key | Action |
| --- | --- |
| Up / Down or `j` / `k` | Select a check; scroll its details |
| Enter | Open the selected check |
| Page Up / Page Down / Home / End | Scroll through details |
| Esc or `b` | Return to the overview |
| `t` | Start guided screen-sharing troubleshooting |
| `m` | Mark the time before reproducing a problem |
| `r` | Run a new scan, filtering journal logs from the marked time |
| `i` | Inspect environment and probe availability |
| `c` | Show changes since the previous scan |
| `s` | Save a redacted report in the current directory |
| `q` | Quit |

Run as your regular desktop user so user services and desktop environment variables are available. In a container, SSH session, or sandbox, results describe that environment and may not reflect your desktop.

For a single scan without an interactive terminal:

```bash
python3 -m linux_doctor --report
python3 -m linux_doctor --output doctor-report.txt
```

`--report` writes a redacted plain-text report to standard output, so it also works with pipes. `--output` saves a redacted UTF-8 report and refuses to overwrite an existing file. Saved reports are readable and writable only by your user. These modes do not need `curses`.

## Investigate screen sharing

Press `t` to start the guided check. Choose the affected application, how it was installed, and what happens when you try to share. Linux Doctor combines these answers with the available portal, PipeWire, and application evidence to suggest the next useful check.

To capture evidence around a failure:

1. Press `m` immediately before reproducing the problem.
2. Attempt screen sharing in the affected application.
3. Return to Linux Doctor and press `r` to collect a new scan with journal logs from the marked time.
4. Press `c` to compare the current and previous scans, or open the screen-sharing check to inspect its evidence.
5. Press `s` to save a report, then review it before sharing.

Without a time marker, scans include recent logs. Change comparisons describe differences in the captured evidence; a log change alone does not establish a cause.

## Current capabilities

- Distribution, kernel, desktop, session, and environment detection.
- Graphics device and loaded kernel driver evidence from `lspci -nnk`.
- Portal and PipeWire user-service status.
- WirePlumber audio-device listing, running Flatpak apps, and instantaneous battery power where available.
- Guided screen-sharing investigation using your application, package type, and observed outcome.
- Time-filtered user journal evidence and comparisons between scans.
- Inspectable evidence, probe availability, and redacted plain-text diagnostic reports.

Optional tools: `lspci` (pciutils), `wpctl` (WirePlumber), `systemctl`, `journalctl`, and `flatpak`. Missing tools, inaccessible services, and insufficient evidence are reported as **not verified**, not as hardware failures. Checks run concurrently with a five-second timeout per command.

## Scope and privacy

Linux Doctor is a terminal-only application. Checks do not modify settings, install packages, restart services, or ask for root. Diagnostic results stay in memory unless you export a report.

Exported reports apply automatic redaction to common personal identifiers. Redaction is best effort: unusual identifiers, application content, or sensitive text in logs may remain. Review every report before sharing it. Raw evidence displayed locally may still include personal information.

A passed check has a narrow meaning: a loaded graphics driver does not prove Vulkan works, and a running portal does not prove screen sharing works. The guided flow records what you observed; it cannot verify an application's ScreenCast permission. Per-app XWayland use, fractional scaling, audio playback, idle-power baselines, and discrete-GPU suspension are not tested. A battery power sample is an instantaneous reading, not proof of abnormal idle consumption.

## Development

The terminal interface, report formatter, and diagnostic probes use the Python standard library in `linux_doctor/`.

```bash
python3 -m unittest discover -s tests -v
```
