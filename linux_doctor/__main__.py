"""Linux Doctor command-line entry point."""
import argparse
from pathlib import Path
import sys


def make_parser():
    parser = argparse.ArgumentParser(
        description='Linux Doctor — read-only Linux desktop diagnostics',
        epilog='With no options, opens the terminal interface. Run as your regular desktop user.',
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--report', action='store_true', help='scan once and print a redacted plain-text report')
    mode.add_argument('--output', metavar='PATH', type=Path, help='scan once and save a new redacted report (never overwrites)')
    return parser


def main(argv=None):
    parser = make_parser()
    args = parser.parse_args(argv)
    if not (args.report or args.output) and not (sys.stdin.isatty() and sys.stdout.isatty()):
        parser.error('the terminal interface needs an interactive terminal; use --report to print a diagnostic report')

    try:
        if args.report or args.output:
            from .diagnostics import scan
            from .report import format_report, save_report
            data = scan()
            if args.output:
                save_report(data, args.output)
                print(f'Report saved to {args.output}')
            else:
                report = format_report(data)
                print(report, end='' if report.endswith('\n') else '\n')
        else:
            from .tui import run_tui
            run_tui()
    except KeyboardInterrupt:
        print('\nLinux Doctor stopped.', file=sys.stderr)
        return 130
    except FileExistsError:
        print(f'Linux Doctor: {args.output} already exists; choose a new report filename.', file=sys.stderr)
        return 1
    except Exception as error:
        # Terminal initialization and unavailable local resources should not leave
        # users with a traceback. curses.wrapper restores the terminal first.
        print(f'Linux Doctor: {error}', file=sys.stderr)
        if not (args.report or args.output):
            print('Try --report to print a diagnostic report without the terminal interface.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
