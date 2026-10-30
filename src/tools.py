"""General web search for Jarvis, kept separate from browser control."""

from __future__ import annotations

import asyncio
import logging

from ddgs import DDGS
from livekit.agents import RunContext, function_tool
from livekit.agents.llm import ToolError

logger = logging.getLogger(__name__)

_MAX_RESULTS = 5


def _search(query: str) -> list[dict[str, str]]:
    with DDGS() as ddgs:
        return list(ddgs.text(query, max_results=_MAX_RESULTS))


@function_tool
async def search_web(context: RunContext, query: str) -> str:
    """Search the open internet and return result titles, links, and snippets.

    Use this only when no website, service, or domain is named. It does not open
    pages: pass a returned link to open_url when the user wants the page read or
    used. Results can be stale or wrong; mention uncertainty when they conflict.

    Args:
        query: Search terms, for example "current weather in London".
    """
    try:
        results = await asyncio.to_thread(_search, query)
    except Exception as exc:
        logger.warning("web search failed", exc_info=True)
        raise ToolError(
            "The web search failed. Try again, or name a site to open."
        ) from exc

    if not results:
        return f'No results for "{query}".'

    lines = [f'Results for "{query}":']
    for index, result in enumerate(results, start=1):
        title = (result.get("title") or "Untitled").strip()
        href = (result.get("href") or "").strip()
        body = (result.get("body") or "").strip()
        lines.append(f"{index}. {title} - {href}")
        if body:
            lines.append(f"   {body[:240]}")

    logger.info("web search for %r returned %d results", query, len(results))
    return "\n".join(lines)
