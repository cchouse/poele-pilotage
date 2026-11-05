from __future__ import annotations

from homeassistant.components.switch import SwitchEntity

from .entity import PoeleEntity


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    p = entry.runtime_data
    async_add_entities([Drapeau(p, "auto"), Drapeau(p, "arret_auto"), Drapeau(p, "absent")])


class Drapeau(PoeleEntity, SwitchEntity):
    """Interrupteurs : allumage automatique, arrêt automatique, mode absent. Tous désactivés par défaut."""

    def __init__(self, p, cle: str) -> None:
        super().__init__(p, cle)
        self._cle = cle

    @property
    def is_on(self) -> bool:
        return self.p.drapeaux[self._cle]

    async def async_turn_on(self, **kwargs) -> None:
        await self.p.definir_drapeau(self._cle, True)

    async def async_turn_off(self, **kwargs) -> None:
        await self.p.definir_drapeau(self._cle, False)
