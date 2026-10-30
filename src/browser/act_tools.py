"""Acting tools: click, type, keys, scroll, and the confirmation gate."""

from __future__ import annotations

import logging
from typing import Literal

from livekit.agents import RunContext, function_tool
from livekit.agents.llm import ToolError

from .controller import BrowserError, BrowserUserData
from .policy import BrowserPolicyError

logger = logging.getLogger("browser")


def _tool_error(exc: Exception) -> ToolError:
    return ToolError(str(exc))


def _confirmation(token: str, description: str) -> str:
    return (
        f"CONFIRMATION_REQUIRED token={token}. {description}. "
        "Tell the user exactly what will happen and ask for a clear yes or no. "
        "Do not claim it is done yet. If they say yes, call confirm_browser_action "
        "with this token. If they say no, drop it and do not retry."
    )


@function_tool
async def click(ctx: RunContext[BrowserUserData], element: str) -> str:
    """Click one element on the current page.

    Call inspect_page first and pass the ref it returned. Consequential clicks
    (buy, submit, send, delete, and similar) return CONFIRMATION_REQUIRED instead
    of acting; wait for the user's explicit yes, then call confirm_browser_action.

    Args:
        element: Element ref from inspect_page, for example "e3".
    """
    browser = ctx.userdata.browser
    try:
        pending = browser.describe_click(element)
        if pending:
            token = browser.stash(pending, lambda: browser.click(element))
            return _confirmation(token, pending)

        ctx.disallow_interruptions()
        async with ctx.with_filler("One moment, sir.", delay=3):
            return await browser.click(element)
    except (BrowserError, BrowserPolicyError) as exc:
        raise _tool_error(exc) from exc


@function_tool
async def type_text(
    ctx: RunContext[BrowserUserData],
    element: str,
    text: str,
    submit: bool = False,
) -> str:
    """Type text into a text box, search box, or similar field on the current page.

    Call inspect_page first and pass the ref it returned. Set submit=true only
    when the user wants the form sent; that path requires explicit confirmation.
    Never type passwords, card numbers, or one-time codes unless the user has just
    given them to you for this exact site.

    Args:
        element: Element ref from inspect_page, for example "e2".
        text: Exact text to enter.
        submit: True to press Enter afterwards and submit the form.
    """
    browser = ctx.userdata.browser
    try:
        if submit:
            description = browser.describe_submit(element, text)
            if description:
                token = browser.stash(
                    description,
                    lambda: browser.type_text(element, text, submit=True),
                )
                return _confirmation(token, description)

        return await browser.type_text(element, text, submit=submit)
    except (BrowserError, BrowserPolicyError) as exc:
        raise _tool_error(exc) from exc


@function_tool
async def press_key(ctx: RunContext[BrowserUserData], key: str) -> str:
    """Press a single keyboard key on the current page.

    Use for Escape, Tab, PageDown, arrow keys, or Enter after the field already
    has focus. Prefer type_text with submit=true for sending a form, because that
    path asks the user first.

    Args:
        key: Key name such as "Enter", "Escape", "Tab", "PageDown", or "ArrowDown".
    """
    browser = ctx.userdata.browser
    try:
        if key.lower() == "enter":
            pending = await browser.describe_enter()
            if pending:
                token = browser.stash(pending, lambda: browser.press_key(key))
                return _confirmation(token, pending)
            ctx.disallow_interruptions()

        return await browser.press_key(key)
    except (BrowserError, BrowserPolicyError) as exc:
        raise _tool_error(exc) from exc


@function_tool
async def scroll(
    ctx: RunContext[BrowserUserData],
    direction: Literal["up", "down", "top", "bottom"] = "down",
    amount: int = 1,
) -> str:
    """Scroll the current page when the content the user wants is not in view.

    Args:
        direction: "up", "down", "top", or "bottom".
        amount: Number of screens to scroll, between 1 and 10.
    """
    try:
        return await ctx.userdata.browser.scroll(direction, amount)
    except (BrowserError, BrowserPolicyError) as exc:
        raise _tool_error(exc) from exc


@function_tool
async def confirm_browser_action(ctx: RunContext[BrowserUserData], token: str) -> str:
    """Run the action that was held for confirmation, after the user says yes.

    Only call this after the user clearly agrees to the exact action you read
    back. Each token works once and expires quickly. Never invent a token.

    Args:
        token: The token from the CONFIRMATION_REQUIRED result.
    """
    ctx.disallow_interruptions()
    try:
        return await ctx.userdata.browser.confirm(token)
    except (BrowserError, BrowserPolicyError) as exc:
        raise _tool_error(exc) from exc


ACT_TOOLS = [click, type_text, press_key, scroll, confirm_browser_action]

__all__ = [
    "ACT_TOOLS",
    "click",
    "confirm_browser_action",
    "press_key",
    "scroll",
    "type_text",
]
