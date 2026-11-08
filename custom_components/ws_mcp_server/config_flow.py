import inspect

import voluptuous as vol
from typing import Any
import logging
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_LLM_HASS_API
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import llm,selector
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector
)

from homeassistant.config_entries import ConfigEntry
from .session import SessionManager

from .const import DOMAIN

CONF_CLIENT_ENDPOINT = "client_endpoint"
CONF_MODE = "control_mode"
MORE_INFO_URL = "https://www.home-assistant.io/integrations/mcp_server/#configuration"
DEFAULT_NAME = "WebSocket MCP Server"
_LOGGER = logging.getLogger(__name__)



class WsMCPServerConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for MCP Server."""
    VERSION = 1
    SUPPORT_MULTIPLE_ENTRIES = True  # 添加这行来支持多实例

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}
        apis = llm.async_get_apis(self.hass)
        if inspect.isawaitable(apis):  # tolerate a future async signature
            apis = await apis
        _LOGGER.debug("mcp  LLM APIs available: %s", apis)
        llm_apis = {api.id: api.name for api in apis}
        if user_input is not None:
            # 为每个配置创建唯一标识
            title = f"{user_input[CONF_LLM_HASS_API]}"
            
            # 验证配置是否重复
            await self.async_set_unique_id(f"{user_input[CONF_LLM_HASS_API]}_{user_input[CONF_CLIENT_ENDPOINT]}")
            self._abort_if_unique_id_configured()

            if not user_input[CONF_LLM_HASS_API]:
                errors[CONF_LLM_HASS_API] = "llm_api_required"
            else:
                return self.async_create_entry(
                    title=", ".join(
                        llm_apis[api_id] for api_id in user_input[CONF_LLM_HASS_API]
                    ),
                    data=user_input,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    #vol.Required(CONF_CLIENT_ENDPOINT): TextSelector(),
                    vol.Required(CONF_CLIENT_ENDPOINT): selector.TextSelector(),
                    vol.Optional(
                        CONF_LLM_HASS_API,          # llm_hass_api
                        default=[llm.LLM_API_ASSIST], # assist
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                SelectOptionDict(
                                    label=name,
                                    value=llm_api_id,
                                )
                                for llm_api_id, name in llm_apis.items()
                            ],
                            multiple=True,
                        )
                    ),
                }
            ),
            description_placeholders={"more_info_url": MORE_INFO_URL},
            errors=errors,
        )




