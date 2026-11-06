"""Une consigne par zone (Chambre, Maison) : la changer = dérogation jusqu'à la prochaine tranche du planning."""
from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import UnitOfTemperature

from .entity import PoeleEntity
from .planning import CONSIGNE_MAX, CONSIGNE_MIN, ZONES


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    p = entry.runtime_data
    async_add_entities([ConsigneZone(p, z) for z in ZONES])


class ConsigneZone(PoeleEntity, NumberEntity):
    _attr_native_min_value = CONSIGNE_MIN
    _attr_native_max_value = CONSIGNE_MAX
    _attr_native_step = 0.5
    _attr_mode = NumberMode.BOX
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

    def __init__(self, p, zone: str) -> None:
        super().__init__(p, f"consigne_{zone}")
        self._zone = zone

    @property
    def native_value(self) -> float | None:
        from homeassistant.util import dt as dt_util
        r = self.p.consigne_zone(self._zone, dt_util.now())
        return None if r is None else r[0]

    @property
    def extra_state_attributes(self):
        from homeassistant.util import dt as dt_util
        r = self.p.consigne_zone(self._zone, dt_util.now())
        return {"source": "aucune" if r is None else r[1]}

    async def async_set_native_value(self, value: float) -> None:
        await self.p.definir_derogation(self._zone, value)
