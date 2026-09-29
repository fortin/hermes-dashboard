"""Persistent MCP client sessions (stdio + streamable HTTP)."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import AsyncExitStack
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

logger = logging.getLogger(__name__)


class McpServerHandle:
    def __init__(self, name: str, session: ClientSession, stack: AsyncExitStack):
        self.name = name
        self.session = session
        self._stack = stack
        self._lock = asyncio.Lock()

    async def call_tool(self, tool: str, arguments: dict[str, Any] | None = None) -> Any:
        async with self._lock:
            result = await self.session.call_tool(tool, arguments or {})
        return _normalize_tool_result(result)

    async def close(self) -> None:
        await self._stack.aclose()


def is_cancel_scope_noise(exc: BaseException, _seen: set[int] | None = None) -> bool:
    """True for anyio/MCP shutdown when close runs on a different task than open."""
    seen = _seen if _seen is not None else set()
    ident = id(exc)
    if ident in seen:
        return False
    seen.add(ident)
    if "cancel scope" in str(exc).lower():
        return True
    for inner in getattr(exc, "exceptions", ()) or ():
        if isinstance(inner, BaseException) and is_cancel_scope_noise(inner, seen):
            return True
    for linked in (exc.__cause__, exc.__context__):
        if isinstance(linked, BaseException) and is_cancel_scope_noise(linked, seen):
            return True
    return False


_CONNECTION_FAILURES = (
    "lost the connection",
    "waiting for authorization",
    "failed to connect",
    "not connected",
    "invalid response",
    "timed out",
    "xpc connection",
)


def _content_texts(result: Any) -> list[str]:
    texts: list[str] = []
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if text:
            texts.append(text)
    return texts


def _connection_failure(texts: list[str]) -> str | None:
    blob = " ".join(texts).strip()
    if not blob:
        return None
    lowered = blob.lower()
    if any(snippet in lowered for snippet in _CONNECTION_FAILURES):
        return blob[:300]
    return None


def _structured_has_items(structured: Any) -> bool:
    if isinstance(structured, list):
        return len(structured) > 0
    if not isinstance(structured, dict):
        return False
    for key in ("items", "events", "results", "calendarItems", "data"):
        val = structured.get(key)
        if isinstance(val, list) and val:
            return True
    return False


def _normalize_tool_result(result: Any) -> Any:
    texts = _content_texts(result)
    if getattr(result, "isError", False):
        raise RuntimeError("; ".join(texts) or f"MCP tool error: {result}")
    failure = _connection_failure(texts)
    structured = getattr(result, "structuredContent", None)
    if failure and not _structured_has_items(structured):
        raise RuntimeError(failure)

    if structured is not None:
        return structured

    if not texts:
        return None
    if len(texts) == 1:
        raw = texts[0]
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return raw
    return texts


class McpRegistry:
    """Keeps long-lived MCP sessions alive across dashboard requests."""

    def __init__(self) -> None:
        self._servers: dict[str, McpServerHandle] = {}
        self._start_lock = asyncio.Lock()

    async def get_stdio(
        self,
        name: str,
        command: str,
        args: list[str] | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> McpServerHandle:
        existing = self._servers.get(name)
        if existing:
            return existing
        async with self._start_lock:
            existing = self._servers.get(name)
            if existing:
                return existing
            stack = AsyncExitStack()
            params = StdioServerParameters(
                command=command,
                args=args or [],
                cwd=cwd,
                env=env,
            )
            read, write = await stack.enter_async_context(stdio_client(params))
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            handle = McpServerHandle(name, session, stack)
            self._servers[name] = handle
            logger.info("MCP stdio session ready: %s", name)
            return handle

    async def get_http(
        self,
        name: str,
        url: str,
        headers: dict[str, str] | None = None,
    ) -> McpServerHandle:
        existing = self._servers.get(name)
        if existing:
            return existing
        async with self._start_lock:
            existing = self._servers.get(name)
            if existing:
                return existing
            stack = AsyncExitStack()
            http_client = create_mcp_http_client(headers=headers or None)
            await stack.enter_async_context(http_client)
            read, write = await stack.enter_async_context(
                streamable_http_client(url, http_client=http_client)
            )
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            handle = McpServerHandle(name, session, stack)
            self._servers[name] = handle
            logger.info("MCP HTTP session ready: %s", name)
            return handle

    async def drop(self, name: str) -> None:
        """Close one session so the next call opens a fresh process."""
        handle = self._servers.pop(name, None)
        if handle is None:
            return
        try:
            await handle.close()
        except BaseException as exc:
            if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            if not is_cancel_scope_noise(exc):
                logger.exception("Error closing MCP server %s", name)

    async def close_all(self) -> None:
        for name in list(self._servers):
            await self.drop(name)


registry = McpRegistry()
