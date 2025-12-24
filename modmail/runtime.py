from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from modmail.core.models import getLogger

logger = getLogger("bot")

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMP_DIR = REPO_ROOT / "temp"
LOG_DIR = TEMP_DIR / "logs"


def ensure_runtime_dirs() -> None:
    """Ensure runtime directories exist.

    Historically, the legacy entrypoint created these at import-time. We do it
    explicitly during startup to reduce import side-effects while keeping the
    same paths.
    """

    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)


def set_windows_event_loop_policy() -> None:
    """Best-effort Windows loop policy selection.

    Keeps legacy behavior: try Proactor policy, log on failure.
    """

    if sys.platform != "win32":
        return

    try:
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    except AttributeError:
        logger.error("Failed to use WindowsProactorEventLoopPolicy.", exc_info=True)


def setup_colorama() -> None:
    """Initialize colorama if installed."""

    try:
        # noinspection PyUnresolvedReferences
        from colorama import init

        init()
    except ImportError:
        return


def is_pipenv_active() -> bool:
    return bool(os.getenv("PIPENV_ACTIVE"))
