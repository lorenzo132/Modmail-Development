from __future__ import annotations

import discord

from modmail.core.models import getLogger

logger = getLogger("bot")


class BotConfigMixin:
    @property
    def log_channel(self) -> discord.TextChannel | None:
        channel_id = self.config["log_channel_id"]
        if channel_id is not None:
            try:
                channel = self.get_channel(int(channel_id))
                if channel is not None:
                    return channel
            except ValueError:
                pass
            logger.debug("LOG_CHANNEL_ID was invalid, removed.")
            self.config.remove("log_channel_id")
        if self.main_category is not None:
            try:
                channel = self.main_category.channels[0]
                self.config["log_channel_id"] = channel.id
                logger.warning(
                    "No log channel set, setting #%s to be the log channel.",
                    channel.name,
                )
                return channel
            except IndexError:
                pass
        logger.warning(
            "No log channel set, set one with `%ssetup` or `%sconfig set log_channel_id <id>`.",
            self.prefix,
            self.prefix,
        )
        return None

    @property
    def mention_channel(self) -> discord.TextChannel | None:
        channel_id = self.config["mention_channel_id"]
        if channel_id is not None:
            try:
                channel = self.get_channel(int(channel_id))
                if channel is not None:
                    return channel
            except ValueError:
                pass
            logger.debug("MENTION_CHANNEL_ID was invalid, removed.")
            self.config.remove("mention_channel_id")

        return self.log_channel

    @property
    def update_channel(self) -> discord.TextChannel | None:
        channel_id = self.config["update_channel_id"]
        if channel_id is not None:
            try:
                channel = self.get_channel(int(channel_id))
                if channel is not None:
                    return channel
            except ValueError:
                pass
            logger.debug("UPDATE_CHANNEL_ID was invalid, removed.")
            self.config.remove("update_channel_id")

        return self.log_channel

    @property
    def snippets(self) -> dict[str, str]:
        return self.config["snippets"]

    @property
    def aliases(self) -> dict[str, str]:
        return self.config["aliases"]

    @property
    def auto_triggers(self) -> dict[str, str]:
        return self.config["auto_triggers"]

    @property
    def token(self) -> str:
        token = self.config["token"]
        if token is None:
            logger.critical("TOKEN must be set, set this as bot token found on the Discord Developer Portal.")
            raise SystemExit(0)
        return token

    @property
    def guild_id(self) -> int | None:
        guild_id = self.config["guild_id"]
        if guild_id is not None:
            try:
                return int(str(guild_id))
            except ValueError:
                self.config.remove("guild_id")
                logger.critical("Invalid GUILD_ID set.")
        else:
            logger.debug("No GUILD_ID set.")
        return None

    @property
    def guild(self) -> discord.Guild | None:
        """The guild that the bot is serving (the server where users message it from)"""
        return discord.utils.get(self.guilds, id=self.guild_id)

    @property
    def modmail_guild(self) -> discord.Guild | None:
        """The guild that the bot is operating in (where the bot is creating threads)"""
        modmail_guild_id = self.config["modmail_guild_id"]
        if modmail_guild_id is None:
            return self.guild
        try:
            guild = discord.utils.get(self.guilds, id=int(modmail_guild_id))
            if guild is not None:
                return guild
        except ValueError:
            pass
        self.config.remove("modmail_guild_id")
        logger.critical("Invalid MODMAIL_GUILD_ID set.")
        return self.guild

    @property
    def using_multiple_server_setup(self) -> bool:
        return self.modmail_guild != self.guild

    @property
    def main_category(self) -> discord.CategoryChannel | None:
        if self.modmail_guild is not None:
            category_id = self.config["main_category_id"]
            if category_id is not None:
                try:
                    cat = discord.utils.get(self.modmail_guild.categories, id=int(category_id))
                    if cat is not None:
                        return cat
                except ValueError:
                    pass
                self.config.remove("main_category_id")
                logger.debug("MAIN_CATEGORY_ID was invalid, removed.")
            cat = discord.utils.get(self.modmail_guild.categories, name="Modmail")
            if cat is not None:
                self.config["main_category_id"] = cat.id
                logger.debug(
                    'No main category set explicitly, setting category "Modmail" as the main category.'
                )
                return cat
        return None

    @property
    def blocked_users(self) -> dict[str, str]:
        return self.config["blocked"]

    @property
    def blocked_roles(self) -> dict[str, str]:
        return self.config["blocked_roles"]

    @property
    def blocked_whitelisted_users(self) -> list[str]:
        return self.config["blocked_whitelist"]

    @property
    def prefix(self) -> str:
        return str(self.config["prefix"])

    @property
    def mod_color(self) -> int:
        return self.config.get("mod_color")

    @property
    def recipient_color(self) -> int:
        return self.config.get("recipient_color")

    @property
    def main_color(self) -> int:
        return self.config.get("main_color")

    @property
    def error_color(self) -> int:
        return self.config.get("error_color")
