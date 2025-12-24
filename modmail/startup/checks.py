from __future__ import annotations

import os
import platform
import struct

import discord


def setup_uvloop(*, logger) -> None:
    """Install uvloop when available (non-Windows only).

    uvloop is an optional dependency.
    """

    try:
        # noinspection PyUnresolvedReferences
        import uvloop  # type: ignore

        logger.debug("Setting up with uvloop.")
        uvloop.install()
    except ImportError:
        return


def check_cairosvg_importable(*, logger) -> None:
    """Validate that CairoSVG can be imported.

    CairoSVG is used for some asset rendering. On some platforms, missing native
    dependencies can prevent import.
    """

    try:
        import cairosvg  # noqa: F401
    except OSError as exc:
        if os.name == "nt":
            if struct.calcsize("P") * 8 != 64:
                logger.error(
                    "Unable to import cairosvg, ensure your Python is a 64-bit version: https://www.python.org/downloads/"
                )
            else:
                logger.error(
                    "Unable to import cairosvg, install GTK Installer for Windows and restart your system (https://github.com/tschoonj/GTK-for-Windows-Runtime-Environment-Installer/releases/latest)"
                )
        else:
            distro = platform.version().lower()
            if "ubuntu" in distro or "debian" in distro:
                logger.error(
                    "Unable to import cairosvg, try running `sudo apt-get install libpangocairo-1.0-0` or report on our support server with your OS details: https://discord.gg/etJNHCQ"
                )
            else:
                logger.error(
                    "Unable to import cairosvg, report on our support server with your OS details: https://discord.gg/etJNHCQ"
                )

        raise SystemExit(0) from exc


def check_discord_py_version(*, logger, expected: str = "2.6.3") -> None:
    """Ensure the runtime discord.py matches the pinned dependency version."""

    if discord.__version__ != expected:
        logger.error(
            "Dependencies are not updated, run pipenv install. discord.py version expected %s, received %s",
            expected,
            discord.__version__,
        )
        raise SystemExit(0)
