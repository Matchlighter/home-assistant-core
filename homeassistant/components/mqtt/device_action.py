"""Provides device automations for MQTT."""
from __future__ import annotations

from collections.abc import Callable
import logging
from typing import Any

import attr
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_ALIAS,
    CONF_DEVICE,
    CONF_DOMAIN,
    CONF_NAME,
    CONF_PLATFORM,
)
from homeassistant.core import Context, CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

from . import debug_info
from .client import async_publish
from .config import DEFAULT_RETAIN, MQTT_BASE_SCHEMA
from .const import (
    ATTR_DISCOVERY_HASH,
    CONF_COMMAND_TEMPLATE,
    CONF_COMMAND_TOPIC,
    CONF_ENCODING,
    CONF_PAYLOAD,
    CONF_QOS,
    CONF_RETAIN,
    DOMAIN,
)
from .discovery import MQTTDiscoveryPayload
from .mixins import (
    MQTT_ENTITY_DEVICE_INFO_SCHEMA,
    MqttDiscoveryDeviceUpdate,
    send_discovery_done,
    update_device,
)
from .models import MqttCommandTemplate, PublishPayloadType
from .util import get_mqtt_data, valid_publish_topic

_LOGGER = logging.getLogger(__name__)

CONF_AUTOMATION_TYPE = "automation_type"
CONF_DISCOVERY_ID = "discovery_id"
DEFAULT_PAYLOAD = "set"
DEVICE = "device"

MQTT_ACTION_BASE = {
    # Trigger when MQTT message is received
    CONF_PLATFORM: DEVICE,
    CONF_DOMAIN: DOMAIN,
}

ACTION_DISCOVERY_SCHEMA = MQTT_BASE_SCHEMA.extend(
    {
        vol.Required(CONF_AUTOMATION_TYPE): str,
        vol.Optional(CONF_COMMAND_TEMPLATE): cv.template,
        vol.Required(CONF_COMMAND_TOPIC): valid_publish_topic,
        vol.Required(CONF_DEVICE): MQTT_ENTITY_DEVICE_INFO_SCHEMA,
        vol.Optional(CONF_NAME): vol.Any(cv.string, None),
        vol.Optional(CONF_PAYLOAD, default=DEFAULT_PAYLOAD): cv.string,
        vol.Optional(CONF_RETAIN, default=DEFAULT_RETAIN): cv.boolean,
    },
    extra=vol.REMOVE_EXTRA,
)

LOG_NAME = "Device action"


class MqttDeviceAction(MqttDiscoveryDeviceUpdate):
    """Setup a MQTT device action with auto discovery."""

    _command_template: Callable[[PublishPayloadType], PublishPayloadType]

    def __init__(
        self,
        hass: HomeAssistant,
        config: ConfigType,
        device_id: str,
        discovery_data: DiscoveryInfoType,
        config_entry: ConfigEntry,
    ) -> None:
        """Initialize."""
        self._config = config
        self._config_entry = config_entry
        self.device_id = device_id
        self.discovery_data = discovery_data
        self.hass = hass
        self._mqtt_data = get_mqtt_data(hass)

        MqttDiscoveryDeviceUpdate.__init__(
            self,
            hass,
            discovery_data,
            device_id,
            config_entry,
            LOG_NAME,
        )

    @property
    def name(self):
        return self._config[CONF_NAME]

    @property
    def command_topic(self):
        return self._config[CONF_COMMAND_TOPIC]

    @property
    def discovery_hash(self):
        return self.discovery_data[ATTR_DISCOVERY_HASH]

    @property
    def discovery_id(self):
        return self.discovery_hash[1]

    async def async_setup(self) -> None:
        """Initialize the device action."""
        self._mqtt_data.device_actions[self.discovery_id] = self

        self._command_template = MqttCommandTemplate(
            self._config.get(CONF_COMMAND_TEMPLATE), entity=self
        ).async_render

        debug_info.add_action_discovery_data(
            self.hass, self.discovery_hash, self.discovery_data, self.device_id
        )

    async def async_update(self, discovery_data: MQTTDiscoveryPayload) -> None:
        """Handle MQTT device action discovery updates."""
        debug_info.update_action_discovery_data(
            self.hass, self.discovery_hash, discovery_data
        )
        config = ACTION_DISCOVERY_SCHEMA(discovery_data)
        update_device(self.hass, self._config_entry, config)

    async def async_tear_down(self) -> None:
        """Cleanup device action."""
        if self.discovery_id in self._mqtt_data.device_actions:
            _LOGGER.info("Removing action: %s", self.discovery_hash)
            debug_info.remove_action_discovery_data(self.hass, self.discovery_hash)

    async def fire(self):
        """Fire the Action"""
        payload = self._command_template(self._config[CONF_PAYLOAD])
        await async_publish(
            self.hass,
            self._config[CONF_COMMAND_TOPIC],
            payload,
            self._config[CONF_QOS],
            self._config[CONF_RETAIN],
            self._config[CONF_ENCODING],
        )


async def async_setup_action(
    hass: HomeAssistant,
    config: ConfigType,
    config_entry: ConfigEntry,
    discovery_data: DiscoveryInfoType,
) -> None:
    """Set up the MQTT device action."""
    config = ACTION_DISCOVERY_SCHEMA(config)
    device_id = update_device(hass, config_entry, config)

    assert isinstance(device_id, str)
    mqtt_device_action = MqttDeviceAction(
        hass, config, device_id, discovery_data, config_entry
    )
    await mqtt_device_action.async_setup()
    send_discovery_done(hass, discovery_data)


async def async_removed_from_device(hass: HomeAssistant, device_id: str) -> None:
    """Handle Mqtt removed from a device."""
    mqtt_data = get_mqtt_data(hass)
    actions = await async_get_actions(hass, device_id)
    for action in actions:
        device_action = mqtt_data.device_actions.pop(action[CONF_DISCOVERY_ID])
        if device_action:
            discovery_data = device_action.discovery_data
            assert discovery_data is not None
            discovery_hash = discovery_data[ATTR_DISCOVERY_HASH]
            debug_info.remove_action_discovery_data(hass, discovery_hash)


async def async_get_actions(
    hass: HomeAssistant, device_id: str
) -> list[dict[str, Any]]:
    """List device actions for MQTT devices."""
    mqtt_data = get_mqtt_data(hass)
    actions: list[dict] = []

    if not mqtt_data.device_actions:
        return actions

    for discovery_id, action in mqtt_data.device_actions.items():
        if action.device_id != device_id or action.command_topic is None:
            continue

        action = {
            **MQTT_ACTION_BASE,
            CONF_ALIAS: action.name,
            "device_id": device_id,
            "discovery_id": discovery_id,
        }
        actions.append(action)

    return actions


async def async_get_action_capabilities(
    hass: HomeAssistant, config: ConfigType
) -> dict[str, vol.Schema]:
    """List action capabilities."""
    mqtt_data = get_mqtt_data(hass)
    action = mqtt_data.device_actions[config["discovery_id"]]

    # # TODO
    # fields = {vol.Required(ATTR_VALUE): cv.string}
    # return {"extra_fields": vol.Schema(fields)}
    return {}


async def async_call_action_from_config(
    hass: HomeAssistant, config: dict, variables: dict, context: Context | None
) -> None:
    """Execute a device action."""
    mqtt_data = get_mqtt_data(hass)
    action = mqtt_data.device_actions[config["discovery_id"]]
    await action.fire()
