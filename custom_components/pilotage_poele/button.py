from __future__ import annotations

from homeassistant.components.button import ButtonEntity

from .entity import PoeleEntity


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    p = entry.runtime_data
    async_add_entities([Allumer(p), Annuler(p)])


class Allumer(PoeleEntity, ButtonEntity):
    def __init__(self, p) -> None:
        super().__init__(p, "allumer")

    async def async_press(self) -> None:
        await self.p.allumer_maintenant()


class Annuler(PoeleEntity, ButtonEntity):
    def __init__(self, p) -> None:
        super().__init__(p, "annuler")

    async def async_press(self) -> None:
        await self.p.annuler()
