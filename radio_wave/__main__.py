"""Точка входа: python -m radio_wave."""

from __future__ import annotations

import sys


def main(argv=None) -> int:
    from .diagnostics import configure_playback_log

    args = list(sys.argv[1:] if argv is None else argv)
    options_end = args.index("--") if "--" in args else len(args)
    options = args[:options_end]
    developer = "--dev" in options
    configure_playback_log(developer=developer)

    from .ui.app import main as ui_main

    return ui_main([option for option in options if option != "--dev"] + args[options_end:])


if __name__ == "__main__":
    sys.exit(main())
