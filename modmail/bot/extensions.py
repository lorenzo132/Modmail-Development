from __future__ import annotations

"""Central registry of built-in bot extensions.

Keeping this in one place makes it easy to add/remove cogs without hunting
through startup code.
"""

DEFAULT_EXTENSIONS: tuple[str, ...] = (
    "modmail.cogs.modmail",
    "modmail.cogs.plugins",
    "modmail.cogs.utility",
    "modmail.cogs.threadmenu",
)
