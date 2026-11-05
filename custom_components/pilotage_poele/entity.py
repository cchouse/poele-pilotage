from __future__ import annotations

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity

from .const import DOMAIN
from .runtime import Pilote


class PoeleEntity(Entity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, p: Pilote, cle: str) -> None:
        self.p = p
        self._attr_translation_key = cle
        self._attr_unique_id = f"{p.entry.entry_id}_{cle}"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, p.entry.entry_id)}, name="Pilotage poêle",
                                            manufacturer="B-Home", model="Pilotage poêle")

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(self.hass, self.p.signal, self._maj))

    @callback
    def _maj(self) -> None:
        self.async_write_ha_state()
