from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import discord
from discord import ButtonStyle, Embed, Interaction, Message
from discord.ext import commands
from discord.ui import Button, Select, View


class PaginatorSession:
    """
    Class that interactively paginates something.

    Parameters
    ----------
    ctx : Context
        The context of the command.
    timeout : float
        How long to wait for before the session closes.
    pages : List[Any]
        A list of entries to paginate.

    Attributes
    ----------
    ctx : Context
        The context of the command.
    timeout : float
        How long to wait for before the session closes.
    pages : List[Any]
        A list of entries to paginate.
    running : bool
        Whether the paginate session is running.
    base : Message
        The `Message` of the `Embed`.
    current : int
        The current page number.
    callback_map : Dict[str, method]
        A mapping for text to method.
    view : PaginatorView
        The view that is sent along with the base message.
    select_menu : Select
        A select menu that will be added to the View.
    """

    def __init__(self, ctx: commands.Context, *pages: Any, **options: Any) -> None:
        self.ctx = ctx
        self.timeout: int = options.get("timeout", 210)
        self.running = False
        self.base: Message | None = None
        self.current = 0
        self.pages = list(pages)
        self.destination = options.get("destination", ctx)
        self.view: PaginatorView | None = None
        self.select_menu: Select | None = None

        self.callback_map: dict[str, Callable[[], int]] = {
            "<<": self.first_page,
            "<": self.previous_page,
            ">": self.next_page,
            ">>": self.last_page,
        }
        self._buttons_map: dict[str, PageButton | None] = {"<<": None, "<": None, ">": None, ">>": None}

    async def show_page(self, index: int) -> dict[str, Any] | None:
        """
        Show a page by page number.

        Parameters
        ----------
        index : int
            The index of the page.
        """
        if not 0 <= index < len(self.pages):
            return

        self.current = index
        page = self.pages[index]
        result = None

        if self.running:
            result = self._show_page(page)
        else:
            await self.create_base(page)

        self.update_disabled_status()
        return result

    def update_disabled_status(self) -> None:
        if self.current == self.first_page():
            # disable << button
            if self._buttons_map["<<"] is not None:
                self._buttons_map["<<"].disabled = True

            if self._buttons_map["<"] is not None:
                self._buttons_map["<"].disabled = True
        else:
            if self._buttons_map["<<"] is not None:
                self._buttons_map["<<"].disabled = False

            if self._buttons_map["<"] is not None:
                self._buttons_map["<"].disabled = False

        if self.current == self.last_page():
            # disable >> button
            if self._buttons_map[">>"] is not None:
                self._buttons_map[">>"].disabled = True

            if self._buttons_map[">"] is not None:
                self._buttons_map[">"].disabled = True
        else:
            if self._buttons_map[">>"] is not None:
                self._buttons_map[">>"].disabled = False

            if self._buttons_map[">"] is not None:
                self._buttons_map[">"].disabled = False

    async def create_base(self, item: Any) -> None:
        """
        Create a base `Message`.
        """
        if len(self.pages) == 1:
            self.view = None
            self.running = False
        else:
            self.view = PaginatorView(self, timeout=self.timeout)
            self.update_disabled_status()
            self.running = True

        await self._create_base(item, self.view)

    async def _create_base(self, item: Any, view: View | None) -> None:
        raise NotImplementedError

    def _show_page(self, page: Any) -> dict[str, Any]:
        raise NotImplementedError

    def first_page(self) -> int:
        """Returns the index of the first page"""
        return 0

    def next_page(self) -> int:
        """Returns the index of the next page"""
        return min(self.current + 1, self.last_page())

    def previous_page(self) -> int:
        """Returns the index of the previous page"""
        return max(self.current - 1, self.first_page())

    def last_page(self) -> int:
        """Returns the index of the last page"""
        return len(self.pages) - 1

    async def run(self) -> None:
        """
        Starts the pagination session.
        """
        if not self.running:
            await self.show_page(self.current)

        # Don't block command execution while waiting for the View timeout.
        # Schedule the wait-and-close sequence in the background so the command
        # returns immediately (prevents typing indicator from hanging).
        if self.view is not None:

            async def _wait_and_close() -> None:
                try:
                    await self.view.wait()
                finally:
                    await self.close(delete=False)

            # Fire and forget
            asyncio.create_task(_wait_and_close())
        else:
            await self.close(delete=False)

    async def close(self, delete: bool = True, *, interaction: Interaction | None = None) -> Message | None:
        """
        Closes the pagination session.

        Parameters
        ----------
        delete : bool, optional
            Whether or delete the message upon closure.
            Defaults to `True`.

        Returns
        -------
        Optional[Message]
            If `delete` is `True`.
        """
        if self.running:
            sent_emoji, _ = await self.ctx.bot.retrieve_emoji()
            await self.ctx.bot.add_reaction(self.ctx.message, sent_emoji)

            message = interaction.message if interaction else self.base

            self.running = False

            if self.view is not None:
                self.view.stop()
                if delete:
                    await message.delete()
                else:
                    self.view.clear_items()
                    await message.edit(view=self.view)

            return message

        return interaction.message if interaction else self.base


