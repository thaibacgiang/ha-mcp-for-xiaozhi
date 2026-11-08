"""Exercise the MCP 2.x style server callbacks without a running Home Assistant.

External dependencies are stubbed, so this checks the handler logic and the
constructor-based registration only. It does NOT prove compatibility with a
real ``mcp`` 2.x or Home Assistant install.
"""

import ast
from dataclasses import dataclass, replace
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
import unittest
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "ws_mcp_server"


@dataclass
class FakeContext:
    device_id: str | None = None


class FakeServer:
    """Captures the on_* handlers passed to the constructor (MCP 2.x style)."""

    def __init__(self, name, **handlers):
        self.name = name
        self.handlers = handlers


class FakeTypes:
    def __getattr__(self, name):
        return lambda **kwargs: SimpleNamespace(_type=name, **kwargs)


class ServerCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tree = ast.parse((COMPONENT / "server.py").read_text(encoding="utf-8"))
        wanted = {"create_server", "_has_live_context_tool", "_get_device_id", "_error_result"}
        nodes = [
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in wanted
        ]
        self.api = SimpleNamespace(
            tools=[SimpleNamespace(name="homeassistant__GetLiveContext")],
            api=SimpleNamespace(name="Assist"),
            api_prompt="prompt",
            custom_serializer=None,
            async_call_tool=AsyncMock(),
        )
        namespace = {
            "Any": Any,
            "Server": FakeServer,
            "STATELESS_LLM_API": "stateless",
            "META_DEVICE_ID": "io.home-assistant/device_id",
            "LIVE_CONTEXT_TOOL_NAME": "homeassistant__GetLiveContext",
            "SNAPSHOT_RESOURCE_URI": "homeassistant://assist/context-snapshot",
            "SNAPSHOT_RESOURCE_MIME_TYPE": "text/plain",
            "llm": SimpleNamespace(
                LLM_API_ASSIST="assist",
                async_get_api=AsyncMock(return_value=self.api),
                ToolInput=lambda **kwargs: SimpleNamespace(**kwargs),
            ),
            "types": FakeTypes(),
            "HomeAssistantError": RuntimeError,
            "probatio": SimpleNamespace(Invalid=ValueError),
            "_LOGGER": SimpleNamespace(debug=lambda *args: None),
            "_format_tool": lambda tool, ser: tool,
            "json": json,
            "cast": cast,
            "replace": replace,
        }
        module = ast.Module(
            body=[
                ast.ImportFrom(
                    module="__future__", names=[ast.alias(name="annotations")], level=0
                ),
                *nodes,
            ],
            type_ignores=[],
        )
        exec(compile(ast.fix_missing_locations(module), "server.py", "exec"), namespace)
        self.ctx = SimpleNamespace(meta=None)
        server = await namespace["create_server"](None, "assist", FakeContext(), read_only=False)
        self.handlers = server.handlers

    async def test_all_handlers_registered_in_constructor(self):
        self.assertEqual(
            set(self.handlers),
            {
                "on_list_prompts",
                "on_get_prompt",
                "on_list_resources",
                "on_read_resource",
                "on_list_tools",
                "on_call_tool",
            },
        )

    async def test_call_tool_preserves_data_and_error_flag(self):
        for error in (False, True):
            with self.subTest(error=error):
                data = {"message": "测试", "nested": [1, {"success": not error}]}
                self.api.async_call_tool.return_value = SimpleNamespace(data=data, error=error)
                params = SimpleNamespace(name="test", arguments=None)
                result = await self.handlers["on_call_tool"](self.ctx, params)
                self.assertEqual(json.loads(result.content[0].text), data)
                self.assertIs(result.is_error, error)

    async def test_call_tool_exception_becomes_model_visible_error(self):
        self.api.async_call_tool.side_effect = RuntimeError("boom")
        params = SimpleNamespace(name="test", arguments={})
        result = await self.handlers["on_call_tool"](self.ctx, params)
        self.assertTrue(result.is_error)
        self.assertIn("boom", result.content[0].text)

    async def test_context_resource_reads_tool_result_data(self):
        self.api.async_call_tool.return_value = SimpleNamespace(
            data={"result": "Sony XB10 is idle"}, error=False
        )
        params = SimpleNamespace(uri="homeassistant://assist/context-snapshot")
        result = await self.handlers["on_read_resource"](self.ctx, params)
        self.assertEqual(result.contents[0].text, "Sony XB10 is idle")

    async def test_context_resource_propagates_tool_error(self):
        self.api.async_call_tool.return_value = SimpleNamespace(
            data={"error": "Context unavailable"}, error=True
        )
        params = SimpleNamespace(uri="homeassistant://assist/context-snapshot")
        with self.assertRaisesRegex(RuntimeError, "Context unavailable"):
            await self.handlers["on_read_resource"](self.ctx, params)

    async def test_device_id_must_be_string(self):
        ctx = SimpleNamespace(meta=SimpleNamespace(**{}))
        setattr(ctx.meta, "io.home-assistant/device_id", 123)
        self.api.async_call_tool.return_value = SimpleNamespace(data={}, error=False)
        with self.assertRaises(ValueError):
            await self.handlers["on_call_tool"](ctx, SimpleNamespace(name="t", arguments={}))


if __name__ == "__main__":
    unittest.main()
