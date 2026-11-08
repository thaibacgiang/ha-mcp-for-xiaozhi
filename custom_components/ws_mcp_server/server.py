"""The Model Context Protocol Server implementation (MCP SDK 2.x).

Adapted for Home Assistant 2026.11, which ships ``mcp`` 2.x. In MCP 2.x the
low-level ``Server`` no longer supports decorators: handlers are passed to the
constructor as ``on_*`` callbacks, take ``(ctx, params)`` and must return full
result objects. Tool exceptions are no longer wrapped automatically, so they are
converted to ``CallToolResult(is_error=True)`` here.
"""

from __future__ import annotations

from dataclasses import replace
import json
import logging
from typing import Any, cast

from mcp import types
from mcp.server import Server
import probatio

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import llm

from .const import STATELESS_LLM_API

_LOGGER = logging.getLogger(__name__)

SNAPSHOT_RESOURCE_URI = "homeassistant://assist/context-snapshot"
SNAPSHOT_RESOURCE_MIME_TYPE = "text/plain"
LIVE_CONTEXT_TOOL_NAME = "homeassistant__GetLiveContext"
META_DEVICE_ID = "io.home-assistant/device_id"


def _has_live_context_tool(llm_api: llm.APIInstance) -> bool:
    """Return if the selected API exposes the live context tool."""
    return any(tool.name == LIVE_CONTEXT_TOOL_NAME for tool in llm_api.tools)


def _get_device_id(ctx: Any) -> str | None:
    """Read the optional device id from the request metadata."""
    meta = getattr(ctx, "meta", None)
    device_id = None
    if meta is not None:
        device_id = getattr(meta, META_DEVICE_ID, None)
        if device_id is None and isinstance(meta, dict):
            device_id = meta.get(META_DEVICE_ID)
        if device_id is None and hasattr(meta, "model_extra") and meta.model_extra:
            device_id = meta.model_extra.get(META_DEVICE_ID)
    if device_id is not None and not isinstance(device_id, str):
        raise ValueError(f"{META_DEVICE_ID} must be a string")
    return device_id


def _format_tool(tool: llm.Tool, custom_serializer: Any) -> types.Tool:
    """Format tool specification."""
    input_schema = probatio.to_openapi(
        tool.parameters,
        custom_serializer=custom_serializer,
        openapi_version="3.1.0",
    )
    mcp_schema: dict[str, Any] = {
        "type": "object",
        "properties": input_schema["properties"],
    }
    # Omitted by to_openapi when the tool has no required parameters.
    if required := input_schema.get("required"):
        mcp_schema["required"] = required
    return types.Tool(
        name=tool.name,
        title=tool.title,
        description=tool.description or "",
        input_schema=mcp_schema,
        annotations=types.ToolAnnotations(
            read_only_hint=tool.annotations.read_only,
            destructive_hint=tool.annotations.destructive,
            idempotent_hint=tool.annotations.idempotent,
            open_world_hint=tool.annotations.open_world,
        ),
    )


def _error_result(message: str) -> types.CallToolResult:
    """Build a model-visible tool error."""
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=message)],
        is_error=True,
    )


