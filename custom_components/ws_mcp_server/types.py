"""Types for the MCP server integration."""

from homeassistant.config_entries import ConfigEntry

# The session manager lives in hass.data[DOMAIN][entry_id]["session_manager"]
# (``ConfigEntry.runtime_data`` is not used any more).
WsMCPServerConfigEntry = ConfigEntry
