from __future__ import annotations

from discord.ext import commands

from .mixins_config import BotConfigMixin
from .mixins_core import BotCoreMixin
from .mixins_events import BotEventsMixin
from .mixins_tasks import BotTasksMixin
from .mixins_threading import BotThreadingMixin


class ModmailBot(
    BotCoreMixin,
    BotConfigMixin,
    BotThreadingMixin,
    BotEventsMixin,
    BotTasksMixin,
    commands.Bot,
):
    """Modmail bot implementation.

    Split into mixins to keep each file focused while preserving the public
    API and behavior of the legacy monolithic implementation.
    """
