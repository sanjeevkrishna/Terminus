"""Terminus — entry point.

    python run.py                 open the native desktop window
    python run.py browser         open in your default browser
    python run.py dev             browser, no caching, reloader on

    python run.py --log DEBUG     any mode, with verbose logging
    python run.py --port 5050     any mode, on a specific port

File path: run.py
"""

import os
import sys

for _name in ("stdin", "stdout", "stderr"):
    if getattr(sys, _name, None) is None:
        _mode = "r" if _name == "stdin" else "w"
        _sink = open(os.devnull, _mode, encoding="utf-8")
        setattr(sys, _name, _sink)
        setattr(sys, f"__{_name}__", _sink)

import argparse  # noqa: E402  — must follow the stdio guard above

MODES = ("browser", "desktop", "dev")


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="run.py",
        description="Launch Terminus.",
        epilog="Default mode is 'desktop'.",
    )
    parser.add_argument(
        "mode",
        nargs="?",
        default="desktop",
        choices=MODES,
        help="how to run: desktop window, browser, or dev",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="bind port (default 5001; desktop picks a free "
        "one if it is taken)",
    )
    parser.add_argument(
        "--log",
        default=None,
        metavar="LEVEL",
        help="DEBUG, INFO, WARNING, ERROR (overrides TERMINUS_LOG)",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="browser and dev modes: do not open a tab",
    )

    parser.add_argument(
        "--selftest",
        action="store_true",
        help="run a self-test and exit (no server or window)",
    )

    args = parser.parse_args(argv)

    # Imported lazily so a missing optional dependency only matters for the
    # mode that needs it — pywebview is not required to run in a browser.

    if args.selftest:
        # Import-and-wire only; never start the server or a window.
        from terminus.app import create_app

        create_app()
        print("OK selftest")
        raise SystemExit(0)

    if args.mode == "desktop":
        from launchers import desktop as launcher
    elif args.mode == "browser":
        from launchers import browser as launcher
    else:
        from launchers import dev as launcher

    return launcher.main(
        port=args.port, log_level=args.log, open_browser=not args.no_browser
    )


if __name__ == "__main__":
    sys.exit(main())
