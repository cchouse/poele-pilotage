from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity

from .entity import PoeleEntity


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    async_add_entities([Probleme(entry.runtime_data)])


class Probleme(PoeleEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, p) -> None:
        super().__init__(p, "probleme")

    @property
    def is_on(self) -> bool:
        return self.p.etat == "probleme" or self.p.drapeaux["verrou"]