async def create_server(
    hass: HomeAssistant,
    llm_api_id: str | list[str],
    llm_context: llm.LLMContext,
    read_only: bool,
) -> Server:
    """Create a new Model Context Protocol Server.

    A Model Context Protocol Server object is associated with a single session.
    The MCP SDK handles the details of the protocol.
    """
    if llm_api_id == STATELESS_LLM_API:
        llm_api_id = llm.LLM_API_ASSIST

    async def get_api_instance(ctx: Any) -> llm.APIInstance:
        """Get the LLM API selected."""
        device_id = _get_device_id(ctx)
        llm_api = await llm.async_get_api(
            hass, llm_api_id, replace(llm_context, device_id=device_id)
        )
        if read_only:
            # Removing the tools also blocks calls to them.
            llm_api.tools = [
                tool for tool in llm_api.tools if tool.annotations.read_only
            ]
        return llm_api

    async def handle_list_prompts(
        ctx: Any, params: Any = None
    ) -> types.ListPromptsResult:
        llm_api = await get_api_instance(ctx)
        return types.ListPromptsResult(
            prompts=[
                types.Prompt(
                    name=llm_api.api.name,
                    description=(
                        f"Default prompt for Home Assistant {llm_api.api.name} API"
                    ),
                )
            ]
        )

    async def handle_get_prompt(
        ctx: Any, params: Any
    ) -> types.GetPromptResult:
        llm_api = await get_api_instance(ctx)
        if params.name != llm_api.api.name:
            raise ValueError(f"Unknown prompt: {params.name}")

        return types.GetPromptResult(
            description=f"Default prompt for Home Assistant {llm_api.api.name} API",
            messages=[
                types.PromptMessage(
                    role="assistant",
                    content=types.TextContent(
                        type="text",
                        text=llm_api.api_prompt,
                    ),
                )
            ],
        )

    async def handle_list_resources(
        ctx: Any, params: Any = None
    ) -> types.ListResourcesResult:
        llm_api = await get_api_instance(ctx)
        if not _has_live_context_tool(llm_api):
            return types.ListResourcesResult(resources=[])

        return types.ListResourcesResult(
            resources=[
                types.Resource(
                    uri=SNAPSHOT_RESOURCE_URI,
                    name="assist_context_snapshot",
                    title="Assist context snapshot",
                    description=(
                        "A snapshot of the current Assist context, matching the"
                        " existing homeassistant__GetLiveContext tool output."
                    ),
                    mime_type=SNAPSHOT_RESOURCE_MIME_TYPE,
                )
            ]
        )

    async def handle_read_resource(
        ctx: Any, params: Any
    ) -> types.ReadResourceResult:
        if str(params.uri) != SNAPSHOT_RESOURCE_URI:
            raise ValueError(f"Unknown resource: {params.uri}")

        llm_api = await get_api_instance(ctx)
        if not _has_live_context_tool(llm_api):
            raise ValueError(f"Unknown resource: {params.uri}")

        tool_response = await llm_api.async_call_tool(
            llm.ToolInput(tool_name=LIVE_CONTEXT_TOOL_NAME, tool_args={})
        )
        if tool_response.error:
            raise HomeAssistantError(cast(str, tool_response.data["error"]))

        return types.ReadResourceResult(
            contents=[
                types.TextResourceContents(
                    uri=SNAPSHOT_RESOURCE_URI,
                    mime_type=SNAPSHOT_RESOURCE_MIME_TYPE,
                    text=cast(str, tool_response.data["result"]),
                )
            ]
        )

    async def handle_list_tools(
        ctx: Any, params: Any = None
    ) -> types.ListToolsResult:
        """List available tools."""
        llm_api = await get_api_instance(ctx)
        return types.ListToolsResult(
            tools=[
                _format_tool(tool, llm_api.custom_serializer)
                for tool in llm_api.tools
            ]
        )

    async def handle_call_tool(
        ctx: Any, params: Any
    ) -> types.CallToolResult:
        """Handle calling tools."""
        llm_api = await get_api_instance(ctx)
        tool_input = llm.ToolInput(
            tool_name=params.name, tool_args=params.arguments or {}
        )
        _LOGGER.debug("Tool call: %s(%s)", tool_input.tool_name, tool_input.tool_args)

        try:
            tool_response = await llm_api.async_call_tool(tool_input)
        except (HomeAssistantError, probatio.Invalid) as err:
            # MCP 2.x no longer converts handler exceptions to tool errors.
            return _error_result(f"Error calling tool: {err}")
        return types.CallToolResult(
            content=[
                types.TextContent(
                    type="text",
                    text=json.dumps(tool_response.data, ensure_ascii=False),
                )
            ],
            is_error=bool(tool_response.error),
        )

    return Server(
        "home-assistant",
        on_list_prompts=handle_list_prompts,
        on_get_prompt=handle_get_prompt,
        on_list_resources=handle_list_resources,
        on_read_resource=handle_read_resource,
        on_list_tools=handle_list_tools,
        on_call_tool=handle_call_tool,
    )
