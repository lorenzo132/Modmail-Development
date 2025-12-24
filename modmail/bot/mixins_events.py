from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime

import discord
from discord.ext import commands

from modmail import __version__
from modmail.core import checks, utils
from modmail.core.models import DMDisabled, PermissionLevel, getLogger

logger = getLogger("bot")


class BotEventsMixin:
    async def on_connect(self):
        try:
            await self.api.validate_database_connection()
        except Exception:
            logger.debug("Logging out due to failed database connection.")
            return await self.close()

        logger.debug("Connected to gateway.")
        await self.config.refresh()
        await self.api.setup_indexes()
        await self.load_extensions()
        self._connected.set()

    async def on_ready(self):
        """Bot startup, sets uptime."""

        await self.wait_for_connected()

        if self.guild is None:
            logger.error("Logging out due to invalid GUILD_ID.")
            return await self.close()

        if self._started:
            logger.line()
            logger.warning("Bot restarted due to internal discord reloading.")
            logger.line()
            return

        logger.line()
        logger.debug("Client ready.")
        logger.info("Logged in as: %s", self.user)
        logger.info("Bot ID: %s", self.user.id)
        owners = ", ".join(
            getattr(self.get_user(owner_id), "name", str(owner_id)) for owner_id in self.bot_owner_ids
        )
        logger.info("Owners: %s", owners)
        logger.info("Prefix: %s", self.prefix)
        logger.info("Guild Name: %s", self.guild.name)
        logger.info("Guild ID: %s", self.guild.id)
        if self.using_multiple_server_setup:
            logger.info("Receiving guild ID: %s", self.modmail_guild.id)
        logger.line()

        if "dev" in __version__:
            logger.warning(
                "You are running a developmental version. This should not be used in production. (v%s)",
                __version__,
            )
            logger.line()

        await self.threads.populate_cache()

        closures = self.config["closures"]
        logger.info("There are %d thread(s) pending to be closed.", len(closures))
        logger.line()

        for recipient_id, items in tuple(closures.items()):
            after = (
                datetime.fromisoformat(items["time"]).astimezone(UTC) - discord.utils.utcnow()
            ).total_seconds()
            if after <= 0:
                logger.debug("Closing thread for recipient %s.", recipient_id)
                after = 0
            else:
                logger.debug(
                    "Thread for recipient %s will be closed after %s seconds.",
                    recipient_id,
                    after,
                )

            thread = await self.threads.find(recipient_id=int(recipient_id))

            if not thread:
                logger.debug("Failed to close thread for recipient %s.", recipient_id)
                self.config["closures"].pop(recipient_id)
                await self.config.update()
                continue

            await thread.close(
                closer=await self.get_or_fetch_user(items["closer_id"]),
                after=after,
                silent=items["silent"],
                delete_channel=items["delete_channel"],
                message=items["message"],
                auto_close=items.get("auto_close", False),
            )

        for log in await self.api.get_open_logs():
            if log.get("channel_id") is None or self.get_channel(int(log["channel_id"])) is None:
                logger.debug("Unable to resolve thread with channel %s.", log["channel_id"])
                log_data = await self.api.post_log(
                    log["channel_id"],
                    {
                        "open": False,
                        "title": None,
                        "closed_at": str(discord.utils.utcnow()),
                        "close_message": "Channel has been deleted, no closer found.",
                        "closer": {
                            "id": str(self.user.id),
                            "name": self.user.name,
                            "discriminator": self.user.discriminator,
                            "avatar_url": self.user.display_avatar.url,
                            "mod": True,
                        },
                    },
                )
                if log_data:
                    logger.debug("Successfully closed thread with channel %s.", log["channel_id"])
                else:
                    logger.debug(
                        "Failed to close thread with channel %s, skipping.",
                        log["channel_id"],
                    )

        other_guilds = [guild for guild in self.guilds if guild not in {self.guild, self.modmail_guild}]
        if any(other_guilds):
            logger.warning(
                "The bot is in more servers other than the main and staff server. "
                "This may cause data compromise (%s).",
                ", ".join(str(guild.name) for guild in other_guilds),
            )
            logger.warning("If the external servers are valid, you may ignore this message.")

        self.post_metadata.start()
        self.autoupdate.start()
        self.log_expiry.start()
        self._started = True

    async def on_message(self, message):
        await self.wait_for_connected()
        if message.type == discord.MessageType.pins_add and message.author == self.user:
            await message.delete()

        if (
            (f"<@{self.user.id}" in message.content or f"<@!{self.user.id}" in message.content)
            and self.config["alert_on_mention"]
            and not message.author.bot
        ):
            em = discord.Embed(
                title="Bot mention",
                description=f"[Jump URL]({message.jump_url})\n{utils.truncate(message.content, 50)}",
                color=self.main_color,
            )
            if self.config["show_timestamp"]:
                em.timestamp = discord.utils.utcnow()

            content = self.config["mention"] if not self.config["silent_alert_on_mention"] else ""
            await self.mention_channel.send(content=content, embed=em)

        if not message.author.bot and not isinstance(message.channel, discord.DMChannel):
            thread = await self.threads.find(channel=message.channel)
            if thread is not None:
                ctxs = await self.get_contexts(message)
                is_command = any(ctx.command for ctx in ctxs)
                if not is_command:
                    perms = message.channel.permissions_for(message.author)
                    if perms.manage_messages or perms.administrator:
                        await self.api.append_log(message, type_="internal")

        await self.process_commands(message)

    async def process_commands(self, message):
        if message.author.bot:
            return

        if isinstance(message.channel, discord.DMChannel):
            return await self._queue_dm_message(message)

        ctxs = await self.get_contexts(message)
        for ctx in ctxs:
            if ctx.command:
                if not any(1 for check in ctx.command.checks if hasattr(check, "permission_level")):
                    logger.debug(
                        "Command %s has no permissions check, adding invalid level.",
                        ctx.command.qualified_name,
                    )
                    checks.has_permissions(PermissionLevel.INVALID)(ctx.command)

                thread = await self.threads.find(channel=ctx.channel)
                if thread and thread._unsnoozing:
                    queued = await thread.queue_command(ctx, ctx.command)
                    if queued:
                        try:
                            await ctx.message.add_reaction("⏳")
                        except Exception as e:
                            logger.warning("Failed to add queued-reaction: %s", e)
                        continue

                await self.invoke(ctx)
                continue

            thread = await self.threads.find(channel=ctx.channel)
            if thread is not None:
                behavior = (self.config.get("snooze_behavior") or "delete").lower()
                if thread.snoozed and behavior == "move":
                    if not thread.snooze_data:
                        try:
                            log_entry = await self.api.logs.find_one(
                                {"recipient.id": str(thread.id), "snoozed": True}
                            )
                            if log_entry:
                                thread.snooze_data = log_entry.get("snooze_data")
                        except Exception:
                            logger.debug(
                                "Failed to add queued command reaction (⏳).",
                                exc_info=True,
                            )
                    try:
                        await thread.restore_from_snooze()
                        self.threads.cache[thread.id] = thread
                    except Exception as e:
                        logger.warning("Auto-unsnooze on direct message failed: %s", e)
                anonymous = False
                plain = False
                if self.config.get("anon_reply_without_command"):
                    anonymous = True
                if self.config.get("plain_reply_without_command"):
                    plain = True

                if (
                    self.config.get("reply_without_command")
                    or self.config.get("anon_reply_without_command")
                    or self.config.get("plain_reply_without_command")
                ):
                    await thread.reply(message, message.content, anonymous=anonymous, plain=plain)
            elif ctx.invoked_with:
                exc = commands.CommandNotFound(f'Command "{ctx.invoked_with}" is not found')
                self.dispatch("command_error", ctx, exc)

    async def on_typing(self, channel, user, _):
        await self.wait_for_connected()

        if user.bot:
            return

        if isinstance(channel, discord.DMChannel):
            if not self.config.get("user_typing"):
                return

            thread = await self.threads.find(recipient=user)

            if thread:
                try:
                    await thread.channel.typing()
                except Exception:
                    logger.debug(
                        "Failed to trigger typing indicator in recipient DM.",
                        exc_info=True,
                    )
        else:
            if not self.config.get("mod_typing"):
                return

            thread = await self.threads.find(channel=channel)
            if thread is not None and thread.recipient:
                for user in thread.recipients:
                    if await self.is_blocked(user):
                        continue
                    try:
                        await user.typing()
                    except Exception:
                        logger.debug(
                            "Failed to trigger typing for recipient %s.",
                            getattr(user, "id", "?"),
                            exc_info=True,
                        )

    async def handle_reaction_events(self, payload):
        user = self.get_user(payload.user_id)
        if user is None or user.bot:
            return

        channel = self.get_channel(payload.channel_id)
        thread = None
        if not channel:
            thread = await self.threads.find(recipient=user)
            if not thread:
                return
            channel = await thread.recipient.create_dm()
            if channel.id != payload.channel_id:
                return

        from_dm = isinstance(channel, discord.DMChannel)
        from_txt = isinstance(channel, discord.TextChannel)
        if not from_dm and not from_txt:
            return

        if not thread:
            params = {"recipient": user} if from_dm else {"channel": channel}
            thread = await self.threads.find(**params)
            if not thread:
                return

        try:
            message = await channel.fetch_message(payload.message_id)
        except (discord.NotFound, discord.Forbidden):
            return

        reaction = payload.emoji
        close_emoji = await self.convert_emoji(self.config["close_emoji"])
        if from_dm:
            if (
                payload.event_type == "REACTION_ADD"
                and message.embeds
                and str(reaction) == str(close_emoji)
                and self.config.get("recipient_thread_close")
            ):
                ts = message.embeds[0].timestamp
                if ts == thread.channel.created_at:
                    return await thread.close(closer=user)
            if (
                message.author == self.user
                and message.embeds
                and self.config.get("confirm_thread_creation")
                and message.embeds[0].title == self.config["confirm_thread_creation_title"]
                and message.embeds[0].description == self.config["confirm_thread_response"]
            ):
                return
            if not thread.recipient.dm_channel:
                await thread.recipient.create_dm()
            try:
                linked_messages = await thread.find_linked_message_from_dm(message, either_direction=True)
            except ValueError as e:
                logger.warning("Failed to find linked message for reactions: %s", e)
                return
        else:
            try:
                _, *linked_messages = await thread.find_linked_messages(
                    message1=message, either_direction=True
                )
            except ValueError as e:
                logger.warning("Failed to find linked message for reactions: %s", e)
                return

        if self.config["transfer_reactions"] and linked_messages != [None]:
            if payload.event_type == "REACTION_ADD":
                for msg in linked_messages:
                    await self.add_reaction(msg, reaction)
                await self.add_reaction(message, reaction)
            else:
                try:
                    for msg in linked_messages:
                        await msg.remove_reaction(reaction, self.user)
                    await message.remove_reaction(reaction, self.user)
                except (discord.HTTPException, TypeError) as e:
                    logger.warning("Failed to remove reaction: %s", e)

    async def handle_react_to_contact(self, payload):
        react_message_id = utils.tryint(self.config.get("react_to_contact_message"))
        react_message_emoji = self.config.get("react_to_contact_emoji")
        if not all((react_message_id, react_message_emoji)) or payload.message_id != react_message_id:
            return
        if payload.emoji.is_unicode_emoji():
            emoji_fmt = payload.emoji.name
        else:
            emoji_fmt = f"<:{payload.emoji.name}:{payload.emoji.id}>"

        if emoji_fmt != react_message_emoji:
            return
        channel = self.get_channel(payload.channel_id)
        member = channel.guild.get_member(payload.user_id)
        if member.bot:
            return
        message = await channel.fetch_message(payload.message_id)
        await message.remove_reaction(payload.emoji, member)
        await message.add_reaction(emoji_fmt)

        if self.config["dm_disabled"] in (
            DMDisabled.NEW_THREADS,
            DMDisabled.ALL_THREADS,
        ):
            embed = discord.Embed(
                title=self.config["disabled_new_thread_title"],
                color=self.error_color,
                description=self.config["disabled_new_thread_response"],
            )
            embed.set_footer(
                text=self.config["disabled_new_thread_footer"],
                icon_url=self.get_guild_icon(guild=channel.guild, size=128),
            )
            logger.info(
                "A new thread using react to contact was blocked from %s due to disabled Modmail.",
                member,
            )
            return await member.send(embed=embed)

        existing_thread = await self.threads.find(recipient=member)
        if existing_thread and existing_thread.snoozed:
            await existing_thread.restore_from_snooze()
            self.threads.cache[existing_thread.id] = existing_thread
            if existing_thread.channel:
                await existing_thread.channel.send(
                    f"ℹ️ {member.mention} reacted to contact and their snoozed thread has been unsnoozed."
                )
            return

        ctx = await self.get_context(message)
        await ctx.invoke(self.get_command("contact"), users=[member], manual_trigger=False)

    async def on_raw_reaction_add(self, payload):
        await asyncio.gather(
            self.handle_reaction_events(payload),
            self.handle_react_to_contact(payload),
        )

    async def on_raw_reaction_remove(self, payload):
        if self.config["transfer_reactions"]:
            await self.handle_reaction_events(payload)

    async def on_guild_channel_delete(self, channel):
        if channel.guild != self.modmail_guild:
            return

        if isinstance(channel, discord.CategoryChannel):
            if self.main_category == channel:
                logger.debug("Main category was deleted.")
                self.config.remove("main_category_id")
                await self.config.update()
            return

        if not isinstance(channel, discord.TextChannel):
            return

        if self.log_channel is None or self.log_channel == channel:
            logger.info("Log channel deleted.")
            self.config.remove("log_channel_id")
            await self.config.update()
            return

        if not self.modmail_guild.me.guild_permissions.view_audit_log:
            logger.debug(
                "Skipping audit log lookup for deleted channel %d: missing view_audit_log permission.",
                channel.id,
            )
            return

        try:
            audit_logs = self.modmail_guild.audit_logs(limit=10, action=discord.AuditLogAction.channel_delete)
            found_entry = False
            async for entry in audit_logs:
                if int(entry.target.id) == channel.id:
                    found_entry = True
                    break
        except discord.Forbidden:
            logger.debug(
                "Forbidden when fetching audit logs for deleted channel %d (missing permission).", channel.id
            )
            return
        except discord.HTTPException as e:
            logger.debug("HTTPException when fetching audit logs for deleted channel %d: %s", channel.id, e)
            return

        if not found_entry:
            logger.debug("Cannot find the audit log entry for channel delete of %d.", channel.id)
            return

        mod = entry.user
        if mod == self.user:
            return

        thread = await self.threads.find(channel=channel)
        if thread and thread.channel == channel:
            logger.debug("Manually closed channel %s.", channel.name)
            await thread.close(closer=mod, silent=True, delete_channel=False)

    async def on_member_remove(self, member):
        thread = await self.threads.find(recipient=member)
        if thread:
            if member.guild == self.guild and self.config["close_on_leave"]:
                await thread.close(
                    closer=member.guild.me,
                    message=self.config["close_on_leave_reason"],
                    silent=True,
                )
            else:
                if len(self.guilds) > 1:
                    guild_left = member.guild
                    remaining_guilds = member.mutual_guilds

                    if remaining_guilds:
                        remaining_guild_names = [guild.name for guild in remaining_guilds]
                        leave_message = (
                            f"The recipient has left {guild_left}. "
                            f"They are still in {utils.human_join(remaining_guild_names, final='and')}."
                        )
                    else:
                        leave_message = (
                            f"The recipient has left {guild_left}. We no longer share any mutual servers."
                        )
                else:
                    leave_message = "The recipient has left the server."

                embed = discord.Embed(description=leave_message, color=self.error_color)
                await thread.channel.send(embed=embed)

    async def on_member_join(self, member):
        thread = await self.threads.find(recipient=member)
        if thread:
            if len(self.guilds) > 1:
                guild_joined = member.guild
                join_message = f"The recipient has joined {guild_joined}."
            else:
                join_message = "The recipient has joined the server."
            embed = discord.Embed(description=join_message, color=self.mod_color)
            await thread.channel.send(embed=embed)

    async def on_message_delete(self, message):
        """Support for deleting linked messages"""

        if message.is_system():
            return

        if isinstance(message.channel, discord.DMChannel):
            if message.author == self.user:
                return
            thread = await self.threads.find(recipient=message.author)
            if not thread:
                return
            try:
                message = await thread.find_linked_message_from_dm(message, get_thread_channel=True)
            except ValueError as e:
                if str(e) != "Thread channel message not found.":
                    logger.debug("Failed to find linked message to delete: %s", e)
                return
            message = message[0]
            embed = message.embeds[0]

            icon_url = embed.footer.icon.url if embed.footer.icon else None

            embed.set_footer(text=f"{embed.footer.text} (deleted)", icon_url=icon_url)
            await message.edit(embed=embed)
            return

        if message.author != self.user:
            return

        thread = await self.threads.find(channel=message.channel)
        if not thread:
            return

        try:
            await thread.delete_message(message, note=False)
            embed = discord.Embed(description="Successfully deleted message.", color=self.main_color)
        except ValueError as e:
            if str(e) not in {
                "DM message not found.",
                "Malformed thread message.",
                "Thread message not found.",
            }:
                logger.debug("Failed to find linked message to delete: %s", e)
                embed = discord.Embed(description="Failed to delete message.", color=self.error_color)
            else:
                return
        except discord.NotFound:
            return
        embed.set_footer(text=f"Message ID: {message.id} from {message.author}.")
        return await message.channel.send(embed=embed)

    async def on_bulk_message_delete(self, messages):
        await discord.utils.async_all(self.on_message_delete(msg) for msg in messages)

    async def on_message_edit(self, before, after):
        if after.author.bot:
            return
        if before.content == after.content:
            return

        if isinstance(after.channel, discord.DMChannel):
            thread = await self.threads.find(recipient=before.author)
            if not thread:
                return

            try:
                await thread.edit_dm_message(after, after.content)
            except ValueError:
                _, blocked_emoji = await self.retrieve_emoji()
                await self.add_reaction(after, blocked_emoji)
            else:
                embed = discord.Embed(description="Successfully Edited Message", color=self.main_color)
                embed.set_footer(text=f"Message ID: {after.id}")
                await after.channel.send(embed=embed)

    async def on_error(self, event_method, *args, **kwargs):
        logger.error("Ignoring exception in %s.", event_method)
        logger.error("Unexpected exception:", exc_info=sys.exc_info())

    async def on_command_error(
        self,
        context: commands.Context,
        exception: Exception,
        *,
        unhandled_by_cog: bool = False,
    ) -> None:
        if not unhandled_by_cog:
            command = context.command
            if command and command.has_error_handler():
                return
            cog = context.cog
            if cog and cog.has_error_handler():
                return

        if isinstance(exception, (commands.BadArgument, commands.BadUnionArgument)):
            try:
                await context.typing()
            except Exception:
                logger.debug(
                    "Failed to start typing context for command error feedback.",
                    exc_info=True,
                )
            await context.send(embed=discord.Embed(color=self.error_color, description=str(exception)))
        elif isinstance(exception, commands.CommandNotFound):
            logger.warning("CommandNotFound: %s", exception)
        elif isinstance(exception, commands.MissingRequiredArgument):
            await context.send_help(context.command)
        elif isinstance(exception, commands.CommandOnCooldown):
            await context.send(
                embed=discord.Embed(
                    title="Command on cooldown",
                    description=f"Try again in {exception.retry_after:.2f} seconds",
                    color=self.error_color,
                )
            )
        elif isinstance(exception, commands.CheckFailure):
            for check in context.command.checks:
                if not await check(context):
                    if hasattr(check, "fail_msg"):
                        await context.send(
                            embed=discord.Embed(color=self.error_color, description=check.fail_msg)
                        )
                    if hasattr(check, "permission_level"):
                        corrected_permission_level = self.command_perm(context.command.qualified_name)
                        logger.warning(
                            "User %s does not have permission to use this command: `%s` (%s).",
                            context.author.name,
                            context.command.qualified_name,
                            corrected_permission_level.name,
                        )
            logger.warning("CheckFailure: %s", exception)
        elif isinstance(exception, commands.DisabledCommand):
            logger.info(
                "DisabledCommand: %s is trying to run eval but it's disabled",
                context.author.name,
            )
        else:
            logger.error("Unexpected exception:", exc_info=exception)
