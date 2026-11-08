"""WebSocket transport that lets Home Assistant act as an MCP server for Xiaozhi.

Adapted for Home Assistant 2026.11 / MCP SDK 2.x.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import re
from typing import Any

import aiohttp
import anyio
from mcp import types
from pydantic import TypeAdapter

try:  # MCP 1.x and (most likely) 2.x
    from mcp.shared.message import SessionMessage
except ImportError:  # pragma: no cover - defensive fallback
    SessionMessage = None  # type: ignore[assignment,misc]

from homeassistant.components import conversation
from homeassistant.const import CONF_LLM_HASS_API
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import llm

from .const import DOMAIN
from .server import create_server
from .session import Session
from .types import WsMCPServerConfigEntry

_LOGGER = logging.getLogger(__name__)

# JSONRPCMessage is a union type in MCP 2.x, so validate through a TypeAdapter.
_JSONRPC_ADAPTER: TypeAdapter[Any] = TypeAdapter(types.JSONRPCMessage)

RECONNECT_MIN_DELAY = 20
RECONNECT_MAX_DELAY = 120

_TOKEN_RE = re.compile(r"(token=)[^&\s]+", re.IGNORECASE)


def _redact(endpoint: str) -> str:
    """Hide the access token of the Xiaozhi endpoint before logging it."""
    return _TOKEN_RE.sub(r"\1***", endpoint)


async def async_setup_entry(hass: HomeAssistant, entry: WsMCPServerConfigEntry) -> bool:
    """Start the reconnect loop for a config entry."""
    # Background tasks created through the entry are cancelled on unload.
    entry.async_create_background_task(
        hass, _connect_loop(hass, entry), f"{DOMAIN}_{entry.entry_id}_connect_loop"
    )
    return True


async def _connect_loop(hass: HomeAssistant, entry: WsMCPServerConfigEntry) -> None:
    """Reconnect on failure loop."""
    delay = RECONNECT_MIN_DELAY
    while True:
        started = hass.loop.time()
        try:
            _LOGGER.info("mcp websocket loop")
            if await _connect_to_client(hass, entry) is False:
                break
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("mcp WebSocket disconnected or failed: %s", err)
        # A connection that lived for a while resets the back-off.
        if hass.loop.time() - started > 60:
            delay = RECONNECT_MIN_DELAY
        _LOGGER.info("mcp websocket retry after %s seconds", delay)
        await asyncio.sleep(delay)
        delay = min(delay * 2, RECONNECT_MAX_DELAY)


async def _build_ha_context(hass: HomeAssistant) -> Context:
    """Return a real Home Assistant Context that carries an administrator user.

    HA 2026.11 validates the LLM context and some tools require an admin user.
    """
    user_id: str | None = None
    try:
        users = await hass.auth.async_get_users()
        candidates = [
            user
            for user in users
            if user.is_active and not user.system_generated and user.is_admin
        ]
        candidates.sort(key=lambda user: not user.is_owner)
        if candidates:
            user_id = candidates[0].id
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("mcp could not look up an admin user: %s", err)
    return Context(user_id=user_id)


def _build_llm_context(ha_context: Context) -> llm.LLMContext:
    """Create the LLMContext in a way that tolerates signature changes."""
    kwargs: dict[str, Any] = {
        "platform": DOMAIN,
        "context": ha_context,
        "language": "*",
        "assistant": conversation.DOMAIN,
        "device_id": None,
    }
    if dataclasses.is_dataclass(llm.LLMContext):
        field_names = {field.name for field in dataclasses.fields(llm.LLMContext)}
        if "requires_admin" in field_names:
            kwargs["requires_admin"] = True
    return llm.LLMContext(**kwargs)


async def _connect_to_client(hass: HomeAssistant, entry: WsMCPServerConfigEntry) -> bool:
    """Connect to the Xiaozhi WebSocket endpoint and serve MCP over it."""
    session_manager = hass.data[DOMAIN][entry.entry_id]["session_manager"]
    endpoint = entry.data.get("client_endpoint")
    if not endpoint:
        _LOGGER.error("No client endpoint configured in config entry")
        return False

    context = _build_llm_context(await _build_ha_context(hass))
    llm_api_id = entry.data[CONF_LLM_HASS_API]
    _LOGGER.info("mcp llm_api_id: %s", llm_api_id)
    server = await create_server(hass, llm_api_id, context, read_only=False)
    options = await hass.async_add_executor_job(server.create_initialization_options)

    read_stream_writer, read_stream = anyio.create_memory_object_stream(0)
    write_stream, write_stream_reader = anyio.create_memory_object_stream(0)

    reconnect = True
    async with session_manager.create(Session(read_stream_writer)):
        _LOGGER.info("mcp Connecting to MCP client at: %s", _redact(endpoint))
        timeout = aiohttp.ClientTimeout(total=60)
        async with aiohttp.ClientSession(timeout=timeout) as client_session:
            try:
                async with client_session.ws_connect(endpoint) as ws:

                    async def ws_reader() -> None:
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                try:
                                    message = _JSONRPC_ADAPTER.validate_python(
                                        msg.json()
                                    )
                                    _LOGGER.debug("mcp reader: %s", message)
                                    if SessionMessage is not None:
                                        message = SessionMessage(message)
                                    await read_stream_writer.send(message)
                                except Exception as err:  # noqa: BLE001
                                    _LOGGER.error(
                                        "mcp Invalid message from client: %s", err
                                    )
                            elif msg.type == aiohttp.WSMsgType.CLOSE:
                                _LOGGER.error("mcp WebSocket closed: %s", msg.extra)
                            elif msg.type == aiohttp.WSMsgType.ERROR:
                                _LOGGER.error("mcp WebSocket error: %s", msg.data)
                        _LOGGER.info("websocket was closed")
                        # Cancel the group right away instead of waiting for the heartbeat.
                        tg.cancel_scope.cancel()

                    async def ws_writer() -> None:
                        nonlocal reconnect
                        async for item in write_stream_reader:
                            _LOGGER.debug("mcp writer: %s", item)
                            actual_message = getattr(item, "message", item)
                            await ws.send_str(
                                actual_message.model_dump_json(
                                    by_alias=True, exclude_none=True
                                )
                            )
                        _LOGGER.info("disconnect websocket")
                        reconnect = False
                        await ws.close()

                    async def heartbeat() -> None:
                        while True:
                            try:
                                await asyncio.sleep(50)
                                _LOGGER.debug("mcp heartbeat")
                                await ws.ping()
                            except Exception as err:  # noqa: BLE001
                                _LOGGER.info("mcp heartbeat ping failed: %s", err)
                                break  # close the connection so that it reconnects

                    async with anyio.create_task_group() as tg:
                        tg.start_soon(ws_reader)
                        tg.start_soon(ws_writer)
                        tg.start_soon(heartbeat)
                        await server.run(read_stream, write_stream, options)
            except Exception as err:  # noqa: BLE001
                _LOGGER.error(
                    "mcp Failed to connect to client WebSocket at %s: %s",
                    _redact(endpoint),
                    _redact(str(err)),
                )
                _LOGGER.debug("mcp connection error details", exc_info=True)
    return reconnect
