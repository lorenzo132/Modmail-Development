from __future__ import annotations

import asyncio
import os
from typing import Any

import discord
from aiohttp import ClientSession
from packaging.version import Version

from modmail import __version__
from modmail.bot.extensions import DEFAULT_EXTENSIONS
from modmail.core import utils
from modmail.core.clients import ApiClient, MongoDBClient, PluginDatabaseClient
from modmail.core.config import ConfigManager
from modmail.core.models import HostingMethod, PermissionLevel, SafeFormatter, configure_logging, getLogger
from modmail.core.thread import ThreadManager
from modmail.runtime import LOG_DIR, ensure_runtime_dirs

logger = getLogger("bot")


PM2_ENV_KEY = "pm_id"


class BotCoreMixin:
    def __init__(self) -> None:
        ensure_runtime_dirs()

        self.config = ConfigManager(self)
        self.config.populate_cache()

        intents = discord.Intents.all()
        if not self.config["enable_presence_intent"]:
            intents.presences = False

        super().__init__(command_prefix=None, intents=intents)  # implemented in `get_prefix`
        self.session: ClientSession | None = None
        self._api: ApiClient | None = None
        self.formatter = SafeFormatter()
        self.loaded_cogs = list(DEFAULT_EXTENSIONS)
        self._connected: asyncio.Event | None = None
        self.start_time = discord.utils.utcnow()
        self._started = False

        self.threads = ThreadManager(self)
        self._message_queues: dict[int, asyncio.Queue[discord.Message]] = {}  # User ID -> message queue

        self.log_file_path = str(LOG_DIR / "modmail.log")
        configure_logging(self)

        self.plugin_db = PluginDatabaseClient(self)  # Deprecated
        self.startup()

    def get_guild_icon(
        self,
        guild: discord.Guild | None,
        *,
        size: int | None = None,
    ) -> str:
        if guild is None:
            guild = self.guild
        if guild.icon is None:
            return "https://cdn.discordapp.com/embed/avatars/0.png"
        if size is None:
            return guild.icon.url
        return guild.icon.with_size(size).url

    def _resolve_snippet(self, name: str) -> str | None:
        """Return a snippet name resolved through single-step aliasing."""

        if name in self.snippets:
            return name

        try:
            (command,) = utils.parse_alias(self.aliases[name])
        except (KeyError, ValueError):
            return None
        else:
            if command in self.snippets:
                return command
        return None

    @property
    def uptime(self) -> str:
        now = discord.utils.utcnow()
        delta = now - self.start_time
        hours, remainder = divmod(int(delta.total_seconds()), 3600)
        minutes, seconds = divmod(remainder, 60)
        days, hours = divmod(hours, 24)

        fmt = "{h}h {m}m {s}s"
        if days:
            fmt = "{d}d " + fmt

        return self.formatter.format(fmt, d=days, h=hours, m=minutes, s=seconds)

    @property
    def hosting_method(self) -> HostingMethod:
        # use enums
        if ".heroku" in os.environ.get("PYTHONHOME", ""):
            return HostingMethod.HEROKU

        if os.environ.get(PM2_ENV_KEY):
            return HostingMethod.PM2

        if os.environ.get("INVOCATION_ID"):
            return HostingMethod.SYSTEMD

        if os.environ.get("USING_DOCKER"):
            return HostingMethod.DOCKER

        if os.environ.get("TERM"):
            return HostingMethod.SCREEN

        return HostingMethod.OTHER

    def startup(self) -> None:
        logger.line()
        logger.info("┌┬┐┌─┐┌┬┐┌┬┐┌─┐┬┬")
        logger.info("││││ │ │││││├─┤││")
        logger.info("┴ ┴└─┘─┴┘┴ ┴┴ ┴┴┴─┘")
        logger.info("v%s", __version__)
        logger.info("Authors: kyb3r, fourjr, Taaku18")
        logger.line()
        logger.info("discord.py: v%s", discord.__version__)
        logger.line()

    async def load_extensions(self) -> None:
        for cog in self.loaded_cogs:
            if cog in self.extensions:
                continue
            logger.debug("Loading %s.", cog)
            try:
                await self.load_extension(cog)
                logger.debug("Successfully loaded %s.", cog)
            except Exception:
                logger.exception("Failed to load %s.", cog)
        logger.line("debug")

    @property
    def version(self) -> Version:
        return Version(__version__)

    @property
    def api(self) -> ApiClient:
        if self._api is None:
            if self.config["database_type"].lower() == "mongodb":
                self._api = MongoDBClient(self)
            else:
                logger.critical("Invalid database type.")
                raise RuntimeError
        return self._api

    @property
    def db(self) -> Any:
        # deprecated
        return self.api.db

    async def get_prefix(self, message: discord.Message | None = None) -> list[str]:
        _ = message
        return [self.prefix, f"<@{self.user.id}> ", f"<@!{self.user.id}> "]

    def run(self) -> None:
        async def runner() -> None:
            async with self:
                self._connected = asyncio.Event()
                self.session = ClientSession()

                if self.config["enable_presence_intent"]:
                    logger.info("Starting bot with presence intent.")
                else:
                    logger.info("Starting bot without presence intent.")

                try:
                    await self.start(self.token)
                except discord.PrivilegedIntentsRequired:
                    logger.critical(
                        "Privileged intents are not explicitly granted in the discord developers dashboard."
                    )
                except discord.LoginFailure:
                    logger.critical("Invalid token")
                except Exception:
                    logger.critical("Fatal exception", exc_info=True)
                finally:
                    if self.session:
                        await self.session.close()
                    if not self.is_closed():
                        await self.close()

        async def _cancel_tasks() -> None:
            async with self:
                task_retriever = asyncio.all_tasks
                loop = self.loop
                tasks = {t for t in task_retriever() if not t.done() and t.get_coro() != cancel_tasks_coro}

                if not tasks:
                    return

                logger.info("Cleaning up after %d tasks.", len(tasks))
                for task in tasks:
                    task.cancel()

                await asyncio.gather(*tasks, return_exceptions=True)
                logger.info("All tasks finished cancelling.")

                for task in tasks:
                    try:
                        if task.exception() is not None:
                            loop.call_exception_handler(
                                {
                                    "message": "Unhandled exception during Client.run shutdown.",
                                    "exception": task.exception(),
                                    "task": task,
                                }
                            )
                    except (asyncio.InvalidStateError, asyncio.CancelledError):
                        pass

        try:
            asyncio.run(runner(), debug=bool(os.getenv("DEBUG_ASYNCIO")))
        except (KeyboardInterrupt, SystemExit):
            logger.info("Received signal to terminate bot and event loop.")
        finally:
            logger.info("Cleaning up tasks.")

            try:
                cancel_tasks_coro = _cancel_tasks()
                asyncio.run(cancel_tasks_coro)
            finally:
                logger.info("Closing the event loop.")

    @property
    def bot_owner_ids(self) -> set[int]:
        owner_ids = self.config["owners"]
        owner_ids = set(map(int, str(owner_ids).split(","))) if owner_ids is not None else set()

        if self.owner_id is not None:
            owner_ids.add(self.owner_id)

        permissions = self.config["level_permissions"].get(PermissionLevel.OWNER.name, [])
        for perm in permissions:
            owner_ids.add(int(perm))
        return owner_ids

    async def is_owner(self, user: discord.User) -> bool:
        if user.id in self.bot_owner_ids:
            return True
        return await super().is_owner(user)

    async def wait_for_connected(self) -> None:
        await self.wait_until_ready()
        if self._connected is not None:
            await self._connected.wait()
        await self.config.wait_until_ready()

    def command_perm(self, command_name: str) -> PermissionLevel:
        level = self.config["override_command_level"].get(command_name)
        if level is not None:
            try:
                return PermissionLevel[level.upper()]
            except KeyError:
                logger.warning("Invalid override_command_level for command %s.", command_name)
                self.config["override_command_level"].pop(command_name)

        command = self.get_command(command_name)
        if command is None:
            logger.debug("Command %s not found.", command_name)
            return PermissionLevel.INVALID
        level = next(
            (check.permission_level for check in command.checks if hasattr(check, "permission_level")),
            None,
        )
        if level is None:
            logger.debug("Command %s does not have a permission level.", command_name)
            return PermissionLevel.INVALID
        return level
