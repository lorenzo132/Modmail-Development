from __future__ import annotations

import asyncio
import copy
import hashlib
import re
import string
from datetime import UTC, datetime
from types import SimpleNamespace

import discord
import isodate
from discord.ext import commands
from discord.ext.commands.view import StringView
from emoji import is_emoji

from modmail.core import checks, utils
from modmail.core.models import DMDisabled, PermissionLevel, getLogger
from modmail.core.time import human_timedelta

logger = getLogger("bot")


class BotThreadingMixin:
    async def convert_emoji(self, name: str) -> str:
        ctx = SimpleNamespace(bot=self, guild=self.modmail_guild)
        converter = commands.EmojiConverter()

        if not is_emoji(name):
            try:
                name = await converter.convert(ctx, name.strip(":"))
            except commands.BadArgument as e:
                logger.warning("%s is not a valid emoji: %s", name, e)
                raise
        return name

    async def get_or_fetch_user(self, id: int) -> discord.User:
        """Retrieve a User based on their ID."""
        return self.get_user(id) or await self.fetch_user(id)

    @staticmethod
    async def get_or_fetch_member(guild: discord.Guild, member_id: int) -> discord.Member | None:
        """Attempt to get a member from cache; on failure fetch from the API."""
        return guild.get_member(member_id) or await guild.fetch_member(member_id)

    async def retrieve_emoji(self) -> tuple[str, str]:
        sent_emoji = self.config["sent_emoji"]
        blocked_emoji = self.config["blocked_emoji"]

        if sent_emoji != "disable":
            try:
                sent_emoji = await self.convert_emoji(sent_emoji)
            except commands.BadArgument:
                logger.warning("Removed sent emoji (%s).", sent_emoji)
                sent_emoji = self.config.remove("sent_emoji")
                await self.config.update()

        if blocked_emoji != "disable":
            try:
                blocked_emoji = await self.convert_emoji(blocked_emoji)
            except commands.BadArgument:
                logger.warning("Removed blocked emoji (%s).", blocked_emoji)
                blocked_emoji = self.config.remove("blocked_emoji")
                await self.config.update()

        return sent_emoji, blocked_emoji

    def check_account_age(self, author: discord.Member) -> bool:
        account_age = self.config.get("account_age")
        now = discord.utils.utcnow()

        try:
            min_account_age = author.created_at + account_age
        except ValueError:
            logger.warning("Error with 'account_age'.", exc_info=True)
            min_account_age = author.created_at + self.config.remove("account_age")

        if min_account_age > now:
            delta = human_timedelta(min_account_age)
            logger.debug("Blocked due to account age, user %s.", author.name)

            if str(author.id) not in self.blocked_users:
                new_reason = f"System Message: New Account. User can try again {delta}."
                self.blocked_users[str(author.id)] = new_reason

            return False
        return True

    def check_guild_age(self, author: discord.Member) -> bool:
        guild_age = self.config.get("guild_age")
        now = discord.utils.utcnow()

        if not hasattr(author, "joined_at"):
            logger.warning("Not in guild, cannot verify guild_age, %s.", author.name)
            return True

        try:
            min_guild_age = author.joined_at + guild_age
        except ValueError:
            logger.warning("Error with 'guild_age'.", exc_info=True)
            min_guild_age = author.joined_at + self.config.remove("guild_age")

        if min_guild_age > now:
            delta = human_timedelta(min_guild_age)
            logger.debug("Blocked due to guild age, user %s.", author.name)

            if str(author.id) not in self.blocked_users:
                new_reason = f"System Message: Recently Joined. User can try again {delta}."
                self.blocked_users[str(author.id)] = new_reason

            return False
        return True

    def check_manual_blocked_roles(self, author: discord.Member) -> bool:
        if isinstance(author, discord.Member):
            for r in author.roles:
                if str(r.id) in self.blocked_roles:
                    blocked_reason = self.blocked_roles.get(str(r.id)) or ""

                    try:
                        end_time, after = utils.extract_block_timestamp(blocked_reason, author.id)
                    except ValueError:
                        return False

                    if end_time is not None and after <= 0:
                        self.blocked_roles.pop(str(r.id))
                        logger.debug("No longer blocked, role %s.", r.name)
                        return True
                    logger.debug("User blocked, role %s.", r.name)
                    return False

        return True

    def check_manual_blocked(self, author: discord.Member) -> bool:
        if str(author.id) not in self.blocked_users:
            return True

        blocked_reason = self.blocked_users.get(str(author.id)) or ""

        if blocked_reason.startswith("System Message:"):
            logger.debug("No longer internally blocked, user %s.", author.name)
            self.blocked_users.pop(str(author.id))
            return True

        try:
            end_time, after = utils.extract_block_timestamp(blocked_reason, author.id)
        except ValueError:
            return False

        if end_time is not None and after <= 0:
            self.blocked_users.pop(str(author.id))
            logger.debug("No longer blocked, user %s.", author.name)
            return True
        logger.debug("User blocked, user %s.", author.name)
        return False

    async def _process_blocked(self, message: discord.Message) -> bool:
        _, blocked_emoji = await self.retrieve_emoji()
        if await self.is_blocked(message.author, channel=message.channel, send_message=True):
            await self.add_reaction(message, blocked_emoji)
            return True
        return False

    async def is_blocked(
        self,
        author: discord.User,
        *,
        channel: discord.TextChannel | None = None,
        send_message: bool = False,
    ) -> bool:
        member = self.guild.get_member(author.id)
        if member is None:
            for g in self.guilds:
                member = g.get_member(author.id)
                if member:
                    break

            if member is None:
                logger.debug("User not in guild, %s.", author.id)

        if member is not None:
            author = member

        if str(author.id) in self.blocked_whitelisted_users:
            if str(author.id) in self.blocked_users:
                self.blocked_users.pop(str(author.id))
                await self.config.update()
            return False

        blocked_reason = self.blocked_users.get(str(author.id)) or ""

        if not self.check_account_age(author) or not self.check_guild_age(author):
            new_reason = self.blocked_users.get(str(author.id))
            if new_reason != blocked_reason and send_message:
                await channel.send(
                    embed=discord.Embed(
                        title="Message not sent!",
                        description=new_reason,
                        color=self.error_color,
                    )
                )
            return True

        if not self.check_manual_blocked(author):
            return True

        if not self.check_manual_blocked_roles(author):
            return True

        await self.config.update()
        return False

    async def get_thread_cooldown(self, author: discord.Member):
        thread_cooldown = self.config.get("thread_cooldown")
        now = discord.utils.utcnow()

        if thread_cooldown == isodate.Duration():
            return

        last_log = await self.api.get_latest_user_logs(author.id)

        if last_log is None:
            logger.debug("Last thread wasn't found, %s.", author.name)
            return

        last_log_closed_at = last_log.get("closed_at")

        if not last_log_closed_at:
            logger.debug("Last thread was not closed, %s.", author.name)
            return

        try:
            cooldown = datetime.fromisoformat(last_log_closed_at).astimezone(UTC) + thread_cooldown
        except ValueError:
            logger.warning("Error with 'thread_cooldown'.", exc_info=True)
            cooldown = datetime.fromisoformat(last_log_closed_at).astimezone(UTC) + self.config.remove(
                "thread_cooldown"
            )

        if cooldown > now:
            delta = human_timedelta(cooldown)
            logger.debug("Blocked due to thread cooldown, user %s.", author.name)
            return delta
        return

    @staticmethod
    async def add_reaction(
        msg: discord.Message,
        reaction: discord.Emoji | discord.Reaction | discord.PartialEmoji | str,
    ) -> bool:
        if reaction != "disable":
            try:
                await msg.add_reaction(reaction)
            except (discord.HTTPException, TypeError) as e:
                logger.warning("Failed to add reaction %s: %s.", reaction, e)
                return False
        return True

    async def _queue_dm_message(self, message: discord.Message) -> None:
        """Queue DM messages to ensure they're processed in order per user."""
        user_id = message.author.id

        if user_id not in self._message_queues:
            self._message_queues[user_id] = asyncio.Queue()
            self.loop.create_task(self._process_user_messages(user_id))

        await self._message_queues[user_id].put(message)

    async def _process_user_messages(self, user_id: int) -> None:
        """Process messages for a specific user in order."""
        queue = self._message_queues[user_id]

        while True:
            try:
                message = await asyncio.wait_for(queue.get(), timeout=300)
                await self.process_dm_modmail(message)
                queue.task_done()
            except TimeoutError:
                if queue.empty():
                    self._message_queues.pop(user_id, None)
                    break
            except Exception as e:
                logger.error(f"Error processing message for user {user_id}: {e}", exc_info=True)
                queue.task_done()

    async def process_dm_modmail(self, message: discord.Message) -> None:
        """Processes messages sent to the bot."""
        blocked = await self._process_blocked(message)
        if blocked:
            return
        sent_emoji, blocked_emoji = await self.retrieve_emoji()

        if hasattr(message, "flags") and getattr(message.flags, "has_snapshot", False):
            if hasattr(message, "message_snapshots") and message.message_snapshots:
                thread = await self.threads.find(recipient=message.author)
                if thread is None:
                    delta = await self.get_thread_cooldown(message.author)
                    if delta:
                        await message.channel.send(
                            embed=discord.Embed(
                                title=self.config["cooldown_thread_title"],
                                description=self.config["cooldown_thread_response"].format(delta=delta),
                                color=self.error_color,
                            )
                        )
                        return
                    if self.config["dm_disabled"] in (DMDisabled.NEW_THREADS, DMDisabled.ALL_THREADS):
                        embed = discord.Embed(
                            title=self.config["disabled_new_thread_title"],
                            color=self.error_color,
                            description=self.config["disabled_new_thread_response"],
                        )
                        embed.set_footer(
                            text=self.config["disabled_new_thread_footer"],
                            icon_url=self.get_guild_icon(guild=message.guild, size=128),
                        )
                        logger.info(
                            "A new thread was blocked from %s due to disabled Modmail.", message.author
                        )
                        await self.add_reaction(message, blocked_emoji)
                        return await message.channel.send(embed=embed)
                    thread = await self.threads.create(message.author, message=message)
                else:
                    if self.config["dm_disabled"] == DMDisabled.ALL_THREADS:
                        embed = discord.Embed(
                            title=self.config["disabled_current_thread_title"],
                            color=self.error_color,
                            description=self.config["disabled_current_thread_response"],
                        )
                        embed.set_footer(
                            text=self.config["disabled_current_thread_footer"],
                            icon_url=self.get_guild_icon(guild=message.guild, size=128),
                        )
                        logger.info("A message was blocked from %s due to disabled Modmail.", message.author)
                        await self.add_reaction(message, blocked_emoji)
                        return await message.channel.send(embed=embed)
                combined_content = (
                    utils.extract_forwarded_content(message) or "[Forwarded message with no content]"
                )

                class ForwardedMessage:
                    def __init__(self, original_message: discord.Message, forwarded_content: str):
                        self.author = original_message.author
                        self.content = forwarded_content
                        self.attachments = []
                        self.stickers = []
                        self.created_at = original_message.created_at
                        self.embeds = []
                        self.id = original_message.id
                        self.flags = original_message.flags
                        self.message_snapshots = original_message.message_snapshots
                        self.type = getattr(original_message, "type", None)

                forwarded_msg = ForwardedMessage(message, combined_content)
                await thread.send(forwarded_msg)
                await self.add_reaction(message, sent_emoji)
                self.dispatch("thread_reply", thread, False, message, False, False)
                return
            else:
                message.content = "[Forwarded message with no content]"
        elif getattr(message, "type", None) == getattr(discord.MessageType, "forward", None):
            ref = getattr(message, "reference", None)
            if ref and getattr(ref, "type", None) == getattr(discord, "MessageReferenceType", None).forward:
                ref_msg = None
                try:
                    if ref.resolved:
                        ref_msg = ref.resolved
                    elif ref.message_id and ref.channel_id:
                        channel = self.get_channel(ref.channel_id) or (
                            await self.fetch_channel(ref.channel_id)
                        )
                        ref_msg = await channel.fetch_message(ref.message_id)
                except Exception:
                    ref_msg = None
                if ref_msg:
                    thread = await self.threads.find(recipient=message.author)
                    if thread is None:
                        delta = await self.get_thread_cooldown(message.author)
                        if delta:
                            await message.channel.send(
                                embed=discord.Embed(
                                    title=self.config["cooldown_thread_title"],
                                    description=self.config["cooldown_thread_response"].format(delta=delta),
                                    color=self.error_color,
                                )
                            )
                            return
                        if self.config["dm_disabled"] in (DMDisabled.NEW_THREADS, DMDisabled.ALL_THREADS):
                            embed = discord.Embed(
                                title=self.config["disabled_new_thread_title"],
                                color=self.error_color,
                                description=self.config["disabled_new_thread_response"],
                            )
                            embed.set_footer(
                                text=self.config["disabled_new_thread_footer"],
                                icon_url=self.get_guild_icon(guild=message.guild, size=128),
                            )
                            logger.info(
                                "A new thread was blocked from %s due to disabled Modmail.", message.author
                            )
                            await self.add_reaction(message, blocked_emoji)
                            return await message.channel.send(embed=embed)
                        thread = await self.threads.create(message.author, message=message)
                    else:
                        if self.config["dm_disabled"] == DMDisabled.ALL_THREADS:
                            embed = discord.Embed(
                                title=self.config["disabled_current_thread_title"],
                                color=self.error_color,
                                description=self.config["disabled_current_thread_response"],
                            )
                            embed.set_footer(
                                text=self.config["disabled_current_thread_footer"],
                                icon_url=self.get_guild_icon(guild=message.guild, size=128),
                            )
                            logger.info(
                                "A message was blocked from %s due to disabled Modmail.", message.author
                            )
                            await self.add_reaction(message, blocked_emoji)
                            return await message.channel.send(embed=embed)

                    class ForwardedMessage:
                        def __init__(self, original_message: discord.Message, ref_message: discord.Message):
                            self.author = original_message.author
                            extracted_content = utils.extract_forwarded_content(original_message)
                            self.content = (
                                extracted_content
                                or ref_message.content
                                or "[Forwarded message with no text content]"
                            )
                            self.attachments = getattr(ref_message, "attachments", [])
                            self.stickers = getattr(ref_message, "stickers", [])
                            self.created_at = original_message.created_at
                            self.embeds = getattr(ref_message, "embeds", [])
                            self.id = original_message.id
                            self.type = getattr(original_message, "type", None)
                            self.reference = original_message.reference

                    forwarded_msg = ForwardedMessage(message, ref_msg)
                    await thread.send(forwarded_msg)
                    await self.add_reaction(message, sent_emoji)
                    self.dispatch("thread_reply", thread, False, message, False, False)
                    return
                else:
                    message.content = "[Forwarded message with no content]"

        if message.type not in [discord.MessageType.default, discord.MessageType.reply]:
            return

        thread = await self.threads.find(recipient=message.author)
        if thread and thread.snoozed:
            await thread.restore_from_snooze()
            self.threads.cache[thread.id] = thread

        try:
            if (
                thread
                and thread.channel
                and isinstance(thread.channel, discord.TextChannel)
                and self.get_channel(getattr(thread.channel, "id", None)) is None
            ):
                logger.info(
                    "Stale thread detected for %s (channel deleted). Purging cache entry and creating new thread.",
                    message.author,
                )
                self.threads.cache.pop(thread.id, None)
                thread = None
        except Exception:
            self.threads.cache.pop(getattr(thread, "id", None), None)
            thread = None

        if thread is None:
            delta = await self.get_thread_cooldown(message.author)
            if delta:
                await message.channel.send(
                    embed=discord.Embed(
                        title=self.config["cooldown_thread_title"],
                        description=self.config["cooldown_thread_response"].format(delta=delta),
                        color=self.error_color,
                    )
                )
                return

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
                    icon_url=self.get_guild_icon(guild=message.guild, size=128),
                )
                logger.info(
                    "A new thread was blocked from %s due to disabled Modmail.",
                    message.author,
                )
                await self.add_reaction(message, blocked_emoji)
                return await message.channel.send(embed=embed)

            thread = await self.threads.create(message.author, message=message)
            if getattr(thread, "_pending_menu", False):
                return
        else:
            if self.config["dm_disabled"] == DMDisabled.ALL_THREADS:
                embed = discord.Embed(
                    title=self.config["disabled_current_thread_title"],
                    color=self.error_color,
                    description=self.config["disabled_current_thread_response"],
                )
                embed.set_footer(
                    text=self.config["disabled_current_thread_footer"],
                    icon_url=self.get_guild_icon(guild=message.guild, size=128),
                )
                logger.info(
                    "A message was blocked from %s due to disabled Modmail.",
                    message.author,
                )
                await self.add_reaction(message, blocked_emoji)
                return await message.channel.send(embed=embed)

        if not thread.cancelled:
            try:
                await thread.send(message)
            except Exception:
                logger.error("Failed to send message:", exc_info=True)
                await self.add_reaction(message, blocked_emoji)

                try:
                    if (
                        thread
                        and thread.channel
                        and isinstance(thread.channel, discord.TextChannel)
                        and self.get_channel(thread.channel.id) is None
                    ):
                        logger.info(
                            "Relay failed due to deleted channel for %s; creating new thread.",
                            message.author,
                        )
                        self.threads.cache.pop(thread.id, None)
                        new_thread = await self.threads.create(message.author, message=message)
                        if not getattr(new_thread, "_pending_menu", False) and not new_thread.cancelled:
                            try:
                                await new_thread.send(message)
                            except Exception:
                                logger.error(
                                    "Failed to relay message after creating new thread:",
                                    exc_info=True,
                                )
                            else:
                                for user in new_thread.recipients:
                                    if user != message.author:
                                        try:
                                            await new_thread.send(message, user)
                                        except Exception:
                                            logger.error(
                                                "Failed to send message to additional recipient:",
                                                exc_info=True,
                                            )
                                await self.add_reaction(message, sent_emoji)
                                self.dispatch(
                                    "thread_reply",
                                    new_thread,
                                    False,
                                    message,
                                    False,
                                    False,
                                )
                except Exception:
                    logger.warning(
                        "Unexpected failure in DM relay/new-thread follow-up block.",
                        exc_info=True,
                    )
            else:
                for user in thread.recipients:
                    if user != message.author:
                        try:
                            await thread.send(message, user)
                        except Exception:
                            logger.error("Failed to send message:", exc_info=True)

                await self.add_reaction(message, sent_emoji)
                self.dispatch("thread_reply", thread, False, message, False, False)

    def _get_snippet_command(self) -> commands.Command:
        """Get the correct reply command based on the snippet config"""
        modifiers = "f"
        if self.config["plain_snippets"]:
            modifiers += "p"
        if self.config["anonymous_snippets"]:
            modifiers += "a"

        return self.get_command(f"{modifiers}reply")

    async def get_contexts(self, message: discord.Message, *, cls=commands.Context):
        """Returns all invocation contexts from the message."""

        view = StringView(message.content)
        ctx = cls(prefix=self.prefix, view=view, bot=self, message=message)
        thread = await self.threads.find(channel=ctx.channel)

        if message.author.id == self.user.id:  # type: ignore
            return [ctx]

        prefixes = await self.get_prefix()

        invoked_prefix = discord.utils.find(view.skip_string, prefixes)
        if invoked_prefix is None:
            return [ctx]

        invoker = view.get_word().lower()

        try:
            snippet_text = self.snippets[message.content[len(invoked_prefix) :]]
        except KeyError:
            snippet_text = None

        alias = self.aliases.get(invoker)
        if alias is not None and snippet_text is None:
            ctxs = []
            aliases = utils.normalize_alias(alias, message.content[len(f"{invoked_prefix}{invoker}") :])
            if not aliases:
                logger.warning("Alias %s is invalid, removing.", invoker)
                self.aliases.pop(invoker)

            for alias in aliases:
                command = None
                try:
                    snippet_text = self.snippets[alias]
                except KeyError:
                    command_invocation_text = alias
                else:
                    command = self._get_snippet_command()
                    command_invocation_text = f"{invoked_prefix}{command} {snippet_text}"
                view = StringView(invoked_prefix + command_invocation_text)
                ctx_ = cls(prefix=self.prefix, view=view, bot=self, message=message)
                ctx_.thread = thread
                discord.utils.find(view.skip_string, prefixes)
                ctx_.invoked_with = view.get_word().lower()
                ctx_.command = command or self.all_commands.get(ctx_.invoked_with)
                ctxs += [ctx_]
            return ctxs

        ctx.thread = thread

        if snippet_text is not None:
            ctx.command = self._get_snippet_command()
            reply_view = StringView(f"{invoked_prefix}{ctx.command} {snippet_text}")
            discord.utils.find(reply_view.skip_string, prefixes)
            ctx.invoked_with = reply_view.get_word().lower()
            ctx.view = reply_view
        else:
            ctx.command = self.all_commands.get(invoker)
            ctx.invoked_with = invoker

        return [ctx]

    async def trigger_auto_triggers(
        self, message: discord.Message, channel: discord.TextChannel, *, cls=commands.Context
    ):
        message.author = self.modmail_guild.me
        message.channel = channel
        message.guild = channel.guild

        view = StringView(message.content)
        ctx = cls(prefix=self.prefix, view=view, bot=self, message=message)
        thread = await self.threads.find(channel=ctx.channel)

        invoked_prefix = self.prefix
        invoker = None

        if self.config.get("use_regex_autotrigger"):
            trigger = next(filter(lambda x: re.search(x, message.content), self.auto_triggers.keys()))
            if trigger:
                invoker = re.search(trigger, message.content).group(0)
        else:
            trigger = next(
                filter(
                    lambda x: x.lower() in message.content.lower(),
                    self.auto_triggers.keys(),
                )
            )
            if trigger:
                invoker = trigger.lower()

        alias = self.auto_triggers[trigger]

        ctxs = []

        if alias is not None:
            ctxs = []
            aliases = utils.normalize_alias(alias)
            if not aliases:
                logger.warning("Alias %s is invalid as called in autotrigger.", invoker)

        message.author = thread.recipient

        for alias in aliases:
            message.content = invoked_prefix + alias
            ctxs += await self.get_contexts(message)

        message.author = self.modmail_guild.me

        for ctx in ctxs:
            if ctx.command:
                old_checks = copy.copy(ctx.command.checks)
                ctx.command.checks = [checks.has_permissions(PermissionLevel.INVALID)]

                await self.invoke(ctx)

                ctx.command.checks = old_checks
                continue

    async def get_context(self, message: discord.Message, *, cls=commands.Context):
        """Returns the invocation context from the message."""

        view = StringView(message.content)
        ctx = cls(prefix=self.prefix, view=view, bot=self, message=message)

        if message.author.id == self.user.id:
            return ctx

        ctx.thread = await self.threads.find(channel=ctx.channel)

        prefixes = await self.get_prefix()

        invoked_prefix = discord.utils.find(view.skip_string, prefixes)
        if invoked_prefix is None:
            return ctx

        invoker = view.get_word().lower()

        ctx.invoked_with = invoker
        ctx.command = self.all_commands.get(invoker)

        return ctx

    async def update_perms(self, name: PermissionLevel | str, value: int, add: bool = True) -> None:
        if value != -1:
            value = str(value)
        if isinstance(name, PermissionLevel):
            level = True
            permissions = self.config["level_permissions"]
            name = name.name
        else:
            level = False
            permissions = self.config["command_permissions"]
        if name not in permissions:
            if add:
                permissions[name] = [value]
        else:
            if add:
                if value not in permissions[name]:
                    permissions[name].append(value)
            else:
                if value in permissions[name]:
                    permissions[name].remove(value)

        if level:
            self.config["level_permissions"] = permissions
        else:
            self.config["command_permissions"] = permissions
        logger.info("Updating permissions for %s, %s (add=%s).", name, value, add)
        await self.config.update()

    def format_channel_name(self, author, exclude_channel=None, force_null=False):
        """Sanitises a username for use with text channel names

        Placed in main bot class to be extendable to plugins"""
        guild = self.modmail_guild

        if force_null:
            name = new_name = "null"
        else:
            if self.config["use_random_channel_name"]:
                to_hash = self.token.split(".")[-1] + str(author.id)
                digest = hashlib.md5(to_hash.encode("utf8"), usedforsecurity=False)
                name = new_name = digest.hexdigest()[-8:]
            elif self.config["use_user_id_channel_name"]:
                name = new_name = str(author.id)
            elif self.config["use_timestamp_channel_name"]:
                name = new_name = author.created_at.isoformat(sep="-", timespec="minutes")
            else:
                if self.config["use_nickname_channel_name"]:
                    author_member = self.guild.get_member(author.id)
                    name = author_member.display_name.lower()
                else:
                    name = author.name.lower()

                if force_null:
                    name = "null"

                name = (
                    "".join(ch for ch in name if ch not in string.punctuation and ch.isprintable()) or "null"
                )
                if author.discriminator != "0":
                    name += f"-{author.discriminator}"
                new_name = name

        counter = 1
        existed = {c.name for c in guild.text_channels if c != exclude_channel}
        while new_name in existed:
            new_name = f"{name}_{counter}"
            counter += 1

        return new_name