class PaginatorView(View):
    """
    View that is used for pagination.

    Parameters
    ----------
    handler : PaginatorSession
        The paginator session that spawned this view.
    timeout : float
        How long to wait for before the session closes.

    Attributes
    ----------
    handler : PaginatorSession
        The paginator session that spawned this view.
    timeout : float
        How long to wait for before the session closes.
    """

    def __init__(self, handler: PaginatorSession, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.handler = handler
        self.clear_items()  # clear first so we can control the order
        self.fill_items()

    async def stop_callback(self, interaction: Interaction) -> None:
        await self.handler.close(interaction=interaction)

    def fill_items(self) -> None:
        if self.handler.select_menu is not None:
            self.add_item(self.handler.select_menu)

        for label, callback in self.handler.callback_map.items():
            if len(self.handler.pages) == 2 and label in ("<<", ">>"):
                continue

            style = ButtonStyle.secondary if label in ("<<", ">>") else ButtonStyle.primary

            button = PageButton(self.handler, callback, label=label, style=style)

            self.handler._buttons_map[label] = button
            self.add_item(button)

        stop_button = Button(label="Stop", style=ButtonStyle.danger)
        stop_button.callback = self.stop_callback
        self.add_item(stop_button)

    async def interaction_check(self, interaction: Interaction) -> bool:
        """Only allow the message author to interact"""
        if interaction.user != self.handler.ctx.author:
            await interaction.response.send_message(
                "Only the original author can control this!", ephemeral=True
            )
            return False
        return True


class PageButton(Button):
    """
    A button that has a callback to jump to the next page

    Parameters
    ----------
    handler : PaginatorSession
        The paginator session that spawned this view.
    page_callback : Callable
        A callable that returns an int of the page to go to.

    Attributes
    ----------
    handler : PaginatorSession
        The paginator session that spawned this view.
    page_callback : Callable
        A callable that returns an int of the page to go to.
    """

    def __init__(
        self,
        handler: PaginatorSession,
        page_callback: Callable[[], int],
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.handler = handler
        self.page_callback = page_callback

    async def callback(self, interaction: Interaction) -> None:
        kwargs = await self.handler.show_page(self.page_callback())
        if kwargs is None:
            return
        await interaction.response.edit_message(**kwargs, view=self.view)


class PageSelect(Select):
    def __init__(self, handler: PaginatorSession, pages: list[tuple[str, str]]):
        self.handler = handler
        options = []
        for n, (label, description) in enumerate(pages):
            options.append(discord.SelectOption(label=label, description=description, value=str(n)))

        options = options[:25]  # max 25 options
        super().__init__(placeholder="Select a page", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: Interaction) -> None:
        page = int(self.values[0])
        kwargs = await self.handler.show_page(page)
        if kwargs is None:
            return
        await interaction.response.edit_message(**kwargs, view=self.view)


class EmbedPaginatorSession(PaginatorSession):
    def __init__(self, ctx: commands.Context, *embeds: Embed, **options: Any) -> None:
        super().__init__(ctx, *embeds, **options)

        if len(self.pages) > 1:
            select_options = []
            create_select = True
            for i, embed in enumerate(self.pages):
                footer_text = f"Page {i + 1} of {len(self.pages)}"
                if embed.footer.text:
                    footer_text = footer_text + " • " + embed.footer.text

                if embed.footer.icon:
                    icon_url = embed.footer.icon.url if embed.footer.icon else None
                else:
                    icon_url = None
                embed.set_footer(text=footer_text, icon_url=icon_url)

                # select menu
                if embed.author.name:
                    title = embed.author.name[:30].strip()
                    if len(embed.author.name) > 30:
                        title += "..."
                else:
                    title = embed.title[:30].strip()
                    if len(embed.title) > 30:
                        title += "..."
                    if not title:
                        create_select = False

                if embed.description:
                    description = embed.description[:40].replace("*", "").replace("`", "").strip()
                    if len(embed.description) > 40:
                        description += "..."
                else:
                    description = ""
                select_options.append((title, description))

            if create_select and len({x[0] for x in select_options}) != 1:  # must have unique authors
                self.select_menu = PageSelect(self, select_options)

    def add_page(self, item: Embed) -> None:
        if isinstance(item, Embed):
            self.pages.append(item)
        else:
            raise TypeError("Page must be an Embed object.")

    async def _create_base(self, item: Embed, view: View | None) -> None:
        self.base = await self.destination.send(embed=item, view=view)

    def _show_page(self, page: Embed) -> dict[str, Any]:
        return {"embed": page}


class MessagePaginatorSession(PaginatorSession):
    def __init__(
        self, ctx: commands.Context, *messages: str, embed: Embed | None = None, **options: Any
    ) -> None:
        self.embed = embed
        self.footer_text = self.embed.footer.text if embed is not None else None
        super().__init__(ctx, *messages, **options)

    def add_page(self, item: str) -> None:
        if isinstance(item, str):
            self.pages.append(item)
        else:
            raise TypeError("Page must be a str object.")

    def _set_footer(self) -> None:
        if self.embed is not None:
            footer_text = f"Page {self.current + 1} of {len(self.pages)}"
            if self.footer_text:
                footer_text = footer_text + " • " + self.footer_text

            if self.embed.footer.icon:
                icon_url = self.embed.footer.icon.url if self.embed.footer.icon else None
            else:
                icon_url = None

            self.embed.set_footer(text=footer_text, icon_url=icon_url)

    async def _create_base(self, item: str, view: View | None) -> None:
        self._set_footer()
        self.base = await self.ctx.send(content=item, embed=self.embed, view=view)

    def _show_page(self, page: str) -> dict[str, Any]:
        self._set_footer()
        return {"content": page, "embed": self.embed}
