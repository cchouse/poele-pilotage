"""Pilotage poêle : planifie, apprend et (si vous l'autorisez) allume le poêle à granulés au bon moment."""
from __future__ import annotations

from pathlib import Path

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN, URL_CARTE, VERSION
from .planning import ZONES
from .runtime import Pilote

PLATFORMS = ["sensor", "binary_sensor", "switch", "button", "number"]
type PoeleConfigEntry = ConfigEntry[Pilote]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    chemin = Path(__file__).parent / "www" / "pilotage-poele-card.js"
    await hass.http.async_register_static_paths([StaticPathConfig(URL_CARTE, str(chemin), False)])
    add_extra_js_url(hass, f"{URL_CARTE}?v={VERSION}")
    _services(hass)
    return True


def _pilote(hass: HomeAssistant) -> Pilote:
    for e in hass.config_entries.async_entries(DOMAIN):
        if getattr(e, "runtime_data", None) is not None:
            return e.runtime_data
    raise ServiceValidationError("Pilotage poêle n'est pas démarré.")


def _services(hass: HomeAssistant) -> None:
    """Services utilisés par la carte (et utilisables dans vos automatisations)."""
    zone = vol.In(list(ZONES))

    async def definir_planning(call: ServiceCall) -> None:
        await _pilote(hass).definir_planning(call.data["zone"], call.data["planning"])

    async def derogation(call: ServiceCall) -> None:
        await _pilote(hass).definir_derogation(call.data["zone"], call.data["consigne"])

    async def retour(call: ServiceCall) -> None:
        await _pilote(hass).retour_planning(call.data["zone"])

    hass.services.async_register(DOMAIN, "definir_planning", definir_planning,
                                 vol.Schema({vol.Required("zone"): zone, vol.Required("planning"): dict}))
    hass.services.async_register(DOMAIN, "derogation", derogation,
                                 vol.Schema({vol.Required("zone"): zone, vol.Required("consigne"): vol.Coerce(float)}))
    hass.services.async_register(DOMAIN, "retour_planning", retour, vol.Schema({vol.Required("zone"): zone}))


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
