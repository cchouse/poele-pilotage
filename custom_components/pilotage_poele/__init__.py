"""Pilotage poêle : planifie, apprend et (si vous l'autorisez) allume le poêle à granulés au bon moment."""
from __future__ import annotations

from pathlib import Path

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN, URL_CARTE, VERSION
from .runtime import Pilote

PLATFORMS = ["sensor", "binary_sensor", "switch", "button"]
type PoeleConfigEntry = ConfigEntry[Pilote]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    chemin = Path(__file__).parent / "www" / "pilotage-poele-card.js"
    await hass.http.async_register_static_paths([StaticPathConfig(URL_CARTE, str(chemin), False)])
    add_extra_js_url(hass, f"{URL_CARTE}?v={VERSION}")
    return True


async def async_setup_entry(hass: HomeAssistant, entry: PoeleConfigEntry) -> bool:
    pilote = Pilote(hass, entry)
    await pilote.async_demarrer()
    entry.runtime_data = pilote
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_recharger))
    return True


async def _recharger(hass: HomeAssistant, entry: PoeleConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: PoeleConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        await entry.runtime_data.async_arreter()
    return ok
