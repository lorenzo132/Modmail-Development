from __future__ import annotations

import asyncio
import os
from subprocess import PIPE

import discord
import isodate
from aiohttp import ClientResponseError
from discord.ext import tasks
from packaging.version import Version

from modmail.core.changelog import Changelog
from modmail.core.models import HostingMethod, InvalidConfigError, getLogger

logger = getLogger("bot")


class BotTasksMixin:
    @tasks.loop(hours=1)
    async def post_metadata(self) -> None:
        info = await self.application_info()

        delta = discord.utils.utcnow() - self.start_time
        data = {
            "bot_id": self.user.id,
            "bot_name": str(self.user),
            "avatar_url": self.user.display_avatar.url,
            "guild_id": self.guild_id,
            "guild_name": self.guild.name,
            "member_count": len(self.guild.members),
            "uptime": delta.total_seconds(),
            "latency": f"{self.ws.latency * 1000:.4f}",
            "version": str(self.version),
            "selfhosted": True,
            "last_updated": str(discord.utils.utcnow()),
        }

        if info.team is not None:
            data.update(
                {
                    "owner_name": info.team.owner.name if info.team.owner is not None else "No Owner",
                    "owner_id": info.team.owner_id,
                    "team": True,
                }
            )
        else:
            data.update(
                {
                    "owner_name": info.owner.name,
                    "owner_id": info.owner.id,
                    "team": False,
                }
            )

        async with self.session.post("https://api.modmail.dev/metadata", json=data):
            logger.debug("Uploading metadata to Modmail server.")

    @post_metadata.before_loop
    async def before_post_metadata(self) -> None:
        await self.wait_for_connected()
        if not self.config.get("data_collection") or not self.guild:
            self.post_metadata.cancel()
            return

        logger.debug("Starting metadata loop.")
        logger.line("debug")

    @tasks.loop(hours=1)
    async def autoupdate(self) -> None:
        changelog = await Changelog.from_url(self)
        latest = changelog.latest_version

        if self.version < Version(latest.version):
            error = None
            data = {}
            try:
                data = await self.api.update_repository()
            except InvalidConfigError:
                pass
            except ClientResponseError as exc:
                error = exc
            if self.hosting_method == HostingMethod.HEROKU:
                if error is not None:
                    logger.error("Autoupdate failed! Status: %s.", error.status)
                    logger.error("Error message: %s", error.message)
                    self.autoupdate.cancel()
                    return

                commit_data = data.get("data")
                if not commit_data:
                    return

                logger.info("Bot has been updated.")

                if not self.config["update_notifications"]:
                    return

                embed = discord.Embed(color=self.main_color)
                message = commit_data["commit"]["message"]
                html_url = commit_data["html_url"]
                short_sha = commit_data["sha"][:6]
                user = data["user"]
                embed.add_field(
                    name="Merge Commit",
                    value=f"[`{short_sha}`]({html_url}) {message} - {user['username']}",
                )
                embed.set_author(
                    name=user["username"] + " - Updating Bot",
                    icon_url=user["avatar_url"],
                    url=user["url"],
                )

                embed.set_footer(text=f"Updating Modmail v{self.version} -> v{latest.version}")

                embed.description = latest.description
                for name, value in latest.fields.items():
                    embed.add_field(name=name, value=value)

                channel = self.update_channel
                await channel.send(embed=embed)
            else:
                command = "git pull"
                proc = await asyncio.create_subprocess_shell(
                    command,
                    stderr=PIPE,
                    stdout=PIPE,
                )
                err = await proc.stderr.read()
                err = err.decode("utf-8").rstrip()
                res = await proc.stdout.read()
                res = res.decode("utf-8").rstrip()

                if err and not res:
                    logger.warning("Autoupdate failed: %s", err)
                    self.autoupdate.cancel()
                    return

                if res != "Already up to date.":
                    if os.getenv("PIPENV_ACTIVE"):
                        await asyncio.create_subprocess_shell(
                            "pipenv sync",
                            stderr=PIPE,
                            stdout=PIPE,
                        )
                        message = ""
                    else:
                        message = "\n\nDo manually update dependencies if your bot has crashed."

                    logger.info("Bot has been updated.")
                    channel = self.update_channel
                    if self.hosting_method in (
                        HostingMethod.PM2,
                        HostingMethod.SYSTEMD,
                    ):
                        embed = discord.Embed(title="Bot has been updated", color=self.main_color)
                        embed.set_footer(
                            text=f"Updating Modmail v{self.version} " f"-> v{latest.version} {message}"
                        )
                        if self.config["update_notifications"]:
                            await channel.send(embed=embed)
                    else:
                        embed = discord.Embed(
                            title="Bot has been updated and is logging out.",
                            description=f"If you do not have an auto-restart setup, please manually start the bot. {message}",
                            color=self.main_color,
                        )
                        embed.set_footer(text=f"Updating Modmail v{self.version} -> v{latest.version}")
                        if self.config["update_notifications"]:
                            await channel.send(embed=embed)
                    return await self.close()

    @autoupdate.before_loop
    async def before_autoupdate(self) -> None:
        await self.wait_for_connected()
        logger.debug("Starting autoupdate loop")

        if self.config.get("disable_autoupdates"):
            logger.warning("Autoupdates disabled.")
            self.autoupdate.cancel()
            return

        if self.hosting_method == HostingMethod.DOCKER:
            logger.warning("Autoupdates disabled as using Docker.")
            self.autoupdate.cancel()
            return

        if not self.config.get("github_token") and self.hosting_method == HostingMethod.HEROKU:
            logger.warning("GitHub access token not found.")
            logger.warning("Autoupdates disabled.")
            self.autoupdate.cancel()
            return

    @tasks.loop(hours=1, reconnect=False)
    async def log_expiry(self) -> None:
        log_expire_after = self.config.get("log_expiration")
        if log_expire_after == isodate.Duration():
            return self.log_expiry.stop()

        now = discord.utils.utcnow()
        expiration_datetime = now - log_expire_after
        expired_logs = await self.db.logs.delete_many({"closed_at": {"$lte": str(expiration_datetime)}})

        logger.info("Deleted %s expired logs.", expired_logs.deleted_count)
