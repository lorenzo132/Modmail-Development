from __future__ import annotations

from modmail import __version__
from modmail.bot import ModmailBot
from modmail.core.models import getLogger
from modmail.startup.bootstrap import bootstrap

logger = getLogger("bot")


def main() -> None:
    """Legacy-compatible entrypoint.

    - Keeps legacy dependency checks
    - Keeps `python bot.py` working via shim
    - Provides a stable import target for `start.py` and `python -m modmail`
    """

    bootstrap(logger=logger)

    logger.debug("Starting Modmail v%s", __version__)
    bot = ModmailBot()
    bot.run()


if __name__ == "__main__":
    main()
