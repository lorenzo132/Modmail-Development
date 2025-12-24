"""Bootstraps the runtime environment before the bot starts.

Keep this module lightweight: it is imported by entrypoints.
"""

from __future__ import annotations

from modmail.runtime import set_windows_event_loop_policy, setup_colorama

from .checks import check_cairosvg_importable, check_discord_py_version, setup_uvloop


def bootstrap(*, logger) -> None:
    """Prepare the runtime environment and validate critical dependencies."""

    setup_colorama()
    set_windows_event_loop_policy()
    setup_uvloop(logger=logger)
    check_cairosvg_importable(logger=logger)
    check_discord_py_version(logger=logger)
